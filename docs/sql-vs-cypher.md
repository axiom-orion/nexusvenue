# From SQL to Cypher: why this domain wants a graph

The B2B hospitality sales domain looks tabular — accounts, contacts, RFPs,
banquet event orders — but the *questions that win deals* are relationship
questions:

> "This new RFP came in through a third-party planner. Has her **agency**
> ever delivered a big event for **any** account we host, at **any** of our
> properties?"

That is a warm-path question. Answering it in a relational CRM means chaining
JOINs through junction tables; answering it in a graph means describing the
path you're looking for.

## The same question, twice

**SQL (5 JOINs, property-siloed):**

```sql
SELECT r.rfp_id, c.full_name, ag.agency_name, a2.account_name, b.fb_spend
FROM rfps r
JOIN contacts c        ON r.contact_id = c.contact_id
JOIN agencies ag       ON c.agency_id = ag.agency_id
JOIN contacts c2       ON c2.agency_id = ag.agency_id
JOIN beo_history b     ON b.contact_id = c2.contact_id
JOIN accounts a2       ON b.account_id = a2.account_id
WHERE r.status IN ('Open', 'Proposal Sent', 'Negotiating')
  AND b.status = 'Executed' AND b.fb_spend > 50000;
```

**Cypher (one path pattern):**

```cypher
MATCH (q:RFP)<-[:MANAGES]-(p:Planner)-[:EMPLOYED_BY]->(g:Agency),
      (g)-[:REPRESENTS]->(a:Account)-[:EXECUTED]->(b:BEO)
WHERE q.status IN ['Open', 'Proposal Sent', 'Negotiating'] AND b.fb_spend > 50000
RETURN q.id, p.name, g.name, a.name, b.fb_spend
ORDER BY b.fb_spend DESC
```

The Cypher isn't just shorter — it's the *shape of the question*. A sales
manager can read it. Adding one more hop ("...and that account's VP used to
work somewhere that booked with us") is one more relationship in the pattern,
not two more JOINs and a subquery.

## Translation cheat sheet

| Relational concept | Graph concept |
|---|---|
| Row in `accounts` | `(:Account)` node |
| Foreign key `contact.account_id` | `(:Planner)-[:WORKS_FOR]->(:Account)` relationship |
| Junction table (`account_agencies`) | Direct relationship `(:Agency)-[:REPRESENTS]->(:Account)` |
| Multi-table JOIN chain | Path pattern `()-[]->()-[]->()` |
| `GROUP BY` + aggregate | `WITH`/`RETURN` + aggregate over matched paths |
| Recursive CTE (org charts, referral chains) | Variable-length path `-[:REPORTS_TO*1..5]->` |
| Index on FK column | Traversal is index-free adjacency — pointer-chasing, no join cost |

## What the graph does that SQL structurally can't (without ER)

The mock CRM in this repo simulates three property-level Salesforce orgs.
"Deloitte", "Deloitte LLP" and "DELOITTE & TOUCHE LLP" are three unrelated
rows in three silos. `GROUP BY account_name` splits the customer into three
smaller, less important-looking customers.

The ETL pipeline (`src/nexusvenue/etl/resolve.py`) fuzzy-clusters those rows
into one canonical `(:Account)` node with `aliases` provenance — so the
`cross_property_whales` query (run `nexusvenue showcase`) surfaces
portfolio-level whales that no per-property CRM report can see.

## Query-tuning notes

- Anchor patterns on the most selective label/property; uniqueness
  constraints (see `graph/schema.py`) double as lookup indexes.
- Prefer explicit relationship directions — undirected patterns double the
  traversal work.
- Use `PROFILE` to check db hits; a well-anchored traversal touches only the
  neighborhood of the anchor, not the whole table like a JOIN scan.
- Vector search + traversal compose: `db.index.vector.queryNodes` yields
  anchor nodes, then plain `MATCH` expands context around them — that
  composition is the heart of GraphRAG (`rag/retrieve.py`).
