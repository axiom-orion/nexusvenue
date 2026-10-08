"""Load resolved entities into Neo4j as a knowledge graph.

Graph model:
  (:Planner)-[:EMPLOYED_BY]->(:Agency)
  (:Agency)-[:REPRESENTS]->(:Account)          # inferred: agency planner booked for account
  (:Planner)-[:MANAGES]->(:RFP)
  (:RFP)-[:FROM_ACCOUNT]->(:Account)
  (:RFP)-[:FOR_PROPERTY]->(:Property)
  (:Account)-[:EXECUTED]->(:BEO)
  (:BEO)-[:AT_PROPERTY]->(:Property)
  (:BEO)-[:BOOKED_BY]->(:Planner)

Two entry points:
  load() — full rebuild: wipe, batch entity resolution, bulk MERGE, set the
           sync watermark to the max last_modified seen.
  sync() — incremental: extract rows at/after the watermark (inclusive, with
           a stored boundary-id guard so late same-stamp rows aren't lost and
           re-runs stay no-ops), resolve NEW entities against canonical nodes
           already in the graph (same normalize/fuzzy criteria as the batch
           path), upsert, re-infer REPRESENTS, advance the watermark.
           Idempotent — a second run with no source changes is a no-op.
"""

from neo4j import Driver

from nexusvenue.etl.extract import extract
from nexusvenue.etl.resolve import (
    match_account,
    resolve_accounts,
    resolve_contacts,
    resolution_report,
)
from nexusvenue.graph.schema import apply_schema, get_driver

PROPERTY_NAMES = {
    "ORL": ("NexusVenue Grand Orlando", "Orlando, FL"),
    "MIA": ("NexusVenue Miami Beachfront", "Miami, FL"),
    "CHI": ("NexusVenue Chicago Riverside", "Chicago, IL"),
}

ACCOUNT_UPSERT = """
UNWIND $rows AS r
MERGE (a:Account {id: r.canonical_id})
SET a.name = r.canonical_name, a.industry = r.industry,
    a.aliases = r.aliases, a.source_ids = r.source_ids
"""

PLANNER_UPSERT = """
UNWIND $rows AS r
MERGE (p:Planner {id: r.canonical_id})
SET p.name = r.full_name, p.email = r.email, p.title = r.title,
    p.source_ids = r.source_ids
WITH p, r WHERE r.agency_id IS NOT NULL
MATCH (g:Agency {id: r.agency_id})
MERGE (p)-[:EMPLOYED_BY]->(g)
"""

# Edited text must drop its stale vector: the CASE compares the node's previous
# text with the incoming row (in its own SET, before the text is overwritten) so
# embed_graph(missing_only=True) re-embeds changed notes instead of keeping the
# old vector forever.
BEO_UPSERT = """
UNWIND $rows AS r
MERGE (b:BEO {id: r.beo_id})
SET b.embedding = CASE
        WHEN b.ops_notes IS NULL OR b.ops_notes = r.ops_notes THEN b.embedding
        ELSE null END
SET b.event_type = r.event_type, b.event_date = r.event_date,
    b.attendee_count = r.attendee_count, b.room_block = r.room_block,
    b.fb_spend = r.fb_spend, b.av_spend = r.av_spend,
    b.total_revenue = r.total_revenue, b.status = r.status,
    b.ops_notes = r.ops_notes
WITH b, r
MATCH (v:Property {code: r.property_code}) MERGE (b)-[:AT_PROPERTY]->(v)
WITH b, r
MATCH (a:Account {id: r.account})        MERGE (a)-[:EXECUTED]->(b)
WITH b, r WHERE r.planner IS NOT NULL
MATCH (p:Planner {id: r.planner})        MERGE (b)-[:BOOKED_BY]->(p)
"""

RFP_UPSERT = """
UNWIND $rows AS r
MERGE (q:RFP {id: r.rfp_id})
SET q.embedding = CASE
        WHEN q.raw_text IS NULL OR q.raw_text = r.raw_text THEN q.embedding
        ELSE null END
SET q.event_type = r.event_type, q.attendee_count = r.attendee_count,
    q.event_date = r.event_date, q.status = r.status, q.raw_text = r.raw_text
WITH q, r
MATCH (v:Property {code: r.property_code}) MERGE (q)-[:FOR_PROPERTY]->(v)
WITH q, r
MATCH (a:Account {id: r.account})          MERGE (q)-[:FROM_ACCOUNT]->(a)
WITH q, r WHERE r.planner IS NOT NULL
MATCH (p:Planner {id: r.planner})          MERGE (p)-[:MANAGES]->(q)
"""

