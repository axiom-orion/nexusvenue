"""NexusVenue CLI — the full pipeline as composable commands.

  generate -> etl -> embed -> ask / showcase / eval-retrieval / eval-judge
"""

import json

import click


@click.group()
def cli():
    """GraphRAG sales intelligence for enterprise B2B hospitality."""


@cli.command()
def generate():
    """Generate the messy multi-property CRM (SQLite) + retrieval gold set."""
    from nexusvenue.mockdata.generate import generate as gen
    counts = gen()
    click.echo(f"wrote crm.db: {counts}")
    click.echo("wrote goldset.json")


@cli.command()
def etl():
    """Extract -> entity-resolve -> load the knowledge graph into Neo4j."""
    from nexusvenue.etl.load import load
    click.echo(load())


@cli.command()
def embed():
    """Embed BEO ops-notes and RFP text onto graph nodes (Gemini or hash backend)."""
    from nexusvenue.config import settings
    from nexusvenue.rag.embed import embed_graph
    click.echo(f"backend={settings.embed_backend} dim={settings.embed_dim}")
    click.echo(f"embedded: {embed_graph()}")


@cli.command()
@click.argument("query")
@click.option("-k", default=6, help="Top-k similar past events to retrieve.")
def search(query, k):
    """Hybrid retrieval only (no LLM): show the subgraph context for a query."""
    from nexusvenue.rag.retrieve import retrieve
    click.echo(json.dumps(retrieve(query, k=k), indent=2, default=str))


@cli.command()
@click.argument("rfp_text")
@click.option("-k", default=6, help="Top-k similar past events to ground on.")
@click.option("--with-judge", is_flag=True, help="Also grade the blueprint with the LLM judge.")
def ask(rfp_text, k, with_judge):
    """Full GraphRAG: retrieve subgraph, generate a Win Strategy Blueprint (Claude)."""
    from nexusvenue.rag.advisor import advise
    blueprint, context = advise(rfp_text, k=k)
    click.echo(blueprint.model_dump_json(indent=2))
    if with_judge:
        from nexusvenue.evals.judge import judge
        click.echo("\n--- judge verdict ---")
        verdict = judge(blueprint.model_dump(), context)
        click.echo(verdict.model_dump_json(indent=2))


@cli.command()
def showcase():
    """Run the SQL-vs-Cypher showcase queries against the live graph."""
    from nexusvenue.graph.queries import run_showcase
    for item in run_showcase():
        click.echo(f"\n=== {item['name']} ===")
        click.echo(item["question"])
        click.echo("\n[SQL — legacy relational approach]")
        click.echo(item["sql"])
        click.echo("\n[Cypher — graph approach]")
        click.echo(item["cypher"])
        click.echo("\n[live results]")
        click.echo(json.dumps(item["rows"], indent=2, default=str))


@cli.command("eval-retrieval")
@click.option("-k", default=6)
def eval_retrieval(k):
    """Deterministic precision/recall@k against the seeded gold set."""
    from nexusvenue.evals.metrics import evaluate_retrieval
    click.echo(json.dumps(evaluate_retrieval(k=k), indent=2))


@cli.command()
def demo():
    """End-to-end: generate -> etl -> embed -> retrieval eval."""
    from nexusvenue.mockdata.generate import generate as gen
    from nexusvenue.etl.load import load
    from nexusvenue.rag.embed import embed_graph
    from nexusvenue.evals.metrics import evaluate_retrieval

    click.echo(f"[1/4] generate: {gen()}")
    click.echo(f"[2/4] etl:\n{load()}")
    click.echo(f"[3/4] embed: {embed_graph()}")
    click.echo(f"[4/4] retrieval eval:\n{json.dumps(evaluate_retrieval(), indent=2)}")


if __name__ == "__main__":
    cli()
