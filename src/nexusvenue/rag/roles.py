"""Role/Concern GraphRAG retrieval — the altitude/lane knowledge layer.

Same two-step hybrid pattern as rag/retrieve.py's sales intelligence:
1. Vector search: embed the incoming query, match it against Concern.description
   (a first-time planner asking "will my event actually turn out right" should
   vector-match "not-living-up-to-vision" even without using that phrase).
2. Graph traversal: expand each matched role through ESCALATES_TO/HANDS_OFF_TO/
   ROUTES_THROUGH to ground the fuzzy match in exact structural facts -- who
   this role reports to, what triggers a handoff and to whom, whether they're
   external and route through someone else. This is what a flat vector store
   over a "worries" document could never give a caller: a hit tells you WHAT
   the concern is; the traversal tells you WHAT TO DO about it.
"""

from neo4j import Driver

from nexusvenue.graph.schema import get_driver
from nexusvenue.rag.embed import get_embedder

CONCERN_VECTOR_QUERY = """
CALL db.index.vector.queryNodes('concern_vec', $k, $embedding)
YIELD node AS c, score
MATCH (role:Role)-[w:WORRIES_ABOUT]->(c)
RETURN score, c.id AS concern_id, c.description AS concern,
       role.id AS role_id, role.title AS role_title, role.altitude AS altitude,
       role.lane AS lane, role.is_internal AS is_internal, w.rank AS rank
ORDER BY score DESC, w.rank ASC
"""

ROLE_STRUCTURE = """
MATCH (role:Role {id: $role_id})
OPTIONAL MATCH (role)-[:ESCALATES_TO]->(up:Role)
OPTIONAL MATCH (role)-[h:HANDS_OFF_TO]->(down:Role)
OPTIONAL MATCH (role)-[:ROUTES_THROUGH]->(via:Role)
RETURN role.id AS id, role.title AS title, role.altitude AS altitude, role.lane AS lane,
       role.is_internal AS is_internal,
       [x IN collect(DISTINCT up.title) WHERE x IS NOT NULL] AS escalates_to,
       [x IN collect(DISTINCT {to: down.title, trigger: h.trigger, worry: h.worry})
        WHERE x.to IS NOT NULL] AS hands_off_to,
       [x IN collect(DISTINCT via.title) WHERE x IS NOT NULL] AS routes_through
"""


def retrieve_role_context(query_text: str, k: int = 5, driver: Driver | None = None) -> dict:
    own = driver is None
    driver = driver or get_driver()
    embedder = get_embedder()
    qvec = embedder.embed([query_text], task="RETRIEVAL_QUERY")[0]

    with driver.session() as s:
        hits = s.run(CONCERN_VECTOR_QUERY, k=k, embedding=qvec).data()
        role_ids = sorted({h["role_id"] for h in hits})
        structures = [s.run(ROLE_STRUCTURE, role_id=rid).single().data() for rid in role_ids]

    if own:
        driver.close()

    return {
        "query": query_text,
        "matched_concerns": hits,
        "role_structure": structures,
    }