# Inferred edge: an agency represents an account if one of its planners booked
# for it. Global + MERGE, so re-running after a sync is idempotent.
REPRESENTS_INFER = """
MATCH (a:Account)-[:EXECUTED]->(:BEO)-[:BOOKED_BY]->(:Planner)-[:EMPLOYED_BY]->(g:Agency)
MERGE (g)-[:REPRESENTS]->(a)
"""


def _max_modified(raw: dict[str, list[dict]]) -> str | None:
    stamps = [r["last_modified"] for rows in raw.values() for r in rows if r.get("last_modified")]
    return max(stamps) if stamps else None


TABLE_KEYS = {
    "accounts": "account_id", "agencies": "agency_id", "contacts": "contact_id",
    "rfps": "rfp_id", "beo_history": "beo_id",
}


def _boundary_ids(raw: dict[str, list[dict]], watermark: str | None) -> list[str]:
    """Keys of the rows stamped exactly at the watermark.

    Delta extraction is inclusive (>=), so a row committed late with a stamp
    equal to the watermark is still picked up. The ids stored on SyncState let
    the next sync drop the boundary rows it has already processed, keeping
    re-runs no-ops."""
    if watermark is None:
        return []
    return sorted(
        f"{t}:{r[key]}"
        for t, key in TABLE_KEYS.items()
        for r in raw.get(t, [])
        if r.get("last_modified") == watermark
    )


def _drop_boundary(raw: dict[str, list[dict]], watermark: str,
                   boundary: list[str] | None) -> dict[str, list[dict]]:
    """Remove already-processed boundary rows from an inclusive extraction.
    A graph built before boundary ids existed (boundary None) re-processes its
    boundary rows once — harmless, every upsert is idempotent."""
    seen = set(boundary or [])
    return {
        t: [r for r in rows
            if not (r.get("last_modified") == watermark
                    and f"{t}:{r[TABLE_KEYS[t]]}" in seen)]
        for t, rows in raw.items()
    }


def _merge_account(target: dict, row: dict) -> None:
    """Fold one delta account row into a canonical account (mutates `target`).

    Same rules as resolve_accounts, so a full rebuild and a sync agree: aliases
    and source ids accumulate, the shortest variant is the display name, and the
    industry is first-wins — except that a row restating an already-known source
    row (a CRM correction) updates it."""
    is_update = row["account_id"] in target["source_ids"]
    target["aliases"] = sorted(set(target["aliases"]) | {row["account_name"]})
    target["source_ids"] = sorted(set(target["source_ids"]) | {row["account_id"]})
    current = target.get("canonical_name")
    if not current or len(row["account_name"]) < len(current):
        target["canonical_name"] = row["account_name"]
    if row.get("industry") and (is_update or not target.get("industry")):
        target["industry"] = row["industry"]


def _merge_planner(target: dict, row: dict) -> None:
    """Fold one delta contact row into a canonical planner matched on email
    (mutates `target`). Same rules as resolve_contacts: the longest spelling is
    the display name, title and agency are first-wins — except that a row
    restating an already-known source row (a CRM correction) updates the title."""
    is_update = row["contact_id"] in target["source_ids"]
    target["source_ids"] = sorted(set(target["source_ids"]) | {row["contact_id"]})
    if row.get("full_name") and len(row["full_name"]) > len(target.get("name") or ""):
        target["name"] = row["full_name"]
    if row.get("title") and (is_update or not target.get("title")):
        target["title"] = row["title"]
    if row.get("agency_id") and not target.get("agency_id"):
        target["agency_id"] = row["agency_id"]


