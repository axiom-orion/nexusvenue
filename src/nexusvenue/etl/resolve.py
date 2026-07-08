"""Entity resolution across property-level CRM silos.

Accounts: the same corporation appears under different legal-name variants at
each property ("Deloitte" / "Deloitte LLP" / "DELOITTE & TOUCHE LLP"). We
normalize legal suffixes and punctuation, then fuzzy-cluster with RapidFuzz
using union-find, producing one canonical account node per real-world entity
with full provenance back to the source rows.

Contacts: resolved primarily on email (exact, case-insensitive), which
collapses spelling drift like "Sarah Mitchell" vs "S. Mitchell".
"""

import re
from collections import defaultdict

from rapidfuzz import fuzz

LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llp|llc|plc|corp(oration)?|co(mpany)?|ltd|holdings|"
    r"group|companies|international|federal services|mutual)\b\.?",
    re.IGNORECASE,
)
PUNCT = re.compile(r"[^\w\s]")
WS = re.compile(r"\s+")

FUZZ_THRESHOLD = 87


def normalize_account_name(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = LEGAL_SUFFIXES.sub(" ", s)
    s = PUNCT.sub(" ", s)
    return WS.sub(" ", s).strip()


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def resolve_accounts(rows: list[dict]) -> list[dict]:
    """Cluster raw account rows into canonical accounts.

    Returns a list of canonical accounts:
      {canonical_id, canonical_name, industry, aliases, source_ids}
    """
    norms = [normalize_account_name(r["account_name"]) for r in rows]
    uf = _UnionFind(len(rows))
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if norms[i] == norms[j] or fuzz.token_sort_ratio(norms[i], norms[j]) >= FUZZ_THRESHOLD:
                uf.union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        clusters[uf.find(i)].append(i)

    canonical = []
    for k, members in enumerate(sorted(clusters.values(), key=lambda m: min(m)), 1):
        member_rows = [rows[i] for i in members]
        # Pick the shortest clean variant as the display name.
        display = min((r["account_name"] for r in member_rows), key=len)
        canonical.append({
            "canonical_id": f"ACCT-{k:04d}",
            "canonical_name": display,
            "industry": member_rows[0]["industry"],
            "aliases": sorted({r["account_name"] for r in member_rows}),
            "source_ids": [r["account_id"] for r in member_rows],
        })
    return canonical


def resolve_contacts(rows: list[dict]) -> list[dict]:
    """Cluster contact rows on lowercase email; fall back to per-row identity."""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("email") or f"__noemail__{r['contact_id']}").lower()
        by_key[key].append(r)

    canonical = []
    for k, (key, members) in enumerate(sorted(by_key.items()), 1):
        display = max((m["full_name"] for m in members), key=len)  # prefer full spelling
        canonical.append({
            "canonical_id": f"PLNR-{k:04d}",
            "full_name": display,
            "email": members[0].get("email"),
            "title": members[0].get("title"),
            "agency_id": next((m["agency_id"] for m in members if m.get("agency_id")), None),
            "source_ids": [m["contact_id"] for m in members],
        })
    return canonical


def resolution_report(raw_accounts: list[dict], canonical_accounts: list[dict],
                      raw_contacts: list[dict], canonical_contacts: list[dict]) -> str:
    merged = [c for c in canonical_accounts if len(c["source_ids"]) > 1]
    lines = [
        f"accounts: {len(raw_accounts)} source rows -> {len(canonical_accounts)} canonical "
        f"({len(merged)} merged clusters)",
        f"contacts: {len(raw_contacts)} source rows -> {len(canonical_contacts)} canonical",
        "",
        "sample merges:",
    ]
    for c in merged[:6]:
        lines.append(f"  {c['canonical_name']:<22} <- {c['aliases']}")
    return "\n".join(lines)
