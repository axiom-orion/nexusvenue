# NexusVenue — GraphRAG Sales Intelligence for Enterprise Hospitality

A working, end-to-end **CRM → knowledge graph → GraphRAG → LLM-as-a-judge**
pipeline, grounded in a domain where relational CRMs have a structural blind
spot: enterprise B2B hospitality sales.

In corporate event sales (conventions, incentive trips, banquet buyouts), the
signal that wins deals is *relational* — a third-party planner's agency has a
track record with a sister property; a corporate account spends across three
cities under three different legal names. Property-siloed CRMs store those
facts as disconnected rows. This project rebuilds them as a graph and puts a
grounded, evaluated LLM advisor on top.

The domain model is deliberately Salesforce-shaped: hotel group sales runs on
Amadeus Delphi, which is built natively on the Salesforce platform — Accounts,
Contacts, and custom Booking/BEO objects, each carrying a `SystemModstamp`.
The mock CRM mirrors that structure (including a `last_modified` stamp on
every row), so the ETL patterns here — batched `MERGE` upserts, watermark-based
incremental sync, cross-org entity resolution — are the ones a real
Salesforce-to-graph pipeline needs.

```
[Messy multi-property CRM (SQLite)]
        │  extract
        ▼
[Python ETL + fuzzy entity resolution]      "Deloitte" = "Deloitte LLP" = "DELOITTE & TOUCHE LLP"
        │  load (Cypher MERGE, batched UNWIND)
        ▼
[Neo4j knowledge graph]                     Accounts / Planners / Agencies / RFPs / BEOs / Properties
        │  embed (Gemini 1536-dim → vector indexes)
        ▼
[Hybrid GraphRAG retrieval]                 vector search over BEO ops-notes + graph traversal expansion
        │  synthesize (Claude, structured outputs)
        ▼
[Win Strategy Blueprint]                    evidence-cited JSON, every claim traceable to a beo_id
        │  grade
        ▼
[Evaluation]                                LLM-as-a-judge (precision / hallucinations / actionability)
                                            + deterministic retrieval precision/recall@k vs a seeded gold set
```

## What each stage demonstrates

| Stage | Code | What it shows |
|---|---|---|
| Mock CRM generator | `mockdata/generate.py` | Realistic dirty data: per-property name variants, contact spelling drift, unstructured BEO ops-notes |
| ETL + entity resolution | `etl/` | RapidFuzz + union-find clustering with provenance; email-keyed contact dedupe |
| Graph modeling | `etl/load.py`, `graph/schema.py` | Relational → graph schema pivot, constraints, batched `UNWIND`/`MERGE` loads, vector indexes |
| Incremental sync | `etl/load.py` (`sync`) | Watermark on a `SyncState` node, delta extraction on `last_modified`, incremental ER against the live graph, new-nodes-only embedding — idempotent |
| SQL → Cypher | `graph/queries.py`, [docs/sql-vs-cypher.md](docs/sql-vs-cypher.md) | Side-by-side legacy SQL and optimized Cypher, runnable against live data |
| GraphRAG retrieval | `rag/retrieve.py` | Vector search anchors + traversal expansion (account portfolios, agency warm paths) |
| Advisor | `rag/advisor.py` | Claude structured outputs (`messages.parse` + Pydantic) — guaranteed-valid, citation-carrying JSON |
| Evaluation | `evals/` | Reference-less LLM-as-a-judge with an adversarial rubric **and** deterministic precision/recall@k against a seeded gold set |

## Quickstart

```bash
# 1. Neo4j
docker compose up -d

# 2. Install
python -m venv .venv && .venv/Scripts/activate     # Windows (source .venv/bin/activate on unix)
pip install -e .

# 3. Configure
cp .env.example .env                                # add ANTHROPIC_API_KEY / GEMINI_API_KEY

# 4. Build the graph and score retrieval (no LLM keys needed with EMBED_BACKEND=hash)
nexusvenue demo

# 5. The full GraphRAG loop (needs keys)
nexusvenue ask "3-day leadership summit for 150 executives. High-end AV, a heavy focus on botanical citrus custom mocktail receptions, and seamless VIP transport." --with-judge
```

Browse the graph at http://localhost:7474 (`neo4j` / `nexusvenue`).

### Commands

