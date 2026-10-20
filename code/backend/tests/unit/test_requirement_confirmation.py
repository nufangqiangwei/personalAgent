"""Requirement confirmation rules (T042).

Covers explicit "no preference", source separation, defaults, missing/conflict/
inapplicable cases, the rule that accepting a suggestion is not execution
confirmation, and the H1/H2/H3 summary shape.
"""

from __future__ import annotations

import pytest

from personal_agent.adapters.rendering.requirement_summary import render_summary
from personal_agent.domain.common import AppError
from personal_agent.domain.requirements import (
    DraftStatus,
    FieldSource,
    FieldValue,
    apply_analysis,
    assert_confirmable,
    assert_editable,
    assert_grantable,
    derive_status,
    invalidate_confirmation,
    missing_fields,
    required_paths,
)


def field(path: str, value: object, source: FieldSource = FieldSource.USER) -> FieldValue:
    return FieldValue(path=path, value=value, source=source)


def test_required_paths_come_from_the_workflow_input_schema() -> None:
    schema = {"type": "object", "required": ["topic", "audience"]}
    assert required_paths(schema) == ["topic", "audience"]
    assert required_paths({}) == []


def test_missing_fields_and_explicit_no_preference() -> None:
    fields = {
        "topic": field("topic", "图书馆夜间开放"),
        # explicit preference_order = null is a real value, not a missing field.
        "preference_order": FieldValue(
            path="preference_order", value=None, source=FieldSource.USER, explicit_none=True
        ),
    }
    assert missing_fields(["topic", "preference_order", "audience"], fields) == ["audience"]


def test_default_values_do_not_satisfy_a_required_null() -> None:
    fields = {"topic": field("topic", None, FieldSource.DEFAULT)}
    assert missing_fields(["topic"], fields) == ["topic"]


def test_status_derivation_and_confirmed_state() -> None:
    fields = {"topic": field("topic", "x")}
    assert derive_status(required=["topic"], fields=fields, conflicts=[], confirmed=False) is (
        DraftStatus.READY
    )
    assert derive_status(required=["topic"], fields={}, conflicts=[], confirmed=False) is (
        DraftStatus.COLLECTING
    )
    conflict_status = derive_status(
        required=["topic"], fields={}, conflicts=[{"path": "topic"}], confirmed=False
    )
    assert conflict_status is (
        DraftStatus.COLLECTING
    )
    assert derive_status(required=["topic"], fields=fields, conflicts=[], confirmed=True) is (
        DraftStatus.CONFIRMED
    )


def test_only_user_sourced_analysis_values_enter_the_draft() -> None:
    fields, suggestions = apply_analysis(
        fields={},
        analysis_fields=[
            {"path": "topic", "value": "夜间开放", "source": "user"},
            {"path": "audience", "value": "城市居民", "source": "suggestion"},
        ],
        message_id="message-1",
        required=["topic"],
    )
    assert fields["topic"].message_id == "message-1"
    assert "audience" not in fields
    assert suggestions == [
        {"field_path": "audience", "value": "城市居民", "rationale": ""}
    ]


def test_editing_a_confirmed_draft_invalidates_the_confirmation() -> None:
    assert invalidate_confirmation(DraftStatus.CONFIRMED) is DraftStatus.STALE
    assert invalidate_confirmation(DraftStatus.READY) is DraftStatus.READY


def test_confirmation_requires_complete_consistent_input() -> None:
    with pytest.raises(AppError) as missing:
        assert_confirmable(status=DraftStatus.READY, missing=["topic"], conflicts=[])
    assert missing.value.code == "INPUT_INCOMPLETE"

    with pytest.raises(AppError) as conflict:
        assert_confirmable(
            status=DraftStatus.READY,
            missing=[],
            conflicts=[{"path": "cost", "message": "两个值冲突"}],
        )
    assert conflict.value.code == "INPUT_INCONSISTENT"

    with pytest.raises(AppError):
        assert_confirmable(status=DraftStatus.CONFIRMED, missing=[], conflicts=[])

    assert_confirmable(status=DraftStatus.STALE, missing=[], conflicts=[])


def test_editable_and_grantable_states() -> None:
    assert_editable(DraftStatus.COLLECTING)
    assert_editable(DraftStatus.STALE)
    with pytest.raises(AppError):
        assert_editable(DraftStatus.CONFIRMED)

    assert_grantable(DraftStatus.READY)
    with pytest.raises(AppError):
        assert_grantable(DraftStatus.CONFIRMED)


def test_output_spec_is_validated_before_execution() -> None:
    from personal_agent.application.requirement_confirmation import RequirementService

    RequirementService._validate_output_spec({
        "format": "markdown",
        "structure": "comparison",
        "language": "en",
        "detail": "brief",
    })
    with pytest.raises(AppError) as invalid:
        RequirementService._validate_output_spec({"structure": "slides"})
    assert invalid.value.code == "OUTPUT_SPEC_INVALID"
    with pytest.raises(AppError) as unknown:
        RequirementService._validate_output_spec({"paper_size": "A4"})
    assert unknown.value.code == "OUTPUT_SPEC_INVALID"


def test_summary_has_three_sections_and_lists_items() -> None:
    markdown = render_summary(
        workflow_name="技术选型对比",
        workflow_version_no=2,
        fields={
            "topic": {"value": "AI 编程助手", "source": "user"},
            "hard_constraints": {
                "value": ["私有化部署", "每人每月 40 元"],
                "source": "user",
            },
        },
        missing=["audience"],
        conflicts=[],
        preference_order=["hard_constraints"],
    )
    assert markdown.startswith("# 技术选型对比")
    assert "## 一、研究要求" in markdown
    assert "## 二、优先顺序" in markdown
    assert "## 三、确认与缺口" in markdown
    assert "AI 编程助手" in markdown
    assert "私有化部署" in markdown
    assert "仍缺少：使用对象" in markdown
    assert "流程版本：v2" in markdown


def test_summary_does_not_invent_absent_sections() -> None:
    markdown = render_summary(
        workflow_name="公开主题资料研究",
        workflow_version_no=1,
        fields={},
        missing=[],
        conflicts=[],
        preference_order=[],
    )
    assert "尚未收集到要求" in markdown
    assert "无特别偏好" in markdown
    assert "必要信息已齐全" in markdown
    assert "使用对象" not in markdown


def test_summary_marks_defaults_and_conflicts() -> None:
    markdown = render_summary(
        workflow_name="技术选型对比",
        workflow_version_no=1,
        fields={"audience": {"value": "个人开发者", "source": "default"}},
        missing=[],
        conflicts=[{"path": "cost", "message": "两个来源冲突"}],
        preference_order=[],
    )
    assert "（默认值，待确认）" in markdown
    assert "冲突：cost — 两个来源冲突" in markdown
