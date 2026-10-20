"""Analysis output contract and grounding (T122)."""

from __future__ import annotations

import pytest

from personal_agent.application.requirement_analysis import (
    RequirementAnalysisService,
    _grounded,
)


def test_a_value_must_be_grounded_in_the_user_text() -> None:
    text = "我们团队 12 人，预算每人每月 40 元，需要私有化部署"
    assert _grounded("私有化部署", text)
    assert _grounded(12, text)
    assert _grounded(["私有化部署", "40"], text)
    assert not _grounded("公有云", text)
    assert not _grounded("", text)


def test_analysis_payload_contract_is_strict() -> None:
    valid = {
        "summary": "s",
        "fields": [{"path": "topic", "value": "x", "source": "user"}],
        "suggestions": [{"field_path": "audience", "value": "team", "rationale": "r"}],
        "clarifying_questions": ["q"],
        "conflicts": [],
    }
    assert RequirementAnalysisService._validated_payload(valid) is valid

    with pytest.raises(ValueError):
        RequirementAnalysisService._validated_payload({"fields": "not-a-list"})
    with pytest.raises(ValueError):
        RequirementAnalysisService._validated_payload({
            "fields": [{"value": "x"}],
        })
    with pytest.raises(ValueError):
        RequirementAnalysisService._validated_payload({
            "fields": [{"path": "x", "value": "y", "source": "model"}],
        })
    with pytest.raises(ValueError):
        RequirementAnalysisService._validated_payload({
            "suggestions": [{"value": 1}],
        })
