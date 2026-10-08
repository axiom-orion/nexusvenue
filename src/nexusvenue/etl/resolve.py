"""Entity resolution across property-level CRM silos.

Accounts: the same corporation appears under different legal-name variants at
each property ("Calder & Voss" / "Calder & Voss LLP" / "CALDER & VOSS ADVISORY LLP"),
and sometimes as an abbreviation no name matcher can connect ("LMK" for
"Luminark"). Two kinds of evidence, in order of trust:

1. Names (strong). Normalize legal suffixes and punctuation, then fuzzy-cluster
   with RapidFuzz using union-find, producing one canonical account node per
   real-world entity with full provenance back to the source rows.

2. Contacts (corroborating). The people filed under an account usually share
   one corporate email domain. After the name pass, whole clusters whose
   dominant contact domain AND industry agree are merged. This recovers
   abbreviations and short forms, and it is built to fail safe:
   - names always outrank it: only clusters are compared, never single rows
     that already matched by name;
   - a cluster's domain is the strict-majority domain of its members, so one
     stray contact (a consultant, a mis-filed record) cannot drag two
     companies together;
   - free-mail providers never count, and the industry must also agree.

Contacts: resolved primarily on email (exact, case-insensitive), which
collapses spelling drift like "Sarah Mitchell" vs "S. Mitchell".
"""

import re
from collections import Counter, defaultdict

from rapidfuzz import fuzz

LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llp|llc|plc|corp(oration)?|co(mpany)?|ltd|holdings|"
    r"group|companies|international|federal services|mutual)\b\.?",
    re.IGNORECASE,
)
PUNCT = re.compile(r"[^\w\s]")
WS = re.compile(r"\s+")

FUZZ_THRESHOLD = 87

# Addresses at these providers say nothing about which company a person works for.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com",
})


def normalize_account_name(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = LEGAL_SUFFIXES.sub(" ", s)
    s = PUNCT.sub(" ", s)
    return WS.sub(" ", s).strip()


def email_domain(email: str | None) -> str | None:
    """Corporate domain of an email address; None for blanks and free-mail
    providers. Subdomains are kept as written (mail.acme.com != acme.com), which
    errs on the side of not merging."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return None if not domain or domain in FREE_MAIL_DOMAINS else domain


def _dominant(values) -> str | None:
    """The value held by a strict majority of the non-null values, else None."""
    present = [v for v in values if v]
    if not present:
        return None
    value, n = Counter(present).most_common(1)[0]
    return value if n * 2 > len(present) else None


def _industry_key(industry: str | None) -> str | None:
    return (industry or "").strip().lower() or None


def account_domains(account_rows: list[dict], contact_rows: list[dict]) -> dict[str, str | None]:
    """account_id -> the dominant corporate email domain of the contacts filed
    under it (None when it has none, or they disagree)."""
    by_account: dict[str, list] = defaultdict(list)
    for c in contact_rows:
        if c.get("account_id"):
            by_account[c["account_id"]].append(email_domain(c.get("email")))
    return {a["account_id"]: _dominant(by_account.get(a["account_id"], [])) for a in account_rows}


def choose_display_name(names: list[str]) -> str:
    """The name a canonical account is shown under: the variant most similar to
    the others (so an abbreviation like "LMK" never wins), then not ALL-CAPS,
    then shortest, then alphabetical. Depends only on the set of variants, so a
    full rebuild and an incremental sync always agree."""
    unique = sorted(set(names))
    if len(unique) == 1:
        return unique[0]
    norms = {n: normalize_account_name(n) for n in unique}

    def centrality(n: str) -> float:
        return sum(fuzz.token_set_ratio(norms[n], norms[m]) for m in unique if m != n)

    return min(unique, key=lambda n: (-centrality(n), n.isupper(), len(n), n))


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


def resolve_accounts(rows: list[dict], contacts: list[dict] | None = None) -> list[dict]:
    """Cluster raw account rows into canonical accounts.

    With `contacts`, clusters that share a dominant corporate email domain and
    an industry are merged as well (see the module docstring); without, only
    names are used.

    Returns a list of canonical accounts:
      {canonical_id, canonical_name, industry, domain, aliases, source_ids}
    """
    norms = [normalize_account_name(r["account_name"]) for r in rows]
    uf = _UnionFind(len(rows))
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if norms[i] == norms[j] or fuzz.token_sort_ratio(norms[i], norms[j]) >= FUZZ_THRESHOLD:
                uf.union(i, j)

    row_domain = account_domains(rows, contacts) if contacts else {}
    if contacts:
        members_by_root: dict[int, list[int]] = defaultdict(list)
        for i in range(len(rows)):
            members_by_root[uf.find(i)].append(i)
        same_company: dict[tuple, list[int]] = defaultdict(list)
        for root, members in members_by_root.items():
            domain = _dominant(row_domain.get(rows[i]["account_id"]) for i in members)
            industry = _dominant(_industry_key(rows[i].get("industry")) for i in members)
            if domain and industry:
                same_company[(industry, domain)].append(root)
        for roots in same_company.values():
            for other in roots[1:]:
                uf.union(roots[0], other)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        clusters[uf.find(i)].append(i)

    canonical = []
    for k, members in enumerate(sorted(clusters.values(), key=lambda m: min(m)), 1):
        member_rows = [rows[i] for i in members]
        aliases = sorted({r["account_name"] for r in member_rows})
        canonical.append({
            "canonical_id": f"ACCT-{k:04d}",
            "canonical_name": choose_display_name(aliases),
            "industry": member_rows[0]["industry"],
            "domain": _dominant(row_domain.get(r["account_id"]) for r in member_rows),
            "aliases": aliases,
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


def match_account(name: str, existing: list[dict], domain: str | None = None,
                  industry: str | None = None) -> str | None:
    """Incremental ER: match one raw account against canonical accounts already
    in the graph. `existing` rows need {id, aliases} and may carry {domain,
    industry}. Same rules as the batch path, so full-load and sync agree on
    identity: a name match wins outright; otherwise the account's contact
    domain + industry may identify exactly one existing canonical (an ambiguous
    match is no match)."""
    norm = normalize_account_name(name)
    for e in existing:
        for alias in e["aliases"]:
            alias_norm = normalize_account_name(alias)
            if norm == alias_norm or fuzz.token_sort_ratio(norm, alias_norm) >= FUZZ_THRESHOLD:
                return e["id"]
    if domain and _industry_key(industry):
        hits = [e["id"] for e in existing
                if e.get("domain") == domain and _industry_key(e.get("industry")) == _industry_key(industry)]
        if len(hits) == 1:
            return hits[0]
    return None


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
