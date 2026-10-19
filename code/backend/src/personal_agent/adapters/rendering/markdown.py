"""Markdown rendering for delivered research documents.

Only Markdown is produced in phase 1. Raw HTML is never emitted; links are
limited to http/https. The document keeps sources, gaps, hard-constraint results
and the completeness marker, so a partial delivery cannot look complete.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


def _safe_link(url: str, title: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        return title or "（链接已省略）"
    label = (title or url).replace("[", "(").replace("]", ")")
    return f"[{label}]({url})"


def _escape(value: str) -> str:
    return value.replace("<", "&lt;").replace(">", "&gt;")


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
    directions = evidence.get("directions") or []
    gaps = evidence.get("gaps") or []
    qualified = evidence.get("qualified") or []
    unknown = evidence.get("unknown_candidates") or []
    rejected = evidence.get("rejected_candidates") or []

    lines: list[str] = [f"# {_escape(title)}", ""]
    lines.append(f"> 完成程度：{completeness}")
    lines.append("")

    lines.append("## 本次要求")
    for path, value in inputs.items():
        rendered = "；".join(str(item) for item in value) if isinstance(value, list) else str(value)
        lines.append(f"- **{path}**：{_escape(rendered) if rendered else '无特别偏好'}")
    if not inputs:
        lines.append("- 无额外输入。")
    lines.append("")

    lines.append("## 查询方向与结果")
    titles = direction_titles or {}
    for direction in directions:
        direction_id = str(direction.get("id"))
        lines.append(f"### {_escape(titles.get(direction_id, direction_id))}")
        if direction.get("status") == "missing":
            lines.append("- 状态：缺少结果")
        for finding in direction.get("findings") or []:
            claim = _escape(str(finding.get("claim", "")))
            validity = finding.get("validity", "UNVERIFIED")
            lines.append(f"- {claim}（{validity}）")
        for gap in direction.get("gaps") or []:
            lines.append(f"- 缺口：{_escape(str(gap))}")
    lines.append("")

    if structure == "comparison" or qualified or unknown or rejected:
        lines.append("## 候选对比")
        if qualified:
            lines.append("### 满足全部硬约束")
            for candidate in qualified:
                lines.append(f"- **{_escape(str(candidate.get('name')))}**")
                for constraint in candidate.get("constraints") or []:
                    lines.append(
                        "  - "
                        f"{_escape(str(constraint.get('constraint')))}："
                        f"{constraint.get('result')}"
                    )
        if unknown:
            lines.append("### 存在未知硬约束（待确认）")
            for candidate in unknown:
                lines.append(
                    "**"
                    f"{_escape(str(candidate.get('name')))}**："
                    f"{_escape(str(candidate.get('reason', '')))}"
                )
        if rejected:
            lines.append("### 不满足硬约束")
            for candidate in rejected:
                lines.append(f"- **{_escape(str(candidate.get('name')))}**")
        if not qualified and not unknown and not rejected:
            lines.append("- 没有可比较的候选；不提供合格推荐。")
        lines.append("")

    lines.append("## 来源")
    if sources:
        for source in sources:
            lines.append(
                f"- {_safe_link(str(source.get('url', '')), str(source.get('title', '')))}"
                f"（获取于 {source.get('fetched_at', '未知')}）"
            )
    else:
        lines.append("- 没有可用来源；相关结论标记为未证实。")
    lines.append("")

    lines.append("## 缺口与未证实")
    if gaps:
        for gap in gaps:
            lines.append(f"- {_escape(str(gap))}")
    if evidence.get("unverified"):
        lines.append("- 存在未证实的重要事实，已在上文标注。")
    if not gaps and not evidence.get("unverified"):
        lines.append("- 无。")
    lines.append("")
    lines.append("> 本报告不包含模型内部推理；来源与缺口可逐条复核。")

    markdown = "\n".join(lines)
    document = {
        "schema_version": 1,
        "title": title,
        "structure": structure,
        "completeness": completeness,
        "inputs": inputs,
        "directions": directions,
        "sources": sources,
        "gaps": gaps,
        "qualified": qualified,
        "unknown_candidates": unknown,
        "rejected_candidates": rejected,
        "notes": notes or [],
    }
    return markdown, document
