"""Showcase queries: the SQL-to-Cypher translation story, runnable.

Each entry pairs the legacy relational query with its graph equivalent, so the
README claim ("multi-JOIN pain becomes one traversal") is demonstrable against
live data, not just prose. See docs/sql-vs-cypher.md for the narrative.
"""

from neo4j import Driver

from nexusvenue.graph.schema import get_driver

SHOWCASE = [
    {
        "name": "agency_warm_paths",
        "question": (
            "Which open RFPs are managed by an agency planner whose agency has "
            "already delivered a $50k+ F&B event for ANY account we host?"
        ),
        "sql": """
SELECT r.rfp_id, c.full_name, ag.agency_name, a2.account_name, b.fb_spend
FROM rfps r
JOIN contacts c        ON r.contact_id = c.contact_id
JOIN agencies ag       ON c.agency_id = ag.agency_id
JOIN contacts c2       ON c2.agency_id = ag.agency_id
JOIN beo_history b     ON b.contact_id = c2.contact_id
JOIN accounts a2       ON b.account_id = a2.account_id
WHERE r.status IN ('Open', 'Proposal Sent', 'Negotiating')
  AND b.status = 'Executed' AND b.fb_spend > 50000;
-- 5 JOINs, and still blind to cross-property name variants
        """.strip(),
        "cypher": """
MATCH (q:RFP)<-[:MANAGES]-(p:Planner)-[:EMPLOYED_BY]->(g:Agency),
      (g)-[:REPRESENTS]->(a:Account)-[:EXECUTED]->(b:BEO)
WHERE q.status IN ['Open', 'Proposal Sent', 'Negotiating'] AND b.fb_spend > 50000
RETURN q.id AS rfp, p.name AS planner, g.name AS agency,
       a.name AS proven_account, round(b.fb_spend, 2) AS fb_spend
ORDER BY fb_spend DESC LIMIT 10
        """.strip(),
    },
    {
        "name": "cross_property_whales",
        "question": (
            "Which corporate accounts have spent across MULTIPLE properties? "
            "(invisible in siloed CRMs where each property has its own name variant)"
        ),
        "sql": """
-- Effectively impossible without entity resolution: 'Deloitte' (ORL),
-- 'Deloitte LLP' (MIA) and 'DELOITTE & TOUCHE LLP' (CHI) are three
-- unrelated account rows. GROUP BY account_name silently splits them.
        """.strip(),
        "cypher": """
MATCH (a:Account)-[:EXECUTED]->(b:BEO)-[:AT_PROPERTY]->(v:Property)
WITH a, collect(DISTINCT v.code) AS props, sum(b.total_revenue) AS revenue
WHERE size(props) > 1
RETURN a.name AS account, a.aliases AS crm_name_variants, props,
       round(revenue, 2) AS lifetime_revenue
ORDER BY revenue DESC LIMIT 10
        """.strip(),
    },
    {
        "name": "planner_influence",
        "question": "Which individual planners control the most booked revenue across the portfolio?",
        "sql": """
SELECT c.full_name, SUM(b.total_revenue) AS controlled
FROM beo_history b JOIN contacts c ON b.contact_id = c.contact_id
GROUP BY c.full_name;
-- Splits 'Sarah Mitchell' and 'S. Mitchell' into two people
        """.strip(),
        "cypher": """
MATCH (p:Planner)<-[:BOOKED_BY]-(b:BEO)
OPTIONAL MATCH (p)-[:EMPLOYED_BY]->(g:Agency)
RETURN p.name AS planner, g.name AS agency, count(b) AS events,
       round(sum(b.total_revenue), 2) AS controlled_revenue
ORDER BY controlled_revenue DESC LIMIT 10
        """.strip(),
    },
]


def run_showcase(driver: Driver | None = None) -> list[dict]:
    own = driver is None
    driver = driver or get_driver()
    results = []
    with driver.session() as s:
        for item in SHOWCASE:
            rows = s.run(item["cypher"]).data()
            results.append({**item, "rows": rows})
    if own:
        driver.close()
    return results
