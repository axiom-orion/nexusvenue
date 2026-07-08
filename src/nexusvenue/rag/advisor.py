"""Win Strategy Advisor — Claude synthesizes retrieved graph context into a
structured, evidence-cited sales blueprint for an incoming RFP.

Uses the Anthropic structured-outputs path (messages.parse + Pydantic) so the
blueprint is guaranteed-valid JSON, and adaptive thinking for reasoning depth.
"""

import json

import anthropic
from pydantic import BaseModel, Field

from nexusvenue.config import settings
from nexusvenue.rag.retrieve import retrieve

SYSTEM = """You are a strategic sales advisor for NexusVenue, a multi-property
enterprise hospitality group. You receive an incoming RFP and a retrieved
knowledge-graph context: semantically similar past events (BEOs with
operational notes and spend), account-level portfolio history, and agency
relationships.

Produce a Win Strategy Blueprint for the sales team. Hard rules:
- Every factual claim about past events, spend, or relationships MUST be
  grounded in the provided context and cite the supporting beo_id(s).
- Never invent revenue figures, event history, contacts, or relationships
  that are not in the context.
- If the context is thin, say so and scope your recommendations accordingly.
- Recommendations must be specific and executable by a sales manager today."""


class HistoricalEvidence(BaseModel):
    beo_id: str = Field(description="The BEO id from the retrieved context that supports this insight")
    insight: str = Field(description="What this past event tells us that is relevant to winning the RFP")


class PackageRecommendation(BaseModel):
    name: str
    description: str
    pricing_guidance: str = Field(description="Directional pricing guidance grounded in historical spend from the context")
    supporting_beo_ids: list[str]


class KeyContact(BaseModel):
    name: str
    role: str = Field(description="e.g. planner, agency, or account relationship")
    why: str


class WinStrategyBlueprint(BaseModel):
    executive_summary: str
    win_probability_factors: list[str] = Field(description="Factors from the graph context that raise or lower win likelihood")
    recommended_packages: list[PackageRecommendation]
    historical_evidence: list[HistoricalEvidence]
    key_contacts: list[KeyContact]
    operational_risks: list[str]
    next_steps: list[str] = Field(description="Concrete actions for the sales team, in priority order")


def advise(rfp_text: str, k: int = 6, context: dict | None = None) -> tuple[WinStrategyBlueprint, dict]:
    """Run retrieval (unless context supplied) and synthesize the blueprint."""
    context = context or retrieve(rfp_text, k=k)
    client = anthropic.Anthropic()

    response = client.messages.parse(
        model=settings.anthropic_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                "## Incoming RFP\n" + rfp_text +
                "\n\n## Retrieved knowledge-graph context\n" +
                json.dumps(context, indent=2, default=str)
            ),
        }],
        output_format=WinStrategyBlueprint,
    )
    blueprint = response.parsed_output
    if blueprint is None:
        raise RuntimeError(f"Advisor returned unparseable output (stop_reason={response.stop_reason})")
    return blueprint, context
