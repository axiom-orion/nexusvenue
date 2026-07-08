import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "nexusvenue")

    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
    judge_model: str = os.getenv("JUDGE_MODEL", "claude-opus-4-8")

    # Cross-family judging: "anthropic" (default) or "grok". A judge from a
    # different model family than the generator controls for self-preference bias.
    judge_provider: str = os.getenv("JUDGE_PROVIDER", "anthropic")
    xai_api_key: str | None = os.getenv("XAI_API_KEY")
    grok_model: str = os.getenv("GROK_MODEL", "grok-4")

    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    embed_model: str = os.getenv("EMBED_MODEL", "gemini-embedding-001")
    embed_dim: int = int(os.getenv("EMBED_DIM", "1536"))
    # "gemini" for real embeddings, "hash" for deterministic offline vectors
    embed_backend: str = os.getenv("EMBED_BACKEND", "gemini")

    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    @property
    def crm_db(self) -> Path:
        return self.data_dir / "crm.db"

    @property
    def goldset_path(self) -> Path:
        return self.data_dir / "goldset.json"


settings = Settings()
