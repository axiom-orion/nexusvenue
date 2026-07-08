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
  sync() — incremental: extract only rows past the watermark, resolve NEW
           entities against canonical nodes already in the graph (same
           normalize/fuzzy criteria as the batch path), upsert, re-infer
           REPRESENTS, advance the watermark. Idempotent — a second run with
           no source changes is a no-op.
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

BEO_UPSERT = """
UNWIND $rows AS r
MERGE (b:BEO {id: r.beo_id})
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
        s.run("MERGE (w:SyncState {id: 'crm'}) SET w.watermark = $wm", wm=watermark)

        stat_line = _graph_stats(s)

    if own:
        driver.close()

    return f"{report}\n\ngraph loaded: {stat_line}\nwatermark: {watermark}"


def sync(driver: Driver | None = None) -> str:
    """Incremental load: only source rows modified after the stored watermark."""
    own = driver is None
    driver = driver or get_driver()

    with driver.session() as s:
        rec = s.run("MATCH (w:SyncState {id: 'crm'}) RETURN w.watermark AS wm").single()
        if rec is None or rec["wm"] is None:
            if own:
                driver.close()
            return "no watermark found - run `nexusvenue etl` (full load) first"
        watermark = rec["wm"]

        raw = extract(since=watermark)
        changed = {t: len(rows) for t, rows in raw.items() if rows}
        if not changed:
            if own:
                driver.close()
            return f"up to date (watermark {watermark}, no source changes)"

        # --- incremental entity resolution against the live graph ---
        existing_accounts = s.run(
            "MATCH (a:Account) RETURN a.id AS id, a.aliases AS aliases, a.source_ids AS source_ids"
        ).data()
        existing_planners = s.run(
            "MATCH (p:Planner) RETURN p.id AS id, p.email AS email, p.source_ids AS source_ids"
        ).data()
        email_to_planner = {p["email"].lower(): p for p in existing_planners if p["email"]}

        merged_accounts, new_accounts, new_seq = [], [], 1
        taken = {a["id"] for a in existing_accounts}
        for r in raw["accounts"]:
            hit = match_account(r["account_name"], existing_accounts + new_accounts_view(new_accounts))
            if hit:
                target = next(a for a in existing_accounts if a["id"] == hit) \
                    if any(a["id"] == hit for a in existing_accounts) \
                    else next(a for a in new_accounts if a["canonical_id"] == hit)
                aliases_key = "aliases"
                target[aliases_key] = sorted(set(target[aliases_key]) | {r["account_name"]})
                target["source_ids"] = sorted(set(target["source_ids"]) | {r["account_id"]})
                if any(a["id"] == hit for a in existing_accounts):
                    merged_accounts.append(target)
            else:
                while f"ACCT-S{new_seq:03d}" in taken:
                    new_seq += 1
                cid = f"ACCT-S{new_seq:03d}"
                taken.add(cid)
                new_accounts.append({
                    "canonical_id": cid, "canonical_name": r["account_name"],
                    "industry": r["industry"], "aliases": [r["account_name"]],
                    "source_ids": [r["account_id"]],
                })

        merged_planners, new_planners, pseq = [], [], 1
        ptaken = {p["id"] for p in existing_planners}
        for r in raw["contacts"]:
            key = (r.get("email") or "").lower()
            if key and key in email_to_planner:
                target = email_to_planner[key]
                target["source_ids"] = sorted(set(target["source_ids"]) | {r["contact_id"]})
                merged_planners.append(target)
            else:
                while f"PLNR-S{pseq:03d}" in ptaken:
                    pseq += 1
                pid = f"PLNR-S{pseq:03d}"
                ptaken.add(pid)
                np = {"canonical_id": pid, "full_name": r["full_name"], "email": r.get("email"),
                      "title": r.get("title"), "agency_id": r.get("agency_id"),
                      "source_ids": [r["contact_id"]]}
                new_planners.append(np)
                if key:
                    email_to_planner[key] = {"id": pid, "email": r.get("email"),
                                             "source_ids": np["source_ids"]}

        # source-id -> canonical maps spanning the whole graph + this delta
        src_to_account = {sid: a["id"] for a in existing_accounts for sid in a["source_ids"]}
        src_to_account.update({sid: a["canonical_id"] for a in new_accounts for sid in a["source_ids"]})
        src_to_planner = {sid: p["id"] for p in existing_planners for sid in p["source_ids"]}
        src_to_planner.update({sid: p["canonical_id"] for p in new_planners for sid in p["source_ids"]})

        # --- upserts (no wipe) ---
        if merged_accounts:
            s.run("UNWIND $rows AS r MATCH (a:Account {id: r.id}) "
                  "SET a.aliases = r.aliases, a.source_ids = r.source_ids", rows=merged_accounts)
        if new_accounts:
            s.run(ACCOUNT_UPSERT, rows=new_accounts)
        if raw["agencies"]:
            s.run("UNWIND $rows AS r MERGE (g:Agency {id: r.agency_id}) SET g.name = r.agency_name",
                  rows=raw["agencies"])
        if merged_planners:
            s.run("UNWIND $rows AS r MATCH (p:Planner {id: r.id}) SET p.source_ids = r.source_ids",
                  rows=merged_planners)
        if new_planners:
            s.run(PLANNER_UPSERT, rows=new_planners)
        if raw["beo_history"]:
            s.run(BEO_UPSERT, rows=_annotate_events(raw["beo_history"], src_to_account, src_to_planner))
        if raw["rfps"]:
            s.run(RFP_UPSERT, rows=_annotate_events(raw["rfps"], src_to_account, src_to_planner))
        s.run(REPRESENTS_INFER)

        new_watermark = _max_modified(raw)
        s.run("MATCH (w:SyncState {id: 'crm'}) SET w.watermark = $wm", wm=new_watermark)
        stat_line = _graph_stats(s)

    if own:
        driver.close()

    lines = [
        f"delta rows since {watermark}: " + ", ".join(f"{t}={n}" for t, n in changed.items()),
        f"accounts: {len(merged_accounts)} merged into existing canonicals, {len(new_accounts)} new",
        f"planners: {len(merged_planners)} merged on email, {len(new_planners)} new",
    ]
    for a in merged_accounts:
        lines.append(f"  merged -> {a['id']}: aliases now {a['aliases']}")
    lines += [f"graph now: {stat_line}", f"watermark advanced: {watermark} -> {new_watermark}"]
    return "\n".join(lines)


def new_accounts_view(new_accounts: list[dict]) -> list[dict]:
    """Adapt in-flight new canonical accounts to match_account's {id, aliases} shape,
    so multiple variants of the same new company within one delta still merge."""
    return [{"id": a["canonical_id"], "aliases": a["aliases"]} for a in new_accounts]
