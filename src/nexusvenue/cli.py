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
@click.option("--dry-run", is_flag=True, help="Parse the venue CSVs and report the graph without touching Neo4j.")
def venues(dry_run):
    """Load the BAI venue/market graph: enriched properties, the OVERFLOW network,
    Tier-2 rooms, and the market housing tiers a citywide fills."""
    if dry_run:
        from nexusvenue.etl.venue_intel import parse_venue_intel, _summary
        d = parse_venue_intel()
        click.echo(_summary(d))
        click.echo("\nsample properties: " + ", ".join(
            f"{p['code']}({p['archetype']},{p['event_sqft']}sqft)" for p in d["properties"][:5]))
        click.echo("sample overflow: " + ", ".join(
            f"{e['a']}-[{e['type']}]->{e['b']}" for e in d["edges"][:5]))
        return
    from nexusvenue.etl.venue_intel import load_venue_intel
    click.echo(load_venue_intel())


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
@click.option("--judge-provider", type=click.Choice(["anthropic", "gemini", "grok"]), default=None,
              help="Judge model family (default: JUDGE_PROVIDER env, then anthropic). "
                   "gemini/grok = cross-family judging, controls for self-preference bias.")
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
    """Generate one blueprint, grade it with every judge family that has a key
    (Claude + Gemini + Grok), and report cross-family agreement — a cheap
    judge-calibration signal."""
    from nexusvenue.config import settings
    from nexusvenue.evals.judge import judge
    from nexusvenue.rag.advisor import advise

    providers = ["anthropic"]
    if settings.gemini_api_key:
        providers.append("gemini")
    else:
        click.echo("skipping gemini judge (GEMINI_API_KEY not set)")
    if settings.xai_api_key:
        providers.append("grok")
    else:
        click.echo("skipping grok judge (XAI_API_KEY not set)")
    if len(providers) == 1:
        click.echo("note: only the anthropic judge is available - agreement needs >=2 families")

    blueprint, context = advise(rfp_text, k=k)
    click.echo(blueprint.model_dump_json(indent=2))

    verdicts = {}
    for provider in providers:
        click.echo(f"\n--- {provider} verdict ---")
        verdicts[provider] = judge(blueprint.model_dump(), context, provider=provider)
        click.echo(verdicts[provider].model_dump_json(indent=2))

    click.echo("\n=== cross-family agreement ===")
    click.echo(f"{'metric':<20}" + "".join(f"{p:>12}" for p in providers) + f"{'spread':>10}")
    for label, get in [
        ("context_precision", lambda v: v.context_precision),
        ("citation_validity", lambda v: v.citation_validity),
        ("actionability", lambda v: v.actionability_score),
        ("hallucinations", lambda v: len(v.hallucinations)),
    ]:
        vals = [get(verdicts[p]) for p in providers]
        click.echo(f"{label:<20}" + "".join(f"{v:>12}" for v in vals)
                   + f"{round(max(vals) - min(vals), 3):>10}")
    passes = [verdicts[p].passed for p in providers]
    click.echo(f"{'passed':<20}" + "".join(f"{str(x):>12}" for x in passes)
               + f"{'AGREE' if len(set(passes)) == 1 else 'DISAGREE':>10}")


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
