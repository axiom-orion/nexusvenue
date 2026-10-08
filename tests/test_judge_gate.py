"""The judge's pass/fail gate: numeric thresholds are enforced in code; the model
keeps only the call on whether a *listed* hallucination is material."""

import pytest

from nexusvenue.evals.judge import JudgeVerdict, _finalize

HALLUCINATION = {"claim": "five mocktail events", "reason": "context documents four"}


def verdict(**kw) -> JudgeVerdict:
    base = dict(context_precision=0.95, citation_validity=1.0, hallucinations=[],
                actionability_score=4, reasoning="r", passed=False)
    base.update(kw)
    return JudgeVerdict.model_validate(base)


@pytest.mark.parametrize("fields, model_said, expected", [
    # clean verdict, the model lied low -> the gate passes it
    (dict(), False, True),
    # model asserts a pass but a numeric threshold fails -> cannot pass
    (dict(context_precision=0.85), True, False),
    (dict(actionability_score=2), True, False),
    # exactly on the thresholds passes
    (dict(context_precision=0.9, actionability_score=3), False, True),
    # hallucination listed, numbers fine -> the model's materiality call stands
    (dict(hallucinations=[HALLUCINATION]), True, True),      # judged immaterial
    (dict(hallucinations=[HALLUCINATION]), False, False),    # judged material
    # listed hallucination does not rescue failing numbers
    (dict(hallucinations=[HALLUCINATION], context_precision=0.5), True, False),
])
def test_gate(fields, model_said, expected):
    assert _finalize(verdict(passed=model_said, **fields)).passed is expected
