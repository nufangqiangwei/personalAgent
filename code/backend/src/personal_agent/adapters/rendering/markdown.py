"""Markdown rendering for delivered documents.

Only Markdown is produced in phase 1. Raw HTML is never emitted and links are
limited to http/https. Three structures are supported (report, checklist,
comparison) with two language and two detail preferences. The completeness marker
is always visible so a partial delivery cannot look complete.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from personal_agent.domain.document_content import DocumentContent, build_content

ALLOWED_SCHEMES = {"http", "https"}

LABELS = {
    "zh-CN": {
        "completeness": "完成程度",
        "objective": "研究目标",
        "constraints": "硬约束",
        "directions": "查询方向与结果",
        "recommendations": "推荐与结论",
        "qualified": "满足全部硬约束",
        "unknown": "存在未知硬约束（待确认）",
        "rejected": "不满足硬约束",
        "sources": "来源",
        "gaps": "缺口与未证实",
        "no_sources": "没有可用来源；相关结论标记为未证实。",
        "no_gaps": "无。",
        "missing_direction": "缺少该方向的结果",
        "no_candidates": "没有可比较的候选；不提供合格推荐。",
        "footer": "本报告不包含模型内部推理；来源与缺口可逐条复核。",
        "unverified_note": "存在未证实的重要事实，已在上文标注。",
        "checklist": "检查清单",
    },
    "en": {
        "completeness": "Completeness",
        "objective": "Objective",
        "constraints": "Hard constraints",
        "directions": "Directions and results",
        "recommendations": "Recommendations",
        "qualified": "All hard constraints satisfied",
        "unknown": "Unknown hard constraints (needs confirmation)",
        "rejected": "Hard constraints violated",
        "sources": "Sources",
        "gaps": "Gaps and unverified claims",
        "no_sources": "No usable sources; related claims are marked unverified.",
        "no_gaps": "None.",
        "missing_direction": "No result for this direction",
        "no_candidates": "No comparable candidates; no qualified recommendation.",
        "footer": "This report contains no model reasoning; sources and gaps are reviewable.",
        "unverified_note": "Some important facts are unverified and marked above.",
        "checklist": "Checklist",
    },
}


def _safe_link(url: str, title: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        # Unsafe targets are shown as plain, escaped text, never as a link.
        return _escape(title) if title else "（链接已省略）"
    label = (title or url).replace("[", "(").replace("]", ")")
    return f"[{label}]({url})"


def _escape(value: str) -> str:
    return value.replace("<", "&lt;").replace(">", "&gt;")


def _labels(language: str) -> dict[str, str]:
    return LABELS.get(language, LABELS["zh-CN"])


def _finding_line(finding: dict[str, Any], *, brief: bool) -> str:
    claim = _escape(str(finding.get("claim", "")))
    if brief:
        return f"- {claim}"
    validity = str(finding.get("validity", "UNVERIFIED"))
    return f"- {claim}（{validity}）"


def _render_report(content: DocumentContent, language: str, brief: bool) -> list[str]:
    labels = _labels(language)
    lines = [
        f"# {_escape(content.title)}",
        "",
        f"> {labels['completeness']}：{content.completeness}",
        "",
    ]
    if content.objective:
        lines += [f"## {labels['objective']}", _escape(content.objective), ""]
    if content.constraints and not brief:
        lines.append(f"## {labels['constraints']}")
        for constraint in content.constraints:
            lines.append(f"- {_escape(constraint)}")
        lines.append("")
    lines.append(f"## {labels['directions']}")
    for direction in content.directions:
        lines.append(f"### {_escape(direction.title)}")
        if direction.status == "missing":
            lines.append(f"- {labels['missing_direction']}")
        for finding in direction.findings:
            lines.append(_finding_line(finding, brief=brief))
        for gap in direction.gaps:
            lines.append(f"- 缺口：{_escape(gap)}")
        lines.append("")
    lines.append(f"## {labels['recommendations']}")
    if not (content.qualified or content.unknown_candidates or content.rejected_candidates):
        lines.append(f"- {labels['no_candidates']}")
    if content.qualified:
        lines.append(f"### {labels['qualified']}")
        for candidate in content.qualified:
            lines.append(f"- **{_escape(str(candidate.get('name')))}**")
            if not brief:
                for constraint in candidate.get("constraints") or []:
                    lines.append(
                        f"  - {_escape(str(constraint.get('constraint')))}："
                        f"{_escape(str(constraint.get('result')))}"
                    )
    if content.unknown_candidates:
        lines.append(f"### {labels['unknown']}")
        for candidate in content.unknown_candidates:
            raw = _escape(str(candidate.get("name"))) + "：" + _escape(
                str(candidate.get("reason", ""))
            )
            lines.append(f"- **{raw}**")
    if content.rejected_candidates:
        lines.append(f"### {labels['rejected']}")
        for candidate in content.rejected_candidates:
            lines.append(f"- **{_escape(str(candidate.get('name')))}**")
    lines.append("")
    lines.append(f"## {labels['sources']}")
    if content.sources:
        for source in content.sources:
            lines.append(
                f"- {_safe_link(str(source.get('url', '')), str(source.get('title', '')))}"
                f"（{source.get('fetched_at', '未知')}）"
            )
    else:
        lines.append(f"- {labels['no_sources']}")
    lines.append("")
    if not brief:
        lines.append(f"## {labels['gaps']}")
        if content.gaps:
            for gap in content.gaps:
                lines.append(f"- {_escape(gap)}")
        if content.unverified:
            lines.append(f"- {labels['unverified_note']}")
        if not content.gaps and not content.unverified:
            lines.append(f"- {labels['no_gaps']}")
        lines.append("")
    lines.append(f"> {labels['footer']}")
    return lines


def _render_checklist(content: DocumentContent, language: str, brief: bool) -> list[str]:
    labels = _labels(language)
    lines = [
        f"# {_escape(content.title)}",
        "",
        f"> {labels['completeness']}：{content.completeness}",
        "",
    ]
    lines.append(f"## {labels['checklist']}")
    for direction in content.directions:
        marker = "x" if direction.status == "covered" else " "
        lines.append(f"- [{marker}] {_escape(direction.title)}")
        for gap in direction.gaps:
            lines.append(f"  - 缺口：{_escape(gap)}")
    for candidate in content.qualified:
        lines.append(f"- [x] 推荐：{_escape(str(candidate.get('name')))}")
    for candidate in content.unknown_candidates:
        lines.append(f"- [ ] 待确认：{_escape(str(candidate.get('name')))}")
    lines.append("")
    lines.append(f"## {labels['sources']}")
    if content.sources:
        for source in content.sources:
            link = _safe_link(str(source.get("url", "")), str(source.get("title", "")))
            lines.append(f"- {link}")
    else:
        lines.append(f"- {labels['no_sources']}")
    lines.append("")
    if not brief:
        lines.append(f"## {labels['gaps']}")
        for gap in content.gaps:
            lines.append(f"- {_escape(gap)}")
        if content.unverified:
            lines.append(f"- {labels['unverified_note']}")
        if not content.gaps and not content.unverified:
            lines.append(f"- {labels['no_gaps']}")
        lines.append("")
    lines.append(f"> {labels['footer']}")
    return lines


def _render_comparison(content: DocumentContent, language: str, brief: bool) -> list[str]:
    labels = _labels(language)
    lines = [
        f"# {_escape(content.title)}",
        "",
        f"> {labels['completeness']}：{content.completeness}",
        "",
    ]
    lines.append(f"## {labels['recommendations']}")
    all_candidates = (
        [("qualified", candidate) for candidate in content.qualified]
        + [("unknown", candidate) for candidate in content.unknown_candidates]
        + [("rejected", candidate) for candidate in content.rejected_candidates]
    )
    if not all_candidates:
        lines.append(f"- {labels['no_candidates']}")
        lines.append("")
    else:
        constraint_names: list[str] = []
        for _kind, candidate in all_candidates:
            for item in candidate.get("constraints") or []:
                name = str(item.get("constraint"))
                if name not in constraint_names:
                    constraint_names.append(name)
        header = ["候选", *constraint_names, "结论"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for kind, candidate in all_candidates:
            results = {
                str(item.get("constraint")): str(item.get("result"))
                for item in candidate.get("constraints") or []
            }
            row = [_escape(str(candidate.get("name")))]
            row.extend(results.get(name, "UNKNOWN") for name in constraint_names)
            row.append(kind.upper())
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    if content.directions and not brief:
        lines.append(f"## {labels['directions']}")
        for direction in content.directions:
            lines.append(f"### {_escape(direction.title)}")
            for finding in direction.findings:
                lines.append(_finding_line(finding, brief=True))
            for gap in direction.gaps:
                lines.append(f"- 缺口：{_escape(gap)}")
        lines.append("")
    lines.append(f"## {labels['sources']}")
    if content.sources:
        for source in content.sources:
            link = _safe_link(str(source.get("url", "")), str(source.get("title", "")))
            lines.append(f"- {link}")
    else:
        lines.append(f"- {labels['no_sources']}")
    lines.append("")
    if not brief:
        lines.append(f"## {labels['gaps']}")
        for gap in content.gaps:
            lines.append(f"- {_escape(gap)}")
        if not content.gaps:
            lines.append(f"- {labels['no_gaps']}")
        lines.append("")
    lines.append(f"> {labels['footer']}")
    return lines


def render_document(
    content: DocumentContent,
    *,
    structure: str | None = None,
    language: str = "zh-CN",
    detail: str = "standard",
) -> tuple[str, dict[str, Any]]:
    chosen = structure or content.structure
    if chosen != content.structure:
        content = DocumentContent(
            title=content.title,
            structure=chosen,
            completeness=content.completeness,
            objective=content.objective,
            constraints=content.constraints,
            directions=content.directions,
            qualified=content.qualified,
            unknown_candidates=content.unknown_candidates,
            rejected_candidates=content.rejected_candidates,
            sources=content.sources,
            gaps=content.gaps,
            unverified=content.unverified,
            notes=content.notes,
        )
    content.validate()
    brief = detail == "brief"
    if chosen == "checklist":
        lines = _render_checklist(content, language, brief)
    elif chosen == "comparison":
        lines = _render_comparison(content, language, brief)
    else:
        lines = _render_report(content, language, brief)
    document = content.as_dict()
    document["rendered"] = {
        "structure": chosen,
        "language": language,
        "detail": detail,
    }
    return "\n".join(lines), document


def render_research_document(
    *,
    title: str,
    structure: str,
    inputs: dict[str, Any],
    evidence: dict[str, Any],
    sources: list[dict[str, Any]],
    completeness: str,
    notes: list[str] | None = None,
    direction_titles: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Compatibility wrapper used before the Artifact Service wiring."""
    content = build_content(
        definition_title=title,
        structure=structure,
        completeness=completeness,
        inputs=inputs,
        evidence=evidence,
        sources=sources,
        direction_titles=direction_titles,
    )
    markdown, document = render_document(content)
    document["notes"] = list(notes or [])
    return markdown, document
