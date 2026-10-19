"""Deterministic summary rendering for requirement confirmation.

The summary is rendered from structured fields only. Headings are never padded
with invented requirements: an empty section is omitted and the missing items are
listed under their own heading.
"""

from __future__ import annotations

from typing import Any

HEADINGS = {
    "topic": "研究主题",
    "audience": "使用对象",
    "hard_constraints": "硬约束",
    "preference_order": "优先顺序",
    "output_spec": "交付要求",
    "recency": "时效要求",
}


def _format_value(value: Any) -> str:
    if value is None:
        return "无特别偏好"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "；".join(str(item) for item in value)
    return str(value)


def render_summary(
    *,
    workflow_name: str,
    workflow_version_no: int,
    fields: dict[str, Any],
    missing: list[str],
    conflicts: list[dict[str, Any]],
    preference_order: list[str],
) -> str:
    lines: list[str] = [f"# {workflow_name} · 需求确认", ""]

    lines.append("## 一、研究要求")
    if fields:
        for path, entry in fields.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            label = HEADINGS.get(path, path)
            suffix = ""
            source = entry.get("source") if isinstance(entry, dict) else None
            if source == "default":
                suffix = "（默认值，待确认）"
            lines.append(f"- **{label}**：{_format_value(value)}{suffix}")
    else:
        lines.append("- 尚未收集到要求。")
    lines.append("")

    lines.append("## 二、优先顺序")
    if preference_order:
        for index, path in enumerate(preference_order, start=1):
            lines.append(f"{index}. {HEADINGS.get(path, path)}")
    else:
        lines.append("- 无特别偏好（按流程默认顺序）。")
    lines.append("")

    lines.append("## 三、确认与缺口")
    if missing:
        lines.append("- 仍缺少：" + "、".join(HEADINGS.get(item, item) for item in missing))
    else:
        lines.append("- 必要信息已齐全。")
    if conflicts:
        for conflict in conflicts:
            lines.append(
                f"- 冲突：{conflict.get('path', '未知字段')} — {conflict.get('message', '')}"
            )
    else:
        lines.append("- 未发现冲突。")
    lines.append("")
    lines.append(f"> 流程版本：v{workflow_version_no}。确认后才会创建执行。")
    return "\n".join(lines)


def summary_sections(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("## ")]
