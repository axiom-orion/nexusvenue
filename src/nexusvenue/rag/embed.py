"""Embedding backends and graph embedding population.

- "gemini": real semantic embeddings via gemini-embedding-001 (1536-dim).
- "hash":   deterministic pseudo-embeddings derived from token hashes. No API
            key needed; lets the full pipeline (vector index, retrieval, eval
            plumbing) run offline in CI. Token-overlap similarity only - use
            gemini for real semantic quality.
"""

import hashlib
import math
import re

from neo4j import Driver

from nexusvenue.config import settings
from nexusvenue.graph.schema import get_driver

_TOKEN = re.compile(r"[a-z]{3,}")


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


class HashEmbedder:
    """Bag-of-hashed-tokens vectors: identical tokens -> identical dimensions,
    so overlapping text gets high cosine similarity. Deterministic, offline."""

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _TOKEN.findall(text.lower()):
                h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
                vec[h % self.dim] += 1.0
            out.append(_normalize(vec))
        return out


class GeminiEmbedder:
    def __init__(self, dim: int):
        from google import genai  # deferred so hash backend needs no key

        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY not set - set it or use EMBED_BACKEND=hash")
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.dim = dim

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), 100):  # API batch limit
            res = self.client.models.embed_content(
                model=settings.embed_model,
                contents=texts[i:i + 100],
                config=types.EmbedContentConfig(
                    task_type=task, output_dimensionality=self.dim
                ),
            )
            # Truncated-dimension gemini embeddings are not pre-normalized.
            out.extend(_normalize(list(e.values)) for e in res.embeddings)
        return out


def get_embedder():
    if settings.embed_backend == "hash":
        return HashEmbedder(settings.embed_dim)
    return GeminiEmbedder(settings.embed_dim)


def embed_graph(driver: Driver | None = None, batch_size: int = 50,
                missing_only: bool = False) -> dict:
    """Embed BEO ops_notes and RFP raw_text onto their nodes.

    missing_only=True embeds just nodes without an embedding — the cheap path
    after an incremental sync, instead of re-embedding the whole graph."""
    own = driver is None
    driver = driver or get_driver()
    embedder = get_embedder()
    counts = {}

    missing_filter = "AND n.embedding IS NULL " if missing_only else ""
    for label, text_prop in [("BEO", "ops_notes"), ("RFP", "raw_text")]:
        with driver.session() as s:
            rows = s.run(
                f"MATCH (n:{label}) WHERE n.{text_prop} IS NOT NULL {missing_filter}"
                "RETURN n.id AS id, n." + text_prop + " AS text"
            ).data()
        done = 0
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            vectors = embedder.embed([r["text"] for r in chunk])
            payload = [{"id": r["id"], "vec": v} for r, v in zip(chunk, vectors)]
            with driver.session() as s:
                s.run(
                    f"UNWIND $rows AS r MATCH (n:{label} {{id: r.id}}) "
                    "CALL db.create.setNodeVectorProperty(n, 'embedding', r.vec)",
                    rows=payload,
                )
            done += len(chunk)
        counts[label] = done

    if own:
        driver.close()
    return counts