def _resolve_delta(raw: dict[str, list[dict]], existing_accounts: list[dict],
                   existing_planners: list[dict]) -> dict:
    """Incremental entity resolution for one delta batch.

    Resolves new rows against the canonical entities already in the graph — and
    against any minted earlier in the same batch, so two variants of one new
    company, or two rows for one new person, still collapse onto one node. Pure
    function (no database access) so the identity rules are unit-testable.

    existing_accounts: {id, name, industry, aliases, source_ids}
    existing_planners: {id, email, name, title, agency_id, source_ids}

    Returns the resolved state of every canonical the delta touched, shaped for
    ACCOUNT_UPSERT / PLANNER_UPSERT (so merges update names, titles and agencies
    through the same idempotent MERGE as the batch path, not just aliases and
    source ids), plus source-id -> canonical maps for edge annotation.
    """
    accounts = [
        {"canonical_id": a["id"], "canonical_name": a.get("name"), "industry": a.get("industry"),
         "aliases": list(a.get("aliases") or []), "source_ids": list(a.get("source_ids") or []),
         "existing": True, "touched": False}
        for a in existing_accounts
    ]
    taken = {a["canonical_id"] for a in accounts}
    new_seq, account_rows_merged = 1, 0
    for r in raw["accounts"]:
        hit = match_account(
            r["account_name"],
            [{"id": a["canonical_id"], "aliases": a["aliases"]} for a in accounts])
        if hit:
            target = next(a for a in accounts if a["canonical_id"] == hit)
            _merge_account(target, r)
            target["touched"] = True
            if target["existing"]:
                account_rows_merged += 1
        else:
            while f"ACCT-S{new_seq:03d}" in taken:
                new_seq += 1
            cid = f"ACCT-S{new_seq:03d}"
            taken.add(cid)
            accounts.append({
                "canonical_id": cid, "canonical_name": r["account_name"],
                "industry": r.get("industry"), "aliases": [r["account_name"]],
                "source_ids": [r["account_id"]], "existing": False, "touched": True,
            })

    planners = [
        {"id": p["id"], "email": p.get("email"), "name": p.get("name"), "title": p.get("title"),
         "agency_id": p.get("agency_id"), "source_ids": list(p.get("source_ids") or []),
         "existing": True, "touched": False}
        for p in existing_planners
    ]
    by_email = {p["email"].lower(): p for p in planners if p["email"]}
    ptaken = {p["id"] for p in planners}
    pseq, planner_rows_merged = 1, 0
    for r in raw["contacts"]:
        key = (r.get("email") or "").lower()
        target = by_email.get(key) if key else None
        if target is not None:
            _merge_planner(target, r)
            target["touched"] = True
            planner_rows_merged += 1
        else:
            while f"PLNR-S{pseq:03d}" in ptaken:
                pseq += 1
            pid = f"PLNR-S{pseq:03d}"
            ptaken.add(pid)
            minted = {
                "id": pid, "email": r.get("email"), "name": r["full_name"],
                "title": r.get("title"), "agency_id": r.get("agency_id"),
                "source_ids": [r["contact_id"]], "existing": False, "touched": True,
            }
            planners.append(minted)
            if key:
                by_email[key] = minted  # later rows for this person merge into the new node

    def planner_row(p: dict) -> dict:
        return {"canonical_id": p["id"], "full_name": p["name"], "email": p["email"],
                "title": p["title"], "agency_id": p["agency_id"], "source_ids": p["source_ids"]}

    new_accounts = [a for a in accounts if not a["existing"]]
    merged_accounts = [a for a in accounts if a["existing"] and a["touched"]]
    new_planners = [planner_row(p) for p in planners if not p["existing"]]
    merged_planners = [planner_row(p) for p in planners if p["existing"] and p["touched"]]
    return {
        "accounts": new_accounts + merged_accounts,
        "new_accounts": new_accounts,
        "merged_accounts": merged_accounts,
        "planners": new_planners + merged_planners,
        "new_planners": new_planners,
        "merged_planners": merged_planners,
        "account_rows_merged": account_rows_merged,
        "planner_rows_merged": planner_rows_merged,
        "src_to_account": {sid: a["canonical_id"] for a in accounts for sid in a["source_ids"]},
        "src_to_planner": {sid: p["id"] for p in planners for sid in p["source_ids"]},
    }


def _annotate_events(rows: list[dict], src_to_account: dict, src_to_planner: dict) -> list[dict]:
    return [
        {**r,
         "account": src_to_account.get(r["account_id"]),
         "planner": src_to_planner.get(r["contact_id"])}
        for r in rows
    ]


def _graph_stats(session) -> str:
    stats = session.run(
        "MATCH (n) WITH labels(n)[0] AS label, count(*) AS c RETURN label, c ORDER BY label"
    ).data()
    return ", ".join(f"{r['label']}={r['c']}" for r in stats)


