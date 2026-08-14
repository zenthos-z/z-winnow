"""quality.py — automated quality checks for composed reports.

Performs structural, content, consistency, provenance, and Markdown syntax
checks. Returns a quality score (0-100) and a list of warning messages.
Score below 60 triggers degraded rendering warnings in the caller.

Architecture reference: plans/tracks/track-e4.md T-E4-4
"""

from __future__ import annotations

import logging

from z_winnow.subagents.output_composer.merger import ComposedData
from z_winnow.subagents.output_composer.renderer import check_markdown_syntax

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum score per check category (total = 100)
SCORE_SECTION_COMPLETENESS = 20.0  # All expected sections present
SCORE_CONTENT_NON_EMPTY = 20.0  # Key fields are non-empty
SCORE_DATA_CONSISTENCY = 20.0  # Counts and summaries are consistent
SCORE_PROVENANCE = 20.0  # source_server_ids present on key conclusions
SCORE_MARKDOWN_VALIDITY = 20.0  # No syntax errors in rendered output

QUALITY_THRESHOLD = 60.0  # Minimum acceptable score


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _check_section_completeness(composed: ComposedData) -> tuple[float, list[str]]:
    """Check that all expected daily report sections exist.

    Expected sections: overview, topics, trend_analysis.
    Soft sections (nice to have): highlights.

    Returns:
        (score, warnings) where score is 0-SCORE_SECTION_COMPLETENESS.
    """
    warnings: list[str] = []
    max_score = SCORE_SECTION_COMPLETENESS
    deductions = 0.0

    if not composed.overview:
        warnings.append("板块完整性: 缺少日报概览")
        deductions += 1

    if not composed.topics:
        warnings.append("板块完整性: 缺少价值议题表")
        deductions += 1

    if not composed.trend_analysis:
        warnings.append("板块完整性: 缺少讨论趋势分析")
        deductions += 1
    elif isinstance(composed.trend_analysis, dict):
        if not composed.trend_analysis.get("current_phase"):
            warnings.append("板块完整性: 讨论趋势缺少 current_phase")
            deductions += 0.5
        if not composed.trend_analysis.get("pending_issues"):
            warnings.append("板块完整性: 讨论趋势缺少 pending_issues")
            deductions += 0.5

    # Resources and engineering are optional (may not be requested)
    # Only warn if they were expected but missing. We can't know at this
    # level whether they were requested, so we skip the check.

    score = max(0.0, max_score - deductions * (max_score / 4))
    return score, warnings


def _check_content_non_empty(composed: ComposedData) -> tuple[float, list[str]]:
    """Check that key content fields are non-empty.

    Checks that:
    - overview is not an empty string
    - At least one topic_section has meaningful content
    - If resources exist, at least one has a summary

    Returns:
        (score, warnings) where score is 0-SCORE_CONTENT_NON_EMPTY.
    """
    warnings: list[str] = []
    max_score = SCORE_CONTENT_NON_EMPTY
    deductions = 0.0

    # Overview must be non-empty
    if not composed.overview or not composed.overview.strip():
        warnings.append("内容非空: 日报概览为空")
        deductions += 2

    # At least one meaningful topic section
    if composed.topics:
        meaningful = any(
            (
                (
                    t.get("background", "").strip()
                    or t.get("process", "").strip()
                    or t.get("conclusion", "").strip()
                    or t.get("description", "").strip()
                )
                if isinstance(t, dict)
                else ""
            )
            for t in composed.topics
        )
        if not meaningful:
            warnings.append("内容非空: 价值议题表的内容列为空")
            deductions += 1
    # Don't penalize if topics is empty — that's covered by
    # _check_section_completeness.

    # Resources: if present, at least one should have a summary
    if composed.resources:
        valid_resources = [
            r
            for r in composed.resources
            if (r.get("summary", "").strip() if isinstance(r, dict) else False)
        ]
        if len(valid_resources) == 0:
            warnings.append("内容非空: 所有资源项的简介为空")
            deductions += 1

    score = max(0.0, max_score - deductions * (max_score / 4))
    return score, warnings


def _check_data_consistency(composed: ComposedData) -> tuple[float, list[str]]:
    """Check internal data consistency.

    Checks:
    - Resource count matches count_by_type sum
    - Issues and group_summary are aligned
    - Topic entries have valid date format

    Returns:
        (score, warnings) where score is 0-SCORE_DATA_CONSISTENCY.
    """
    warnings: list[str] = []
    max_score = SCORE_DATA_CONSISTENCY
    deductions = 0.0

    # Resource count vs count_by_type
    if composed.resources and composed.resource_count_by_type:
        total_from_counts = sum(composed.resource_count_by_type.values())
        actual_count = len(composed.resources)
        if total_from_counts != actual_count:
            warnings.append(
                f"数据一致性: 资源数量 {actual_count} 与 "
                f"count_by_type 总和 {total_from_counts} 不一致"
            )
            deductions += 1

    # Issues vs group_summary alignment
    if composed.issues and composed.group_summary:
        issue_groups = set()
        for issue in composed.issues:
            group = issue.get("group", "") if isinstance(issue, dict) else ""
            if group:
                issue_groups.add(group)
        summary_groups = set(composed.group_summary.keys())
        missing = issue_groups - summary_groups
        if missing:
            # Warn but don't hard-fail: summaries might be condensed
            logger.debug("Data consistency: issue groups without summaries: %s", missing)
    elif composed.issues and not composed.group_summary:
        warnings.append("数据一致性: 存在工程问题但缺少分组摘要")
        deductions += 0.5

    # Topic date format check (first_seen / last_seen should be valid)
    for topic in composed.topics:
        if not isinstance(topic, dict):
            continue
        for date_field in ("first_seen", "last_seen"):
            date_val = topic.get(date_field, "")
            if date_val and not date_val.strip():
                continue
            # Accept YYYYMMDDTHH:MM:SS or YYYYMMDD format
            raw = str(date_val).split("T")[0] if "T" in str(date_val) else str(date_val)
            if raw and not (len(raw) == 8 and raw.isdigit()):
                warnings.append(
                    f"数据一致性: 议题 '{topic.get('topic_name', '?')}' "
                    f"{date_field} 格式异常: {date_val}"
                )
                deductions += 0.25
                break

    score = max(0.0, max_score - deductions * (max_score / 4))
    return score, warnings


