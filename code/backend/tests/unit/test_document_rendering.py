"""Document rendering unit tests (T064).

Report, checklist and comparison structures; language and detail preferences;
partial/no-qualified labelling; safe links; and the content contract refusals
(fact without source, COMPLETE with gaps, unqualified candidate).
"""

from __future__ import annotations

import pytest

from personal_agent.adapters.rendering.markdown import render_document
from personal_agent.domain.common import AppError
from personal_agent.domain.document_content import DocumentContent, build_content

EVIDENCE = {
    "directions": [
        {
            "id": "official",
            "status": "covered",
            "findings": [
                {
                    "claim": "候选 A 支持私有化部署。",
                    "source_ids": ["s1"],
                    "classification": "FACT",
                    "validity": "VERIFIED",
                }
            ],
            "source_ids": ["s1"],
            "gaps": [],
        },
        {
            "id": "ops",
            "status": "missing",
            "findings": [],
            "source_ids": [],
            "gaps": ["未找到运维成本来源"],
        },
    ],
    "qualified": [
        {
            "id": "a",
            "name": "候选 A",
            "constraints": [{"constraint": "支持私有化部署", "result": "SATISFIED"}],
            "attributes": {"部署方式": {"value": 1, "better": "higher"}},
            "source_ids": ["s1"],
        }
    ],
    "unknown_candidates": [
        {
            "id": "b",
            "name": "候选 B",
            "constraints": [{"constraint": "支持私有化部署", "result": "UNKNOWN"}],
            "reason": "存在未知硬约束，需人工确认",
        }
    ],
    "rejected_candidates": [],
    "gaps": ["未找到运维成本来源"],
    "unverified": [],
}

SOURCES = [
    {
        "url": "https://vendor.example/a/docs",
        "title": "候选 A 文档",
        "fetched_at": "2026-10-19T12:00:03Z",
    }
]


def content(structure: str = "report", completeness: str = "PARTIAL") -> DocumentContent:
    return build_content(
        definition_title="技术选型对比",
        structure=structure,
        completeness=completeness,
        inputs={
            "topic": "AI 编程助手",
            "hard_constraints": ["支持私有化部署"],
            "preference_order": ["部署方式"],
        },
        evidence=EVIDENCE,
        sources=SOURCES,
        direction_titles={"official": "官方能力", "ops": "运维与成本限制"},
    )


def test_report_structure_has_required_sections() -> None:
    markdown, document = render_document(content())
    assert "# 技术选型对比" in markdown
    assert "完成程度：PARTIAL" in markdown
    assert "## 研究目标" in markdown
    assert "## 硬约束" in markdown
    assert "## 查询方向与结果" in markdown
    assert "### 官方能力" in markdown
    assert "### 运维与成本限制" in markdown
    assert "## 来源" in markdown
    assert "## 缺口与未证实" in markdown
    assert "QUALIFIED" not in markdown
    assert "满足全部硬约束" in markdown
    assert "优先顺序：部署方式" in markdown
    assert "部署方式=1（越高越好）" in markdown
    assert document["completeness"] == "PARTIAL"


def test_checklist_structure_marks_covered_and_missing_directions() -> None:
    markdown, document = render_document(content("checklist"))
    assert "- [x] 官方能力" in markdown
    assert "- [ ] 运维与成本限制" in markdown
    assert "- [x] 推荐：候选 A" in markdown
    assert document["structure"] == "checklist"


def test_comparison_structure_renders_a_table_with_results() -> None:
    markdown, document = render_document(content("comparison"))
    assert "| 候选 | 支持私有化部署 | 结论 |" in markdown
    assert "| 候选 A | SATISFIED | QUALIFIED |" in markdown
    assert "| 候选 B | UNKNOWN | UNKNOWN |" in markdown
    assert document["structure"] == "comparison"


