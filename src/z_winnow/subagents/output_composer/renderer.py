"""renderer.py — render ComposedData to Markdown via Jinja2 templates.

Wraps the templates/renderer.py module for template selection and rendering.
Also provides inline rendering for sections that lack a dedicated template.

Architecture reference: plans/tracks/track-e4.md T-E4-3
"""

from __future__ import annotations

import logging
import re
from typing import Any

from z_winnow.subagents.output_composer.merger import ComposedData
from z_winnow.templates.renderer import (
    md_heading,
    md_table,
    render_report,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------

_TEMPLATE_MAP: dict[str, str] = {
    "daily_report": "daily_report_v1",
    "daily": "daily_report_v1",
    "feishu_daily": "feishu_daily_v1",
}


def select_template(report_type: str) -> str:
    """Map a user-facing report type name to an internal schema name.

    Args:
        report_type: e.g. "daily_report", "feishu_daily".

    Returns:
        Schema name for use with render_report(), e.g. "daily_report_v1".
    """
    schema = _TEMPLATE_MAP.get(report_type, "daily_report_v1")
    logger.debug("Template selection: %s → %s", report_type, schema)
    return schema


def is_feishu_format(template_name: str) -> bool:
    """Check if the template targets Feishu Markdown format."""
    return template_name.startswith("feishu_")


# ---------------------------------------------------------------------------
# Data preparation for templates
# ---------------------------------------------------------------------------


def _prepare_daily_data(composed: ComposedData) -> dict[str, Any]:
    """Prepare data dict for daily_report.j2 template.

    Args:
        composed: Merged ComposedData.

    Returns:
        Dict with keys matching daily_report.j2 template variables.
    """
    # Format date for display: YYYYMMDD → YYYY-MM-DD
    date_str = composed.date
    display_date = (
        f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}" if len(date_str) == 8 else date_str
    )

    return {
        "date": display_date,
        "overview": composed.overview,
        "important_notice": composed.important_notice,
        "topics": composed.topics,
        "trend_analysis": composed.trend_analysis,
        "trend_summary": composed.trend_summary,
        "highlights": composed.highlights,
        "model_used": "",
    }


# ---------------------------------------------------------------------------
# Inline rendering for sections not covered by templates
# ---------------------------------------------------------------------------


def _render_resources_md(composed: ComposedData) -> str:
    """Render the resources section as inline Markdown.

    Args:
        composed: Merged ComposedData.

    Returns:
        Markdown string for the resources section.
    """
    lines: list[str] = []
    lines.append(md_heading(2, "资源汇总"))
    lines.append("")

    if not composed.resources:
        lines.append("*(本日无资源分享)*")
        lines.append("")
        return "\n".join(lines)

    headers = ["时间段", "类型", "简介", "链接"]
    rows: list[list[str]] = []
    for r in composed.resources:
        rows.append(
            [
                r.get("time_range", ""),
                r.get("resource_type", ""),
                r.get("summary", ""),
                r.get("content", ""),
            ]
        )
    lines.append(md_table(headers, rows))
    lines.append("")

    # Count summary
    if composed.resource_count_by_type:
        counts = [f"{k}: {v}个" for k, v in composed.resource_count_by_type.items()]
        lines.append(f"*资源类型分布: {' / '.join(counts)}*")
        lines.append("")

    return "\n".join(lines)


# Custom-table column keys that are pure plumbing, not reader-facing.
_NOISE_COLUMN_KEYS = frozenset({"source_server_ids"})