def _check_provenance(composed: ComposedData) -> tuple[float, list[str]]:
    """Check provenance traceability (serverID cross-references).

    Warns (does not fail) when key conclusions lack source_server_ids.
    This is a soft check — provenance is desirable but not required
    for a usable report.

    Returns:
        (score, warnings) where score is 0-SCORE_PROVENANCE.
    """
    warnings: list[str] = []
    max_score = SCORE_PROVENANCE
    deductions = 0.0

    src_id_count = 0
    total_topics = 0

    for topic in composed.topics:
        total_topics += 1
        if isinstance(topic, dict) and topic.get("source_server_ids"):
            src_id_count += 1

    # Check topics have source_server_ids

    # If less than 30% of daily topics have provenance, warn
    if total_topics > 0:
        ratio = src_id_count / total_topics
        if ratio < 0.3:
            warnings.append(
                f"溯源完整性: 仅 {src_id_count}/{total_topics} 个议题有 serverId 溯源 ({ratio:.0%})"
            )
            deductions += 1

    score = max(0.0, max_score - deductions * (max_score / 4))
    return score, warnings


def _check_markdown_validity(rendered: str) -> tuple[float, list[str]]:
    """Check rendered Markdown for syntax errors.

    Delegates to check_markdown_syntax() and scores based on error count.

    Returns:
        (score, warnings) where score is 0-SCORE_MARKDOWN_VALIDITY.
    """
    max_score = SCORE_MARKDOWN_VALIDITY

    if not rendered:
        return 0.0, ["Markdown 有效性: 渲染输出为空"]

    errors = check_markdown_syntax(rendered)
    if not errors:
        return max_score, []

    # Deduct 10 points per error
    deductions = min(len(errors) * 10, max_score)
    warnings = [f"Markdown 有效性: {e}" for e in errors]
    score = max(0.0, max_score - deductions)
    return score, warnings


# ---------------------------------------------------------------------------
# Main quality check function
# ---------------------------------------------------------------------------


def quality_check(
    composed: ComposedData,
    rendered: str = "",
) -> tuple[float, list[str]]:
    """Run all quality checks on a composed report.

    Evaluates five dimensions (20 points each, total 100):
    1. Section completeness: All expected sections present
    2. Content non-empty: Key fields have meaningful content
    3. Data consistency: Counts and internal references match
    4. Provenance: Key conclusions trace to source serverIds
    5. Markdown validity: No syntax errors (only if rendered text is provided)

    Args:
        composed: Merged ComposedData to check.
        rendered: Rendered Markdown string (optional). If empty, Markdown
                  validity check is skipped and scored at 20/20.

    Returns:
        Tuple of (quality_score 0-100, list of warning strings).

    Example:
        >>> composed = ComposedData(overview="Today's summary", ...)
        >>> score, warnings = quality_check(composed, rendered="# Report...")
        >>> score > 60
        True
    """
    total_score = 0.0
    all_warnings: list[str] = []

    # 1. Section completeness (20 points)
    score, w = _check_section_completeness(composed)
    total_score += score
    all_warnings.extend(w)

    # 2. Content non-empty (20 points)
    score, w = _check_content_non_empty(composed)
    total_score += score
    all_warnings.extend(w)

    # 3. Data consistency (20 points)
    score, w = _check_data_consistency(composed)
    total_score += score
    all_warnings.extend(w)

    # 4. Provenance (20 points)
    score, w = _check_provenance(composed)
    total_score += score
    all_warnings.extend(w)

    # 5. Markdown validity (20 points) — only if rendered text is provided
    if rendered:
        score, w = _check_markdown_validity(rendered)
    else:
        score = SCORE_MARKDOWN_VALIDITY
        w = []
    total_score += score
    all_warnings.extend(w)

    # Clamp to [0, 100]
    total_score = max(0.0, min(100.0, total_score))

    logger.info(
        "quality_check: score=%.0f/100, %d warnings",
        total_score,
        len(all_warnings),
    )

    return total_score, all_warnings


__all__ = [
    "QUALITY_THRESHOLD",
    "quality_check",
]
