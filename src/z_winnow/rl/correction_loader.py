"""Correction example loader — query feedback_events for few-shot prompt injection.

Loads historical correction examples from the feedback_events table, filtered
by group_id, target_type, recency, and sorted by severity then date. Used by
prompt builders (e.g. unified_reporter) to inject administrator feedback
as few-shot examples into LLM prompts.

Reference:
    docs/wave10-design.md §5.2 — load_corrections interface
    docs/wave10-design.md §5.3 — XML injection format
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from z_winnow.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================
# CorrectionExample — single admin correction as a few-shot example
# ============================================================


@dataclass
class CorrectionExample:
    """A single administrator correction event for prompt injection.

    Attributes:
        context: Surrounding text or topic name providing context.
        original: Original text that was corrected (the "wrong" output).
        issue: Comma-joined tags from feedback.tags (e.g. "fact_error,missing").
        should_be: Corrected text (the desired output).
        severity: Severity level (1=mild, 2=noticeable, 3=severe).
        corrected_at: ISO 8601 timestamp of correction.
    """

    context: str = ""
    original: str = ""
    issue: str = ""
    should_be: str = ""
    severity: int = 1
    corrected_at: str = ""


# ============================================================
# _build_query — construct parameterized SQL from cascading args
# P009: Optional Parameter Cascading — each parameter independently
#       appends a WHERE clause and a bound parameter.
# ============================================================


def _build_query(
    group_id: str,
    target_type: str | None,
    days: int,
    limit: int,
) -> tuple[str, list]:
    """Build the SELECT query and parameter list for load_corrections.

    P009: Each optional parameter independently appends its WHERE condition.
    A008: Query parameters are built as a list, not string interpolation.

    Args:
        group_id: Group chat identifier for per-group isolation.
        target_type: Optional target type filter (report|topic|trend|etc).
        days: Lookback window in days.
        limit: Maximum number of CorrectionExample entries to return.

    Returns:
        Tuple of (SQL query string, parameter list).
    """
    # A008: defensive init — start with mandatory params only
    params: list = [group_id]

    query = """
        SELECT
            target_type,
            target_id,
            original_text,
            corrected_text,
            tags,
            severity,
            correction_note,
            created_at
        FROM feedback_events
        WHERE group_id = ?
          AND correction_mode IN ('free_text', 'inline_edit')
          AND corrected_text IS NOT NULL
          AND corrected_text != ''
    """

    # P009: days filter — build modifier string safely from int
    query += "      AND created_at >= datetime('now', ?)\n"
    params.append(f"-{days} days")

    # M4: status filter — 跳过 rolled_back 反馈（生效过滤）
    query += "      AND (status IS NULL OR status = 'active')\n"

    # P009: target_type cascade — only appended when parameter is non-None
    if target_type is not None:
        query += "      AND target_type = ?\n"
        params.append(target_type)

    # G1: severity 是 TEXT 枚举（error/warning/info），不能直接 ORDER BY 字母序——
    # 用 CASE 映射成数值（error=3 > warning=2 > info=1）再 DESC。
    query += """      ORDER BY CASE severity WHEN 'error' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC,
        created_at DESC
        LIMIT ?"""

    params.append(limit)

    return query, params


# ============================================================
# _row_to_example — convert a feedback_events row dict → CorrectionExample
# A008: Defensive Init — each row conversion wrapped in try/except
# ============================================================


def _row_to_example(row: dict) -> CorrectionExample | None:
    """Convert a single feedback_events row dict to a CorrectionExample.

    A008: Row mapping is wrapped in try/except so a single malformed row
    does not crash the entire batch. Returns None if conversion fails.

    Args:
        row: Dict from aiosqlite.Row, containing feedback_events columns.

    Returns:
        CorrectionExample on success, None if row is malformed.
    """
    try:
        # Build context from target_type + target_id
        target_type = row.get("target_type") or ""
        target_id = row.get("target_id") or ""
        context_parts = [p for p in [target_type, target_id] if p]
        context = " / ".join(context_parts) if context_parts else ""

        # Parse tags from JSON string → comma-joined issue string
        tags_raw: str | None = row.get("tags")  # A008
        tags: list[str] = []
        if tags_raw and isinstance(tags_raw, str):
            with suppress(json.JSONDecodeError):
                parsed = json.loads(tags_raw)
                if isinstance(parsed, list):
                    tags = [str(t) for t in parsed]
        # If tags_raw is already a list (pre-parsed), use directly
        elif tags_raw and isinstance(tags_raw, list):
            tags = [str(t) for t in tags_raw]
        issue = ",".join(tags)

        # G1: severity 存为 TEXT 枚举（error/warning/info）——映射成数值；
        # 兼容旧数值 schema（int 直存）。
        severity_raw = row.get("severity")
        _sev_map = {"error": 3, "warning": 2, "info": 1}
        if isinstance(severity_raw, str) and severity_raw in _sev_map:
            severity = _sev_map[severity_raw]
        else:
            try:
                severity = int(severity_raw)
            except (ValueError, TypeError):
                severity = 1

        return CorrectionExample(
            context=context,
            original=(row.get("original_text") or ""),
            issue=issue,
            should_be=(row.get("corrected_text") or ""),
            severity=severity,
            corrected_at=(row.get("created_at") or ""),
        )
    except Exception:
        logger.debug("Failed to convert feedback row to CorrectionExample", exc_info=True)
        return None


# ============================================================
# M4: _load_from_experiences — 主源（group_experiences，curated 经验）
# ============================================================


async def _load_from_experiences(
    conn: aiosqlite.Connection,
    group_id: str,
    target_type: str | None,
    days: int,
    limit: int,
) -> list[CorrectionExample]:
    """M4: 从 group_experiences（active）加载经验为 CorrectionExample。

    经验家园是反馈派生、群绑定、可编辑、跨天的可召回经验句（L3，不进 MemOS）。
    与 feedback_events 分工：feedback_events=原始事件日志；group_experiences=派生
    可召回经验。本函数为主源；load_corrections 在无经验时回退 feedback_events。
    """
    params: list = [group_id, f"-{days} days"]
    where = ["group_id = ?", "status = 'active'", "created_at >= datetime('now', ?)"]
    if target_type is not None:
        where.append("target_type = ?")
        params.append(target_type)
    params.append(limit)
    sql = (
        "SELECT topic_name, target_type, lesson, created_at "
        "FROM group_experiences "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT ?"
    )
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()

    out: list[CorrectionExample] = []
    for row in rows:
        d = dict(row)
        context_parts = [p for p in [d.get("topic_name"), d.get("target_type")] if p]
        out.append(
            CorrectionExample(
                context=" / ".join(context_parts),
                original="",
                issue="",
                should_be=d.get("lesson") or "",
                severity=2,  # curated 经验，固定中优先级
                corrected_at=d.get("created_at") or "",
            )
        )
    return out


# ============================================================
# M4+: _load_consumed_feedback_for_date — date 维度主源（跟着对应日报走）
# ============================================================


async def _load_consumed_feedback_for_date(
    conn: aiosqlite.Connection,
    group_id: str,
    date: str,
    limit: int,
) -> list[CorrectionExample]:
    """按 date 拉针对该日报、且已消费的 feedback_events，转 CorrectionExample。

    生成 date=X 的日报时，把之前对该日报提过、且已据此重生成
    (consumed_at IS NOT NULL) 的反馈作为历史纠正示例注入 <prior_corrections>。
    与 feedback_hints（未消费反馈，走 get_unconsumed_feedback）职责互补、不重叠：
    已消费 = 上次对该日报的纠正（历史经验）；未消费 = 新提的、本次要注意的。

    日期格式兼容：feedback_events.date 存 YYYY-MM-DD（前端 normDate），
    report_versions.date 存 YYYYMMDD，两种都查。
    """
    if not date:
        return []

    date_compact = date.replace("-", "")
    date_dashed = (
        f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"
        if date_compact and len(date_compact) == 8 and date_compact.isdigit()
        else date
    )

    query = """
        SELECT
            target_type, target_id, original_text, corrected_text,
            tags, severity, correction_note, created_at
        FROM feedback_events
        WHERE group_id = ?
          AND date IN (?, ?)
          AND consumed_at IS NOT NULL
          AND correction_mode IN ('free_text', 'inline_edit')
          AND corrected_text IS NOT NULL
          AND corrected_text != ''
          AND (status IS NULL OR status = 'active')
        ORDER BY CASE severity WHEN 'error' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC,
                 created_at DESC
        LIMIT ?
    """
    cursor = await conn.execute(query, (group_id, date_compact, date_dashed, limit))
    rows = await cursor.fetchall()

    out: list[CorrectionExample] = []
    for row in rows:
        example = _row_to_example(dict(row))
        if example is not None:  # A008: skip malformed rows
            out.append(example)
    return out


def _dedupe(examples: list[CorrectionExample]) -> list[CorrectionExample]:
    """按 should_be 前缀去重，保留先出现者（date 模式下反馈优先于群级经验）。"""
    seen: set[str] = set()
    out: list[CorrectionExample] = []
    for ex in examples:
        key = (ex.should_be or "").strip().lower()[:80]
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(ex)
    return out


# ============================================================
# load_corrections — main public API
# ============================================================


async def load_corrections(
    group_id: str,
    date: str | None = None,
    target_type: str | None = None,
    days: int = 30,
    limit: int = 10,
) -> list[CorrectionExample]:
    """Load historical correction examples from feedback_events.

    Queries the feedback_events table for corrections matching the given
    group_id, recency window, and optional target_type filter. Results are
    sorted by severity (descending) then created_at (descending), and
    truncated to the specified limit.

    P009: Optional Parameter Cascading — target_type, days, limit each
    independently append to the WHERE clause and parameter list.

    A008: Defensive Init — results list is initialized before the query
    try block so an exception path never yields a NameError.

    Args:
        group_id: Per-group isolation — only load corrections for this group.
        date: Optional date (YYYYMMDD or YYYY-MM-DD). When provided, the main
            source switches to feedback_events already consumed for that day's
            report (跟着对应日报走); group_experiences becomes a small supplement.
            When None, falls back to group-wide experiences (legacy behavior).
        target_type: Optional filter (e.g. "report", "topic", "trend").
            When None, corrections of all target types are returned.
        days: Lookback window — only corrections within the last N days.
            Default 30.
        limit: Maximum number of CorrectionExample entries to return.
            Default 10.

    Returns:
        List of CorrectionExample entries, empty list if no matches found
        or the database is unavailable.

    Raises:
        Does not raise — returns [] on any error (graceful degradation).
    """
    # A008: defensive init — prevent NameError on exception path
    results: list[CorrectionExample] = []

    # Validate mandatory parameter
    if not group_id or not isinstance(group_id, str):
        logger.warning("load_corrections: invalid group_id=%r, returning []", group_id)
        return results

    # Resolve database path
    db_path: str = get_settings().sqlite_db_path

    if not Path(db_path).exists():
        logger.debug("load_corrections: database not found at %s", db_path)
        return results

    try:
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row

            # M4+: date 模式 — 主源 = 针对该日报、已消费的原始反馈（跟着对应日报走）。
            # 与 feedback_hints（未消费反馈，走 get_unconsumed_feedback）职责互补不重叠。
            if date:
                results = await _load_consumed_feedback_for_date(
                    conn, group_id, date, limit
                )

            # 群级经验（group_experiences）：date 模式下作为补充（不独占），
            # 非 date 模式下仍为主源（向后兼容）。
            exp_limit = max(0, limit - len(results)) if date else limit
            if exp_limit > 0:
                results.extend(
                    await _load_from_experiences(
                        conn, group_id, target_type, days, exp_limit
                    )
                )

            # 非 date 模式 + 仍无经验 → fallback 群级 feedback_events 原始纠正
            if not date and not results:
                query, params = _build_query(group_id, target_type, days, limit)
                cursor = await conn.execute(query, params)
                rows = await cursor.fetchall()
                for row in rows:
                    example = _row_to_example(dict(row))
                    if example is not None:  # A008: skip malformed rows
                        results.append(example)

            # 去重（date 模式下反馈与群级经验可能表述相近，反馈优先）
            if results:
                results = _dedupe(results)

            logger.debug(
                "load_corrections: group=%s date=%s target=%s days=%d limit=%d → %d results",
                group_id,
                date,
                target_type,
                days,
                limit,
                len(results),
            )

    except aiosqlite.Error as e:
        logger.warning("load_corrections: SQLite query failed: %s", e)
    except Exception:
        logger.warning("load_corrections: unexpected error", exc_info=True)

    return results
