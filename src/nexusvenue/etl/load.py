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
"""

from neo4j import Driver

from nexusvenue.etl.extract import extract
from nexusvenue.etl.resolve import resolve_accounts, resolve_contacts, resolution_report
from nexusvenue.graph.schema import apply_schema, get_driver

PROPERTY_NAMES = {
    "ORL": ("NexusVenue Grand Orlando", "Orlando, FL"),
    "MIA": ("NexusVenue Miami Beachfront", "Miami, FL"),
    "CHI": ("NexusVenue Chicago Riverside", "Chicago, IL"),
}


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
        s.run("MATCH (n) DETACH DELETE n")  # idempotent rebuild for demo purposes

        s.run("UNWIND $rows AS r MERGE (v:Property {code: r.code}) SET v.name = r.name, v.city = r.city",
              rows=properties)

        s.run("""
            UNWIND $rows AS r
            MERGE (a:Account {id: r.canonical_id})
            SET a.name = r.canonical_name, a.industry = r.industry,
                a.aliases = r.aliases, a.source_ids = r.source_ids
        """, rows=accounts)

        s.run("UNWIND $rows AS r MERGE (g:Agency {id: r.agency_id}) SET g.name = r.agency_name",
              rows=raw["agencies"])

        s.run("""
            UNWIND $rows AS r
            MERGE (p:Planner {id: r.canonical_id})
            SET p.name = r.full_name, p.email = r.email, p.title = r.title,
                p.source_ids = r.source_ids
            WITH p, r WHERE r.agency_id IS NOT NULL
            MATCH (g:Agency {id: r.agency_id})
            MERGE (p)-[:EMPLOYED_BY]->(g)
        """, rows=contacts)

        beos = [
            {**b,
             "account": src_to_account.get(b["account_id"]),
             "planner": src_to_planner.get(b["contact_id"])}
            for b in raw["beo_history"]
        ]
        s.run("""
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
        """, rows=beos)

        rfps = [
            {**r,
             "account": src_to_account.get(r["account_id"]),
             "planner": src_to_planner.get(r["contact_id"])}
            for r in raw["rfps"]
        ]
        s.run("""
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
        """, rows=rfps)

        # Infer agency->account representation from agency planners' bookings.
        s.run("""
            MATCH (a:Account)-[:EXECUTED]->(:BEO)-[:BOOKED_BY]->(:Planner)-[:EMPLOYED_BY]->(g:Agency)
            MERGE (g)-[:REPRESENTS]->(a)
        """)

        stats = s.run("""
            MATCH (n) WITH labels(n)[0] AS label, count(*) AS c
            RETURN label, c ORDER BY label
        """).data()

    if own:
        driver.close()

    stat_line = ", ".join(f"{r['label']}={r['c']}" for r in stats)
    return f"{report}\n\ngraph loaded: {stat_line}"
