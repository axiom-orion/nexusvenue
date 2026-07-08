"""Hybrid GraphRAG retrieval: vector search + graph traversal.

1. Vector search: embed the incoming RFP text, query the BEO ops-notes vector
   index for semantically similar past events.
2. Graph traversal: expand each hit through the relationship structure -
   which account executed it, which planner booked it, which agency that
   planner works for, what else that account and agency have done with us,
   and portfolio-wide spend for the account.

The result is a structured subgraph context the advisor LLM can ground in -
facts a flat vector store cannot surface (cross-property relationships,
agency intermediation, account-level spend history).
"""

from neo4j import Driver

from nexusvenue.rag.embed import get_embedder
from nexusvenue.graph.schema import get_driver

VECTOR_QUERY = """
CALL db.index.vector.queryNodes('beo_notes_vec', $k, $embedding)
YIELD node AS b, score
MATCH (a:Account)-[:EXECUTED]->(b)-[:AT_PROPERTY]->(v:Property)
OPTIONAL MATCH (b)-[:BOOKED_BY]->(p:Planner)
OPTIONAL MATCH (p)-[:EMPLOYED_BY]->(g:Agency)
RETURN score, b.id AS beo_id, b.event_type AS event_type, b.event_date AS event_date,
       b.attendee_count AS attendees, b.fb_spend AS fb_spend, b.av_spend AS av_spend,
       b.total_revenue AS total_revenue, b.ops_notes AS ops_notes,
       a.name AS account, a.id AS account_id, v.name AS property,
       p.name AS planner, g.name AS agency
ORDER BY score DESC
"""

ACCOUNT_PORTFOLIO = """
MATCH (a:Account {id: $account_id})-[:EXECUTED]->(b:BEO)-[:AT_PROPERTY]->(v:Property)
RETURN a.name AS account, a.aliases AS aliases,
       count(b) AS events, round(sum(b.total_revenue), 2) AS lifetime_revenue,
       round(avg(b.fb_spend), 2) AS avg_fb_spend,
       collect(DISTINCT v.name) AS properties
"""

AGENCY_BOOK = """
MATCH (g:Agency {name: $agency})-[:REPRESENTS]->(a:Account)
OPTIONAL MATCH (a)-[:EXECUTED]->(b:BEO)
RETURN g.name AS agency, collect(DISTINCT a.name) AS represented_accounts,
       count(b) AS total_events, round(sum(b.total_revenue), 2) AS total_revenue
"""


def retrieve(query_text: str, k: int = 6, driver: Driver | None = None) -> dict:
    own = driver is None
    driver = driver or get_driver()
    embedder = get_embedder()
    qvec = embedder.embed([query_text], task="RETRIEVAL_QUERY")[0]

    with driver.session() as s:
        hits = s.run(VECTOR_QUERY, k=k, embedding=qvec).data()

        account_ids = {h["account_id"] for h in hits if h["account_id"]}
        portfolios = [
            s.run(ACCOUNT_PORTFOLIO, account_id=aid).single().data()
            for aid in sorted(account_ids)
        ]

        agencies = sorted({h["agency"] for h in hits if h["agency"]})
        agency_books = [s.run(AGENCY_BOOK, agency=g).single().data() for g in agencies]

    if own:
        driver.close()

    return {
        "query": query_text,
        "similar_past_events": [
            {kk: vv for kk, vv in h.items() if kk != "account_id"} for h in hits
        ],
        "account_portfolios": portfolios,
        "agency_relationships": agency_books,
    }
