"""Extract the raw relational CRM rows from SQLite.

`since` filters on last_modified (the SystemModstamp analog): pass the sync
watermark to pull only rows changed since the previous load — the extraction
half of incremental sync.

The comparison is inclusive (>=): a row committed with a stamp exactly equal
to the watermark *after* the previous extraction read it would be silently
lost under a strict >. Rows already processed at the boundary are filtered
out by the loader against the SyncState boundary ids, so re-runs stay no-ops.
"""

import sqlite3
from pathlib import Path

from nexusvenue.config import settings

TABLES = ["accounts", "agencies", "contacts", "rfps", "beo_history"]


def extract(db_path: Path | None = None, since: str | None = None) -> dict[str, list[dict]]:
    db_path = db_path or settings.crm_db
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found - run `nexusvenue generate` first")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if since is None:
        out = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")] for t in TABLES}
    else:
        out = {
            t: [dict(r) for r in con.execute(
                f"SELECT * FROM {t} WHERE last_modified >= ?", (since,))]
            for t in TABLES
        }
    con.close()
    return out
