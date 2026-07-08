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


def _user_prompt(blueprint: dict, context: dict) -> str:
    return (
        "## Retrieved knowledge-graph context (ground truth)\n"
        + json.dumps(context, indent=2, default=str)
        + "\n\n## Win Strategy Blueprint to grade\n"
        + json.dumps(blueprint, indent=2, default=str)
    )


PROVIDERS = ("anthropic", "gemini", "grok")


def judge(blueprint: dict, context: dict, provider: str | None = None) -> JudgeVerdict:
    """Grade a blueprint. provider: "anthropic" (default), "gemini", or "grok".

    Judging with a different model family than the generator (Claude) controls
    for self-preference bias — a judge tends to grade its own family's output
    more favorably. Run every family via `nexusvenue judge-agreement` for a
    cross-family agreement signal.
    """
    provider = provider or settings.judge_provider
    if provider == "anthropic":
        return _judge_anthropic(blueprint, context)
    if provider == "gemini":
        return _judge_gemini(blueprint, context)
    if provider == "grok":
        return _judge_grok(blueprint, context)
    raise ValueError(f"unknown judge provider {provider!r} (use one of {PROVIDERS})")


def _judge_anthropic(blueprint: dict, context: dict) -> JudgeVerdict:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=settings.judge_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": _user_prompt(blueprint, context)}],
        output_format=JudgeVerdict,
    )
    verdict = response.parsed_output
    if verdict is None:
        raise RuntimeError(f"Judge returned unparseable output (stop_reason={response.stop_reason})")
    return verdict


def _judge_gemini(blueprint: dict, context: dict) -> JudgeVerdict:
    """Cross-family judge on Google's Gemini. Reuses the embeddings key; the
    google-genai SDK takes the Pydantic model directly as the response schema."""
    from google import genai
    from google.genai import types

    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not set - add it to .env (get one at "
                           "https://aistudio.google.com/apikey) or use another judge provider")

    client = genai.Client(api_key=settings.gemini_api_key)
    resp = client.models.generate_content(
        model=settings.gemini_judge_model,
        contents=_user_prompt(blueprint, context),
        config=types.GenerateContentConfig(
            system_instruction=JUDGE_SYSTEM,
            response_mime_type="application/json",
            response_schema=JudgeVerdict,
        ),
    )
    if isinstance(resp.parsed, JudgeVerdict):
        return resp.parsed
    return JudgeVerdict.model_validate_json(resp.text)


def _strict_schema(schema: dict) -> dict:
    """Adapt a Pydantic JSON schema for xAI strict structured outputs: every
    object gets additionalProperties=false with all properties required, and
    numeric range keywords (unsupported in strict mode) are dropped — Pydantic
    still enforces ge/le client-side when the response is validated."""
    import copy

    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            for kw in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
                node.pop(kw, None)
            if "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


def _judge_grok(blueprint: dict, context: dict) -> JudgeVerdict:
    """Cross-family judge on xAI's Grok, called over plain HTTP (httpx ships
    with the anthropic SDK — no extra dependency, and no OpenAI shim)."""
    import httpx

    if not settings.xai_api_key:
        raise RuntimeError("XAI_API_KEY not set - add it to .env (get one at https://console.x.ai) "
                           "or use JUDGE_PROVIDER=anthropic")

    resp = httpx.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.xai_api_key}"},
        json={
            "model": settings.grok_model,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": _user_prompt(blueprint, context)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "judge_verdict",
                    "strict": True,
                    "schema": _strict_schema(JudgeVerdict.model_json_schema()),
                },
            },
        },
        timeout=180.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return JudgeVerdict.model_validate_json(content)
