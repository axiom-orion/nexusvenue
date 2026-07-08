"""Deterministic retrieval evaluation: precision/recall@k against the gold set.

The mock-data generator seeds specific BEO ops_notes with signature content
and records which BEOs match each test query (data/goldset.json). This module
runs the real retriever against those queries and scores it — no LLM involved.
"""

import json

from nexusvenue.config import settings
from nexusvenue.rag.retrieve import retrieve


def evaluate_retrieval(k: int = 6) -> dict:
    goldset = json.loads(settings.goldset_path.read_text())
    per_query = []
    for key, spec in goldset.items():
        relevant = set(spec["relevant_beo_ids"])
        if not relevant:
            continue
        hits = retrieve(spec["query"], k=k)["similar_past_events"]
        retrieved = [h["beo_id"] for h in hits]
        tp = len(set(retrieved) & relevant)
        precision = tp / len(retrieved) if retrieved else 0.0
        recall = tp / len(relevant)
        per_query.append({
            "query_key": key,
            "relevant": len(relevant),
            "retrieved": len(retrieved),
            "true_positives": tp,
            f"precision@{k}": round(precision, 3),
            f"recall@{k}": round(recall, 3),
        })

    n = len(per_query) or 1
    return {
        "k": k,
        "queries": per_query,
        "macro_precision": round(sum(q[f"precision@{k}"] for q in per_query) / n, 3),
        "macro_recall": round(sum(q[f"recall@{k}"] for q in per_query) / n, 3),
    }
