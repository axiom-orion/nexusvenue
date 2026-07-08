"""LLM-as-a-judge: grades a Win Strategy Blueprint against the retrieved
subgraph it was generated from.

Reference-less evaluation — the judge never sees a "correct" blueprint. It
scores the output against a rubric using only the retrieved context as ground
truth, the same guardrail posture as production: an insight a sales team acts
on must be traceable to real graph facts, not hallucinated history.

Rubric dimensions:
- context_precision: are cited beo_ids real, and do the claims match them?
- hallucinations: enumerated claims NOT supported by the retrieved context.
- actionability: are next steps concrete and executable, not generic advice?
"""

import json

import anthropic
from pydantic import BaseModel, Field

from nexusvenue.config import settings

JUDGE_SYSTEM = """You are a strict evaluation judge for a GraphRAG sales
advisory system. You receive (1) the retrieved knowledge-graph context that
was given to the advisor model and (2) the Win Strategy Blueprint it produced.

Grade the blueprint against the context ONLY. The context is the sole source
of truth: any factual claim about past events, revenue, spend, contacts,
agencies, or relationships that cannot be verified in the context is a
hallucination — even if it sounds plausible.

Be adversarial: actively try to find unsupported claims, fabricated numbers,
cited beo_ids that don't exist in the context, and misattributed facts.
General sales tactics phrased as suggestions (not facts) are acceptable and
are not hallucinations."""


class Hallucination(BaseModel):
    claim: str = Field(description="The unsupported claim, quoted or closely paraphrased")
    reason: str = Field(description="Why the retrieved context does not support it")


class JudgeVerdict(BaseModel):
    context_precision: float = Field(ge=0, le=1, description="Fraction of factual claims fully supported by the context")
    citation_validity: float = Field(ge=0, le=1, description="Fraction of cited beo_ids that exist in the context and match the claim")
    hallucinations: list[Hallucination]
    actionability_score: int = Field(ge=1, le=5, description="1=generic advice, 5=every next step is concrete and executable")
    reasoning: str = Field(description="Brief grading rationale")
    passed: bool = Field(description="True only if zero material hallucinations AND context_precision >= 0.9 AND actionability_score >= 3")


def judge(blueprint: dict, context: dict) -> JudgeVerdict:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=settings.judge_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                "## Retrieved knowledge-graph context (ground truth)\n"
                + json.dumps(context, indent=2, default=str)
                + "\n\n## Win Strategy Blueprint to grade\n"
                + json.dumps(blueprint, indent=2, default=str)
            ),
        }],
        output_format=JudgeVerdict,
    )
    verdict = response.parsed_output
    if verdict is None:
        raise RuntimeError(f"Judge returned unparseable output (stop_reason={response.stop_reason})")
    return verdict
