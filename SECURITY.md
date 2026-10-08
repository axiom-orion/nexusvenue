# Security policy

## Supported versions

Only the `main` branch is supported. There are no maintained release branches; fixes land on `main`.

## Reporting a vulnerability

Please report security issues privately, not in public issues or pull requests:

- Use GitHub's private vulnerability reporting on this repository (**Security → Report a vulnerability**), or
- Email **security@vorion.org**.

Include what you found, how to reproduce it, and the impact you expect. We aim to reply within 7 days.

## Scope

NexusVenue is a local CLI pipeline: a mock CRM in SQLite, an ETL into Neo4j, GraphRAG retrieval, and an LLM advisor and judge (Anthropic, with Gemini embeddings and optional Gemini/Grok judges). It runs no web server of its own. All CRM data in the repo is synthetic. Relevant reports include:

- **API keys and credentials** — anything that leaks `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY` or the Neo4j password (for example into logs, error output, generated files or advisor output), or a key committed to the repo. Keys belong in `.env`, which is git-ignored.
- **Prompt injection** — BEO ops-notes and other CRM text are retrieved into the advisor and judge prompts. Crafted record text that makes the advisor ignore its instructions, invent citations, or skew the judge's grades is in scope.
- **Cypher and SQL injection** — user or CRM input reaching a Neo4j or SQLite query without parameterisation.
- **File writes** — the pipeline writes the mock CRM database (`data/crm.db`) and the gold set (`data/goldset.json`); anything that makes it write elsewhere.

Note: `docker-compose.yml` is for local development. It publishes Neo4j on ports 7474 and 7687 with a default password (`nexusvenue`) unless `NEO4J_PASSWORD` is set. Don't expose it to a network you don't trust, and set your own password.

Out of scope: the third-party model providers and Neo4j itself.
