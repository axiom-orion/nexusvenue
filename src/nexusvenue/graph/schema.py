"""Neo4j schema: uniqueness constraints and vector indexes."""

from neo4j import Driver, GraphDatabase

from nexusvenue.config import settings

CONSTRAINTS = [
    "CREATE CONSTRAINT account_id IF NOT EXISTS FOR (a:Account) REQUIRE a.id IS UNIQUE",
    "CREATE CONSTRAINT planner_id IF NOT EXISTS FOR (p:Planner) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT agency_id IF NOT EXISTS FOR (g:Agency) REQUIRE g.id IS UNIQUE",
    "CREATE CONSTRAINT property_code IF NOT EXISTS FOR (v:Property) REQUIRE v.code IS UNIQUE",
    "CREATE CONSTRAINT beo_id IF NOT EXISTS FOR (b:BEO) REQUIRE b.id IS UNIQUE",
    "CREATE CONSTRAINT rfp_id IF NOT EXISTS FOR (r:RFP) REQUIRE r.id IS UNIQUE",
]

VECTOR_INDEXES = [
    ("beo_notes_vec", "BEO", "embedding"),
    ("rfp_text_vec", "RFP", "embedding"),
]


def get_driver() -> Driver:
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def apply_schema(driver: Driver | None = None) -> None:
    own = driver is None
    driver = driver or get_driver()
    with driver.session() as s:
        for stmt in CONSTRAINTS:
            s.run(stmt)
        for name, label, prop in VECTOR_INDEXES:
            s.run(
                f"CREATE VECTOR INDEX {name} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.{prop}) "
                "OPTIONS {indexConfig: {`vector.dimensions`: $dim, `vector.similarity_function`: 'cosine'}}",
                dim=settings.embed_dim,
            )
    if own:
        driver.close()
