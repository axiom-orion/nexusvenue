"""Extract the raw relational CRM rows from SQLite."""

import sqlite3
from pathlib import Path

from nexusvenue.config import settings

TABLES = ["accounts", "agencies", "contacts", "rfps", "beo_history"]


def extract(db_path: Path | None = None) -> dict[str, list[dict]]:
    db_path = db_path or settings.crm_db
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found - run `nexusvenue generate` first")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")] for t in TABLES}
    con.close()
    return out