| Command | What it does |
|---|---|
| `nexusvenue generate` | Write the messy CRM (SQLite) + retrieval gold set |
| `nexusvenue etl` | Extract → entity-resolve → load Neo4j (prints merge report) |
| `nexusvenue delta` | Simulate a business day of CRM changes (new/updated rows) |
| `nexusvenue sync` | Incremental sync past the watermark; embeds new nodes only. Run it twice — the second run is a no-op |
| `nexusvenue embed` | Embed BEO notes + RFP text onto nodes (Gemini or offline hash backend) |
| `nexusvenue search "<query>"` | Retrieval only — inspect the subgraph context |
| `nexusvenue ask "<rfp>" [--with-judge] [--judge-provider grok]` | Full GraphRAG → Win Strategy Blueprint (→ judge verdict) |
| `nexusvenue judge-agreement "<rfp>"` | Grade one blueprint with BOTH judge families (Claude + Grok) and report cross-family agreement |
| `nexusvenue showcase` | Run the SQL-vs-Cypher comparison queries live |
| `nexusvenue eval-retrieval` | Precision/recall@k against the gold set |
| `nexusvenue demo` | generate → etl → embed → eval in one shot |

## Design decisions worth asking me about

- **Why hybrid retrieval, not just vector search?** Vector search finds
  *similar events*; only traversal finds *warm paths* — the agency that
  booked a $200k gala at a sister property is invisible to cosine similarity
  over one document. The retriever returns both (`rag/retrieve.py`).
- **Why entity resolution before graph load?** Cross-property intelligence is
  the whole value proposition; without ER, "Deloitte" is three small
  accounts instead of one whale. Resolution keeps full provenance
  (`aliases`, `source_ids`) on the canonical node.
- **Why is incremental sync harder than the full load?** Entity resolution.
  A full rebuild clusters all rows at once; a delta row must be resolved
  against *canonical nodes already in the graph* using the same
  normalize/fuzzy criteria, or the two paths disagree on identity and the
  graph forks ("Accenture Incorporated" must land on the existing Accenture
  node, not mint a duplicate). `sync()` also re-infers derived edges
  (`REPRESENTS`) and advances the watermark only after a successful upsert.
- **Why a cross-family judge?** LLM-as-a-judge has a documented failure mode:
  self-preference bias — a judge grades output from its own model family more
  favorably. The generator here is Claude, so the judge can run on xAI's Grok
  (`--judge-provider grok`), and `judge-agreement` runs both families on the
  same blueprint and diffs the verdicts — a cheap judge-calibration signal
  (`evals/judge.py`). Grok is called over plain HTTP with strict JSON-schema
  output; the verdict validates against the same Pydantic model either way.
- **Why a seeded gold set *and* an LLM judge?** They measure different
  failure modes. Precision/recall@k catches retrieval regressions
  deterministically in CI; the judge catches generation failures
  (hallucinated revenue, invalid citations) that no retrieval metric sees.
- **Why an offline `hash` embedding backend?** The whole pipeline — vector
  indexes, retrieval, eval plumbing — runs in CI with zero API keys.
  Token-overlap similarity is enough to smoke-test wiring; `gemini` gives
  real semantics.
- **Why structured outputs instead of "please return JSON"?** `messages.parse`
  with Pydantic schemas makes the blueprint and the judge verdict
  guaranteed-parseable, so downstream automation (CRM writeback, dashboards)
  never string-munges model output.

## API keys

Everything except `ask` / `judge-agreement` / real embeddings runs with **zero
keys** (`EMBED_BACKEND=hash`). For the full loop, add to `.env`:

| Env var | Powers | Get one at |
|---|---|---|
| `ANTHROPIC_API_KEY` | Advisor blueprint + default judge | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) |
| `GEMINI_API_KEY` | Real semantic embeddings (`EMBED_BACKEND=gemini`) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `XAI_API_KEY` | Optional cross-family Grok judge | [console.x.ai](https://console.x.ai) → API Keys |

## Stack

Python 3.11+ · Neo4j 5 (vector indexes) · Anthropic Claude (`claude-opus-4-8`,
structured outputs + adaptive thinking) · Gemini embeddings
(`gemini-embedding-001`, 1536-dim) · xAI Grok (`grok-4`, cross-family judge) ·
RapidFuzz · Pydantic v2 · Click
