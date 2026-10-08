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

Time-slicing: pass `as_of` (an ISO date) to restrict the context to events
dated strictly before it, so a backtest that replays a historical RFP is never
grounded on bookings that hadn't happened yet. Only event-level facts are
sliced; entity resolution and planner->agency employment are the graph's
current snapshot.
"""

from neo4j import Driver

from nexusvenue.rag.embed import get_embedder
from nexusvenue.graph.schema import get_driver

# The date filter runs after the vector index returns its top matches, so a
# time-sliced query over-fetches to still come back with k usable hits.
SLICE_OVERFETCH_FACTOR = 8
SLICE_OVERFETCH_MIN = 200

VECTOR_QUERY = """
CALL db.index.vector.queryNodes('beo_notes_vec', $fetch_k, $embedding)
YIELD node AS b, score
WHERE $as_of IS NULL OR b.event_date < $as_of
MATCH (a:Account)-[:EXECUTED]->(b)-[:AT_PROPERTY]->(v:Property)
OPTIONAL MATCH (b)-[:BOOKED_BY]->(p:Planner)
OPTIONAL MATCH (p)-[:EMPLOYED_BY]->(g:Agency)
RETURN score, b.id AS beo_id, b.event_type AS event_type, b.event_date AS event_date,
       b.attendee_count AS attendees, b.fb_spend AS fb_spend, b.av_spend AS av_spend,
       b.total_revenue AS total_revenue, b.ops_notes AS ops_notes,
       a.name AS account, a.id AS account_id, v.name AS property,
       p.name AS planner, g.name AS agency
ORDER BY score DESC
LIMIT $k
"""

ACCOUNT_PORTFOLIO = """
MATCH (a:Account {id: $account_id})-[:EXECUTED]->(b:BEO)-[:AT_PROPERTY]->(v:Property)
WHERE $as_of IS NULL OR b.event_date < $as_of
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

# Time-sliced variant. REPRESENTS is inferred from *all* bookings, so a slice
# can't trust it: an agency would "represent" an account it only booked for
# after the cutoff. Rebuild the represented set from bookings before as_of.
AGENCY_BOOK_AS_OF = """
MATCH (g:Agency {name: $agency})<-[:EMPLOYED_BY]-(:Planner)<-[:BOOKED_BY]-(rb:BEO)<-[:EXECUTED]-(a:Account)
WHERE rb.event_date < $as_of
WITH g, collect(DISTINCT a) AS accounts
UNWIND accounts AS a
OPTIONAL MATCH (a)-[:EXECUTED]->(b:BEO)
WHERE b.event_date < $as_of
RETURN g.name AS agency, collect(DISTINCT a.name) AS represented_accounts,
       count(b) AS total_events, round(sum(b.total_revenue), 2) AS total_revenue
"""


def retrieve(query_text: str, k: int = 6, driver: Driver | None = None,
             as_of: str | None = None) -> dict:
    """Hybrid retrieval. With `as_of` set (ISO date, e.g. "2025-03-01") only
    events dated strictly before it are returned, in the hits and in the
    portfolio / agency context alike."""
    own = driver is None
    driver = driver or get_driver()
    embedder = get_embedder()
    qvec = embedder.embed([query_text], task="RETRIEVAL_QUERY")[0]

    fetch_k = k if as_of is None else max(k * SLICE_OVERFETCH_FACTOR, SLICE_OVERFETCH_MIN)
    agency_query = AGENCY_BOOK if as_of is None else AGENCY_BOOK_AS_OF

    with driver.session() as s:
        hits = s.run(VECTOR_QUERY, k=k, fetch_k=fetch_k, embedding=qvec, as_of=as_of).data()

        account_ids = {h["account_id"] for h in hits if h["account_id"]}
        portfolios = [
            s.run(ACCOUNT_PORTFOLIO, account_id=aid, as_of=as_of).single().data()
            for aid in sorted(account_ids)
        ]

        agencies = sorted({h["agency"] for h in hits if h["agency"]})
        agency_books = [s.run(agency_query, agency=g, as_of=as_of).single().data()
                        for g in agencies]

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