def load(driver: Driver | None = None, verbose: bool = True) -> str:
    own = driver is None
    driver = driver or get_driver()
    apply_schema(driver)

    raw = extract()
    accounts = resolve_accounts(raw["accounts"])
    contacts = resolve_contacts(raw["contacts"])
    report = resolution_report(raw["accounts"], accounts, raw["contacts"], contacts)

    src_to_account = {sid: a["canonical_id"] for a in accounts for sid in a["source_ids"]}
    src_to_planner = {sid: c["canonical_id"] for c in contacts for sid in c["source_ids"]}

    properties = [{"code": code, "name": n, "city": c} for code, (n, c) in PROPERTY_NAMES.items()]

    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")  # full rebuild wipes; sync() is the incremental path

        s.run("UNWIND $rows AS r MERGE (v:Property {code: r.code}) SET v.name = r.name, v.city = r.city",
              rows=properties)
        s.run(ACCOUNT_UPSERT, rows=accounts)
        s.run("UNWIND $rows AS r MERGE (g:Agency {id: r.agency_id}) SET g.name = r.agency_name",
              rows=raw["agencies"])
        s.run(PLANNER_UPSERT, rows=contacts)
        s.run(BEO_UPSERT, rows=_annotate_events(raw["beo_history"], src_to_account, src_to_planner))
        s.run(RFP_UPSERT, rows=_annotate_events(raw["rfps"], src_to_account, src_to_planner))
        s.run(REPRESENTS_INFER)

        watermark = _max_modified(raw)
        s.run("MERGE (w:SyncState {id: 'crm'}) SET w.watermark = $wm, w.boundary_ids = $b",
              wm=watermark, b=_boundary_ids(raw, watermark))

        stat_line = _graph_stats(s)

    if own:
        driver.close()

    return f"{report}\n\ngraph loaded: {stat_line}\nwatermark: {watermark}"


def sync(driver: Driver | None = None) -> str:
    """Incremental load: only source rows modified since the stored watermark."""
    own = driver is None
    driver = driver or get_driver()

    with driver.session() as s:
        rec = s.run("MATCH (w:SyncState {id: 'crm'}) "
                    "RETURN w.watermark AS wm, w.boundary_ids AS boundary").single()
        if rec is None or rec["wm"] is None:
            if own:
                driver.close()
            return "no watermark found - run `nexusvenue etl` (full load) first"
        watermark = rec["wm"]

        raw_all = extract(since=watermark)  # inclusive (>=): see extract()
        raw = _drop_boundary(raw_all, watermark, rec["boundary"])
        changed = {t: len(rows) for t, rows in raw.items() if rows}
        if not changed:
            if own:
                driver.close()
            return f"up to date (watermark {watermark}, no source changes)"

        # --- incremental entity resolution against the live graph ---
        existing_accounts = s.run(
            "MATCH (a:Account) RETURN a.id AS id, a.name AS name, a.industry AS industry, "
            "a.aliases AS aliases, a.source_ids AS source_ids"
        ).data()
        existing_planners = s.run(
            "MATCH (p:Planner) OPTIONAL MATCH (p)-[:EMPLOYED_BY]->(g:Agency) "
            "RETURN p.id AS id, p.email AS email, p.name AS name, p.title AS title, "
            "       p.source_ids AS source_ids, head(collect(g.id)) AS agency_id"
        ).data()
        plan = _resolve_delta(raw, existing_accounts, existing_planners)

        # --- upserts (no wipe) ---
        # New and merged canonicals go through the same idempotent MERGE queries
        # as the batch path, so a merge updates names, titles and agencies and
        # nothing is silently dropped.
        if plan["accounts"]:
            s.run(ACCOUNT_UPSERT, rows=plan["accounts"])
        if raw["agencies"]:
            s.run("UNWIND $rows AS r MERGE (g:Agency {id: r.agency_id}) SET g.name = r.agency_name",
                  rows=raw["agencies"])
        if plan["planners"]:
            s.run(PLANNER_UPSERT, rows=plan["planners"])
        if raw["beo_history"]:
            s.run(BEO_UPSERT, rows=_annotate_events(
                raw["beo_history"], plan["src_to_account"], plan["src_to_planner"]))
        if raw["rfps"]:
            s.run(RFP_UPSERT, rows=_annotate_events(
                raw["rfps"], plan["src_to_account"], plan["src_to_planner"]))
        s.run(REPRESENTS_INFER)

        new_watermark = _max_modified(raw)
        # The boundary comes from the unfiltered extraction: it covers every row
        # at the new watermark, including the old boundary rows when it hasn't moved.
        s.run("MATCH (w:SyncState {id: 'crm'}) SET w.watermark = $wm, w.boundary_ids = $b",
              wm=new_watermark, b=_boundary_ids(raw_all, new_watermark))
        stat_line = _graph_stats(s)

    if own:
        driver.close()

    lines = [
        f"delta rows since {watermark}: " + ", ".join(f"{t}={n}" for t, n in changed.items()),
        f"accounts: {plan['account_rows_merged']} merged into existing canonicals, "
        f"{len(plan['new_accounts'])} new",
        f"planners: {plan['planner_rows_merged']} merged on email, {len(plan['new_planners'])} new",
    ]
    for a in plan["merged_accounts"]:
        lines.append(f"  merged -> {a['canonical_id']}: aliases now {a['aliases']}")
    lines += [f"graph now: {stat_line}", f"watermark advanced: {watermark} -> {new_watermark}"]
    return "\n".join(lines)