def test_language_and_detail_preferences_change_the_rendering() -> None:
    english, _ = render_document(content(), language="en", detail="brief")
    assert "Completeness" in english
    assert "## Hard constraints" not in english  # brief omits the constraints section
    assert "模型内部推理" not in english
    brief, _ = render_document(content(), detail="brief")
    assert "## 硬约束" not in brief
    full, _ = render_document(content(), detail="standard")
    assert "## 硬约束" in full


def test_no_qualified_candidates_is_stated_explicitly() -> None:
    evidence = {
        **EVIDENCE,
        "qualified": [],
        "rejected_candidates": [{"id": "c", "name": "候选 C", "constraints": []}],
    }
    built = build_content(
        definition_title="技术选型对比",
        structure="report",
        completeness="NO_QUALIFIED_RESULT",
        inputs={"topic": "x"},
        evidence=evidence,
        sources=[],
    )
    markdown, _ = render_document(built)
    assert "不满足硬约束" in markdown
    assert "完成程度：NO_QUALIFIED_RESULT" in markdown


def test_unknown_candidate_is_single_listed_for_confirmation() -> None:
    markdown, _ = render_document(content())
    assert "存在未知硬约束（待确认）" in markdown
    assert "候选 B" in markdown


def test_unsafe_links_are_not_rendered_as_links() -> None:
    built = build_content(
        definition_title="安全",
        structure="report",
        completeness="PARTIAL",
        inputs={"topic": "x"},
        evidence={
            "directions": [
                {
                    "id": "d",
                    "status": "covered",
                    "findings": [
                        {
                            "claim": "事实",
                            "source_ids": ["s"],
                            "classification": "FACT",
                            "validity": "VERIFIED",
                        }
                    ],
                    "gaps": [],
                }
            ],
            "gaps": ["缺口"],
        },
        sources=[{"url": "javascript:alert(1)", "title": "<script>x</script>", "fetched_at": "t"}],
    )
    markdown, _ = render_document(built)
    assert "javascript:" not in markdown
    assert "<script>" not in markdown


def test_fact_without_source_is_refused() -> None:
    with pytest.raises(AppError) as error:
        build_content(
            definition_title="x",
            structure="report",
            completeness="COMPLETE",
            inputs={"topic": "x"},
            evidence={
                "directions": [
                    {
                        "id": "d",
                        "status": "covered",
                        "findings": [
                            {
                                "claim": "无来源事实",
                                "classification": "FACT",
                                "validity": "VERIFIED",
                            }
                        ],
                        "gaps": [],
                    }
                ],
                "gaps": [],
            },
            sources=[],
        )
    assert error.value.code == "FACT_REQUIRES_SOURCE"


def test_complete_with_open_gaps_is_refused() -> None:
    with pytest.raises(AppError) as error:
        build_content(
            definition_title="x",
            structure="report",
            completeness="COMPLETE",
            inputs={"topic": "x"},
            evidence=EVIDENCE,
            sources=SOURCES,
        )
    assert error.value.code == "COMPLETENESS_MISMATCH"


def test_qualified_candidate_with_unknown_constraint_is_refused() -> None:
    with pytest.raises(AppError) as error:
        build_content(
            definition_title="x",
            structure="report",
            completeness="PARTIAL",
            inputs={"topic": "x"},
            evidence={
                **EVIDENCE,
                "qualified": [
                    {
                        "id": "b",
                        "name": "候选 B",
                        "constraints": [{"constraint": "部署", "result": "UNKNOWN"}],
                    }
                ],
            },
            sources=SOURCES,
        )
    assert error.value.code == "QUALIFIED_CANDIDATE_INVALID"


def test_unsupported_structure_is_refused() -> None:
    with pytest.raises(AppError) as error:
        DocumentContent(
            title="x",
            structure="slides",
            completeness="PARTIAL",
            objective="",
            constraints=[],
            directions=[],
            qualified=[],
            unknown_candidates=[],
            rejected_candidates=[],
            sources=[],
            gaps=[],
            unverified=[],
        ).validate()
    assert error.value.code == "DOCUMENT_STRUCTURE_INVALID"
