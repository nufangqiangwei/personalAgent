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

_BUDGET_LABELS = {
    "active_ms": "有效研究时长（毫秒）",
    "model_requests": "模型请求",
    "tool_calls": "工具调用",
    "search_calls": "搜索",
    "page_reads": "页面读取",
    "tokens": "tokens",
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
    accepted_suggestions: list[dict[str, Any]] | None = None,
    output_spec: dict[str, Any] | None = None,
    policy_hash: str | None = None,
    run_budget: dict[str, int] | None = None,
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

    if accepted_suggestions:
        lines.append("## 四、已接受建议")
        for item in accepted_suggestions:
            path = HEADINGS.get(str(item.get("field_path")), str(item.get("field_path")))
            rationale = str(item.get("rationale") or "").strip()
            rendered = _format_value(item.get("value"))
            lines.append(
                f"- **{path}**：{rendered}" + (f"（依据：{rationale}）" if rationale else "")
            )
        lines.append("")

    if output_spec or run_budget or policy_hash:
        lines.append("## 五、交付与策略")
        if output_spec:
            for key, value in output_spec.items():
                lines.append(f"- {HEADINGS.get(str(key), str(key))}：{_format_value(value)}")
        if run_budget:
            budget_text = "，".join(
                f"{_BUDGET_LABELS.get(key, key)} {value}" for key, value in run_budget.items()
            )
            lines.append(f"- 执行预算上限：{budget_text}")
        if policy_hash:
            lines.append(f"- 政策指纹：{policy_hash}")
        lines.append("")

    lines.append(f"> 流程版本：v{workflow_version_no}。确认后才会创建执行。")
    return "\n".join(lines)


def summary_sections(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("## ")]
