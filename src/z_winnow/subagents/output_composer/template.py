"""T-W13: output_composer lifecycle template rendering.

P022: Storage/Formatting Layer Separation — template only renders by lifecycle field,
does not write to database.

Renders unified topics[] list with lifecycle badges (user_defined|sustained|emerging).
Each topic now contains conclusion, description, trend, participants directly.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Lifecycle badge mapping
# ─────────────────────────────────────────────────────────────

_LIFECYCLE_BADGE: dict[str, str] = {
    "user_defined": "👑 用户定义",
    "sustained": "🔄 持续",
    "emerging": "🆕 新增",
}

_LIFECYCLE_SECTION_HEADERS: dict[str, str] = {
    "user_defined": "## 用户定义议题",
    "sustained": "## 持续议题",
    "emerging": "## 新增议题",
}

# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


def render_topics_with_lifecycle(
    unified_report: dict[str, Any],
    group_id: str = "",
) -> str:
    """Render unified topics grouped by lifecycle classification.

    T-W13: Reads from unified topics[] directly. Each topic has lifecycle
    field (user_defined|sustained|emerging) plus conclusion, description,
    trend, participants.

    Args:
        unified_report: Unified reporter output dict with topics key.
        group_id: Group identifier for constructing promote links.

    Returns:
        Markdown string with lifecycle-grouped topic sections.
    """
    topics_raw = unified_report.get("topics", [])
    if not topics_raw:
        return ""

    # Group by lifecycle
    groups: dict[str, list[dict[str, Any]]] = {
        "user_defined": [],
        "sustained": [],
        "emerging": [],
    }

    for topic in topics_raw:
        if not isinstance(topic, dict):
            continue
        lifecycle = topic.get("lifecycle", "emerging")
        if lifecycle not in groups:
            lifecycle = "emerging"
        groups[lifecycle].append(topic)

    # Render each group
    lines: list[str] = []

    for lc in ("user_defined", "sustained", "emerging"):
        group_topics = groups[lc]
        if not group_topics:
            continue
        lines.append(_LIFECYCLE_SECTION_HEADERS.get(lc, "## 议题"))
        lines.append("")
        lines.extend(_render_topic_list(group_topics, group_id, lifecycle=lc))
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _render_topic_list(
    topics: list[dict[str, Any]],
    group_id: str,
    *,
    lifecycle: str = "emerging",
) -> list[str]:
    """Render a list of topics as Markdown items with lifecycle badges.

    Shows: badge + name + conclusion excerpt + participants.
    Emerging topics show "[⭐ 提升为核心]" link.

    Args:
        topics: Topic dicts from unified topics[].
        group_id: Group identifier for link construction.
        lifecycle: Lifecycle type for this group.

    Returns:
        List of Markdown lines.
    """
    lines: list[str] = []

    for t in topics:
        name = t.get("topic_name", "")
        badge = _LIFECYCLE_BADGE.get(lifecycle, "🆕 新增")
        participants = t.get("participants", [])
        conclusion = t.get("conclusion", "")
        trend = t.get("trend", "")

        # Header line with badge and name
        lines.append(f"- **{badge} {name}**")

        # Participants
        if participants and isinstance(participants, list):
            lines.append(f"  参与群成员: {', '.join(participants)}")

        # Conclusion (truncated for readability)
        if conclusion:
            excerpt = conclusion[:120] + ("..." if len(conclusion) > 120 else "")
            lines.append(f"  > {excerpt}")

        # Trend (brief)
        if trend:
            lines.append(f"  趋势: {trend[:80]}{'...' if len(trend) > 80 else ''}")

        # Emerging topics: show promote link
        if lifecycle == "emerging" and group_id:
            encoded_title = quote(name, safe="")
            promote_url = f"/groups/{group_id}/core_topics/new?title={encoded_title}"
            lines.append(f"  [⭐ 提升为核心]({promote_url})")

    return lines
