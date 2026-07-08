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
def delta():
    """Simulate a business day of CRM changes (new/updated rows, later timestamps)."""
    from nexusvenue.mockdata.generate import mutate_delta
    click.echo(mutate_delta())


@cli.command()
def sync():
    """Incremental sync: load only rows past the watermark, resolve new entities
    against the live graph, embed only new nodes. Idempotent."""
    from nexusvenue.etl.load import sync as run_sync
    from nexusvenue.rag.embed import embed_graph
    report = run_sync()
    click.echo(report)
    if "up to date" not in report and "no watermark" not in report:
        click.echo(f"embedded (new nodes only): {embed_graph(missing_only=True)}")


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
@click.option("--judge-provider", type=click.Choice(["anthropic", "grok"]), default=None,
              help="Judge model family (default: JUDGE_PROVIDER env, then anthropic). "
                   "grok = cross-family judging, controls for self-preference bias.")
def ask(rfp_text, k, with_judge, judge_provider):
    """Full GraphRAG: retrieve subgraph, generate a Win Strategy Blueprint (Claude)."""
    from nexusvenue.rag.advisor import advise
    blueprint, context = advise(rfp_text, k=k)
    click.echo(blueprint.model_dump_json(indent=2))
    if with_judge or judge_provider:
        from nexusvenue.evals.judge import judge
        click.echo(f"\n--- judge verdict ({judge_provider or 'default'}) ---")
        verdict = judge(blueprint.model_dump(), context, provider=judge_provider)
        click.echo(verdict.model_dump_json(indent=2))


@cli.command("judge-agreement")
@click.argument("rfp_text")
@click.option("-k", default=6)
def judge_agreement(rfp_text, k):
    """Generate one blueprint, grade it with BOTH judge families (Claude + Grok),
    and report cross-family agreement — a cheap judge-calibration signal."""
    from nexusvenue.evals.judge import judge
    from nexusvenue.rag.advisor import advise

    blueprint, context = advise(rfp_text, k=k)
    click.echo(blueprint.model_dump_json(indent=2))

    verdicts = {}
    for provider in ("anthropic", "grok"):
        click.echo(f"\n--- {provider} verdict ---")
        verdicts[provider] = judge(blueprint.model_dump(), context, provider=provider)
        click.echo(verdicts[provider].model_dump_json(indent=2))

    a, g = verdicts["anthropic"], verdicts["grok"]
    click.echo("\n=== cross-family agreement ===")
    click.echo(f"{'metric':<22}{'anthropic':>12}{'grok':>12}{'delta':>10}")
    for label, av, gv in [
        ("context_precision", a.context_precision, g.context_precision),
        ("citation_validity", a.citation_validity, g.citation_validity),
        ("actionability", a.actionability_score, g.actionability_score),
        ("hallucinations", len(a.hallucinations), len(g.hallucinations)),
    ]:
        click.echo(f"{label:<22}{av:>12}{gv:>12}{round(abs(av - gv), 3):>10}")
    click.echo(f"{'passed':<22}{str(a.passed):>12}{str(g.passed):>12}"
               f"{'AGREE' if a.passed == g.passed else 'DISAGREE':>10}")


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