def _render_custom_tables_md(composed: ComposedData) -> str:
    """Render non-engineering custom tables as inline Markdown sections.

    engineering has its own dedicated renderer (``_render_engineering_md``) with
    status/summary/topics formatting; every OTHER enabled custom table (e.g.
    world_models) is rendered here generically — one section per table, records
    as a Markdown table (auto-columns from record keys, noise keys excluded),
    plus an optional summary block. Adding a new table needs no renderer change.
    """
    from z_winnow.custom_tables import registry as ct_registry

    sections: list[str] = []
    for kind, table_data in composed.custom_table_data.items():
        if kind == "engineering":
            continue  # dedicated renderer
        if not isinstance(table_data, dict):
            continue
        tdef = ct_registry.get_table(kind)
        display_name = tdef.name if tdef else kind
        records_key = tdef.records_key if tdef else "items"
        summary_key = tdef.summary_key if tdef else None
        records = table_data.get(records_key)
        if not isinstance(records, list):
            records = []

        lines: list[str] = [md_heading(2, display_name), ""]
        if records:
            col_map = tdef.markdown_columns if (tdef and tdef.markdown_columns) else None
            if isinstance(col_map, dict) and col_map:
                # explicit columns from the table def (record_key → 中文列名)
                keys = list(col_map.keys())
                headers = [col_map[k] for k in keys]
            else:
                # auto-columns: union of record keys in first-seen order, minus noise
                keys = []
                headers = []
                seen: set[str] = set()
                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    for k in rec:
                        if k in _NOISE_COLUMN_KEYS or k in seen:
                            continue
                        seen.add(k)
                        keys.append(k)
                        headers.append(k)
            rows = [[str(rec.get(h, "")) for h in keys] for rec in records if isinstance(rec, dict)]
            lines.append(md_table(headers, rows))
            lines.append("")
        else:
            lines.append(f"*(本日无{display_name})*")
            lines.append("")

        if summary_key:
            summary = table_data.get(summary_key)
            if isinstance(summary, dict) and summary:
                lines.append(md_heading(3, f"{display_name}摘要"))
                lines.append("")
                for k, v in summary.items():
                    lines.append(f"- **{k}**: {v}")
                lines.append("")

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _render_engineering_md(composed: ComposedData) -> str:
    """Render the engineering issues section as inline Markdown.

    Args:
        composed: Merged ComposedData.

    Returns:
        Markdown string for the engineering section.
    """
    lines: list[str] = []
    lines.append(md_heading(2, "工程问题"))
    lines.append("")

    if composed.issues:
        headers = ["时间", "分组", "描述", "解决方案", "状态", "来源"]
        rows: list[list[str]] = []
        for issue in composed.issues:
            rows.append(
                [
                    issue.get("datetime", ""),
                    issue.get("group", ""),
                    issue.get("description", ""),
                    issue.get("solution", ""),
                    issue.get("status", ""),
                    issue.get("source_members", ""),
                ]
            )
        lines.append(md_table(headers, rows))
        lines.append("")
    else:
        lines.append("*(本日无工程问题)*")
        lines.append("")

    # Group summaries
    if composed.group_summary:
        lines.append(md_heading(3, "分组摘要"))
        lines.append("")
        for group_name, summary in composed.group_summary.items():
            lines.append(f"- **{group_name}**: {summary}")
        lines.append("")

    # Topics (T-W13: unified topics[] with lifecycle classification)
    topics = composed.topics
    if topics:
        lines.append(md_heading(3, "议题追踪"))
        lines.append("")
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            name = topic.get("topic_name", "")
            tid = topic.get("topic_id", "")
            lifecycle = topic.get("lifecycle", "emerging")
            weight = topic.get("weight", 0)
            trend = topic.get("trend", "")
            background = topic.get("background", "")
            process_text = topic.get("process", "")
            conclusion = topic.get("conclusion", "")
            lines.append(f"- **{tid} {name}** [{lifecycle}] (权重: {weight:.1f}, 趋势: {trend})")
            if background:
                lines.append(f"  - 背景: {background}")
            if process_text:
                lines.append(f"  - 过程: {process_text}")
            if conclusion:
                lines.append(f"  - 结论: {conclusion}")
            lines.append("")
        if composed.trend_summary:
            lines.append(f"> 趋势摘要: {composed.trend_summary}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------


def render_composed(
    composed: ComposedData,
    template_name: str = "daily_report",
) -> str:
    """Render ComposedData to a complete Markdown report.

    Uses Jinja2 templates from templates/renderer.py for the main daily
    body, and inline Markdown helpers for resource and engineering sections
    that may be appended after the main template.

    Args:
        composed: Merged ComposedData instance.
        template_name: Template identifier, e.g. "daily_report", "feishu_daily".

    Returns:
        Complete Markdown report string.

    Raises:
        ValueError: If template_name is unrecognized.
    """
    schema = select_template(template_name)

    # Render main body via Jinja2 template
    if "daily" in template_name.lower():
        data = _prepare_daily_data(composed)
        main_body = render_report(schema, data)
    else:
        raise ValueError(f"Unsupported template: {template_name}")

    # Append resources section if available
    parts: list[str] = [main_body]

    if composed.resources:
        resources_md = _render_resources_md(composed)
        parts.append(resources_md)

    if composed.issues or composed.topics:
        engineering_md = _render_engineering_md(composed)
        parts.append(engineering_md)

    # Generic custom tables (world_models + future) — one section each.
    custom_md = _render_custom_tables_md(composed)
    if custom_md.strip():
        parts.append(custom_md)

    final = "\n\n".join(parts)

    logger.info(
        "render_composed: %s → %d chars",
        template_name,
        len(final),
    )
    return final


# ---------------------------------------------------------------------------
# Markdown syntax check
# ---------------------------------------------------------------------------

# Patterns that indicate broken Markdown
_UNCLOSED_CODE_BLOCK = re.compile(r"```(?![\s\S]*```)")
_BROKEN_LINK = re.compile(r"\[[^\]]*\]\((?!https?://)[^)]*\)")
_UNMATCHED_BACKTICK = re.compile(r"(?<!`)`(?!`)([^`]*)`(?!`)")
_EMPTY_LINK = re.compile(r"\[([^\]]*)\]\(\s*\)")


def check_markdown_syntax(markdown_text: str) -> list[str]:
    """Check Markdown text for common syntax errors.

    Detects:
    - Unclosed fenced code blocks (odd number of ``` fences)
    - Broken references (links without valid URLs)
    - Empty links

    Does NOT attempt full CommonMark validation — only catches errors
    that would cause visible rendering issues in Feishu Docs.

    Args:
        markdown_text: The rendered Markdown string to check.

    Returns:
        List of human-readable error strings (empty if valid).
    """
    errors: list[str] = []

    # Check unclosed code blocks
    fence_count = markdown_text.count("```")
    if fence_count % 2 != 0:
        errors.append("Markdown 语法错误: 代码块未闭合（``` 数量为奇数）")

    # Check for obviously broken links (no http/https)
    for match in _BROKEN_LINK.finditer(markdown_text):
        errors.append(f"Markdown 语法警告: 链接格式可能不正确 — '{match.group()[:60]}'")

    # Check empty links
    empty_links = _EMPTY_LINK.findall(markdown_text)
    if empty_links:
        errors.append(f"Markdown 语法警告: {len(empty_links)} 个链接目标为空")

    return errors


def render_plaintext(composed: ComposedData) -> str:
    """Render ComposedData as plain text (no Markdown formatting).

    Useful for environments where Markdown rendering is unavailable (e.g.
    plain-text notification channels).

    Args:
        composed: Merged ComposedData.

    Returns:
        Plain text report string.
    """
    lines: list[str] = []

    # Format date
    date_str = composed.date
    display_date = (
        f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}" if len(date_str) == 8 else date_str
    )

    lines.append(f"群日报 · {display_date}")
    if composed.group_name:
        lines.append(f"群聊: {composed.group_name}")
    lines.append("")

    lines.append("【日报概览】")
    lines.append(composed.overview)
    lines.append("")

    if composed.important_notice:
        lines.append(f"❗️重要提醒: {composed.important_notice}")
        lines.append("")

    if composed.topics:
        lines.append("【价值议题】")
        for t in composed.topics:
            if not isinstance(t, dict):
                continue
            name = t.get("topic_name", "")
            lifecycle = t.get("lifecycle", "emerging")
            participants = t.get("participants", [])
            conclusion = t.get("conclusion", "")
            badge = {"user_defined": "👑", "sustained": "🔄", "emerging": "🆕"}.get(lifecycle, "")
            lines.append(f"  {badge} {name}")
            if participants:
                lines.append(f"    参与群成员: {', '.join(participants)}")
            if conclusion:
                lines.append(f"    > {conclusion[:100]}")
        lines.append("")

    if composed.trend_analysis:
        lines.append("【讨论趋势】")
        # W8-D: trend_analysis can be str (new format) or dict (old format)
        if isinstance(composed.trend_analysis, str):
            lines.append(f"  {composed.trend_analysis}")
        elif isinstance(composed.trend_analysis, dict):
            phase = composed.trend_analysis.get("current_phase", "")
            if phase:
                lines.append(f"  当前阶段: {phase}")
            pending = composed.trend_analysis.get("pending_issues", "")
            if pending:
                lines.append(f"  待解决问题: {pending}")
        lines.append("")

    if composed.highlights:
        lines.append("【亮点/金句】")
        for h in composed.highlights:
            lines.append(f"  · {h}")
        lines.append("")

    if composed.resources:
        lines.append("【资源汇总】")
        for r in composed.resources:
            lines.append(
                f"  [{r.get('resource_type', '')}] {r.get('summary', '')} — {r.get('content', '')}"
            )
        lines.append("")

    if composed.issues:
        lines.append("【工程问题】")
        for i in composed.issues:
            lines.append(
                f"  [{i.get('status', '')}] [{i.get('group', '')}] "
                f"{i.get('description', '')} — {i.get('solution', '')}"
            )
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "check_markdown_syntax",
    "is_feishu_format",
    "render_composed",
    "render_plaintext",
    "select_template",
]
