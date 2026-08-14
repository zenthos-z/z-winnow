"""T-W14-4: Feedback state machine service.

Wraps the feedback_events table CRUD with state transitions:
  unconsumed (consumed_at IS NULL) → consumed (SET consumed_at/consumed_by)
  consumed → rollback (CLEAR consumed_at/consumed_by)

All functions accept an ``aiosqlite.Connection`` via dependency injection.
No FastAPI imports.  Uses real SQL — P078: no database mocks.

Patterns applied:
  A008: All data variables initialized before try blocks
  L018: Explicit state transitions, not silent empty results
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def create_feedback(
    db: aiosqlite.Connection,
    *,
    feedback_id: str,
    group_id: str,
    date: str,
    target_type: str,
    signal: str,
    report_id: str | None = None,
    target_id: str | None = None,
    target_path: str | None = None,
    target_version_id: str | None = None,
    target_topic_id: str | None = None,
    severity: str = "info",
    rating: str | None = None,
    tags: str | None = None,
    correction_mode: str | None = None,
    original_text: str | None = None,
    corrected_text: str | None = None,
    correction_note: str | None = None,
    reporter: str | None = None,
) -> bool:
    """Insert a new feedback event into feedback_events table.

    Args:
        db: Async SQLite connection.
        feedback_id: UUID primary key.
        group_id: Group identifier.
        date: Date string (YYYYMMDD).
        target_type: Target type (e.g. topic, section).
        signal: Signal type (e.g. correction, approval).
        report_id: Optional report reference.
        target_id: Optional target identifier.
        target_path: Optional target path.
        target_version_id: M4 被反馈的日报版本 id（溯源定位）。
        target_topic_id: M4 议题级反馈时的被反馈议题 id。
        severity: Severity level (default: info).
        rating: Optional rating.
        tags: Optional JSON tags.
        correction_mode: Optional correction mode.
        original_text: Optional original text.
        corrected_text: Optional corrected text.
        correction_note: Optional correction note.
        reporter: Optional reporter name.

    Returns:
        True if insert succeeded.
    """
    # A008
    success: bool = False
    try:
        cursor = await db.execute(
            """INSERT INTO feedback_events
               (feedback_id, group_id, date, report_id, target_type, target_id,
                target_path, target_version_id, target_topic_id,
                signal, severity, rating, tags, correction_mode,
                original_text, corrected_text, correction_note, reporter)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feedback_id,
                group_id,
                date,
                report_id,
                target_type,
                target_id,
                target_path,
                target_version_id,
                target_topic_id,
                signal,
                severity,
                rating,
                tags,
                correction_mode,
                original_text,
                corrected_text,
                correction_note,
                reporter,
            ),
        )
        await db.commit()
        success = cursor.rowcount > 0
    except Exception:
        logger.exception("feedback_service.create_feedback failed for id=%s", feedback_id)
    return success


async def list_unconsumed_feedback(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
) -> list[dict[str, Any]]:
    """List all unconsumed feedback events for a group + date.

    Args:
        db: Async SQLite connection.
        group_id: Group identifier.
        date: Date string (YYYYMMDD).

    Returns:
        List of feedback event dicts where consumed_at IS NULL.
    """
    # A008
    results: list[dict[str, Any]] = []
    try:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM feedback_events
               WHERE group_id = ? AND date = ? AND consumed_at IS NULL
               ORDER BY created_at DESC""",
            (group_id, date),
        )
        results = [dict(r) for r in await cursor.fetchall()]
    except Exception:
        logger.exception(
            "feedback_service.list_unconsumed_feedback failed for group=%s date=%s",
            group_id,
            date,
        )
    return results


async def get_feedback_by_id(
    db: aiosqlite.Connection,
    feedback_id: str,
) -> dict[str, Any] | None:
    """Retrieve a single feedback event by primary key.

    Args:
        db: Async SQLite connection.
        feedback_id: Primary key of the feedback event.

    Returns:
        Dict of all columns if found, None otherwise.
    """
    # A008
    result: dict[str, Any] | None = None
    try:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM feedback_events WHERE feedback_id = ?",
            (feedback_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            result = dict(row)
    except Exception:
        logger.exception("feedback_service.get_feedback_by_id failed for id=%s", feedback_id)
    return result


async def get_feedback_provenance(
    db: aiosqlite.Connection,
    feedback_id: str,
) -> dict[str, Any] | None:
    """M4: 组装反馈四元组溯源 —— feedback 本体 + target 版本议题 + produced 版本 + memos 双节点。

    各部分 best-effort：版本/节点缺失时对应字段为 None，不阻断。
    """
    from z_winnow.config.settings import get_settings
    from z_winnow.pipeline.l3_paths import read_l3_json
    from z_winnow.pipeline.report_version import get_version

    fb = await get_feedback_by_id(db, feedback_id)
    if fb is None:
        return None

    settings = get_settings()
    root = settings.layer3_output_dir

    def _version_summary(v) -> dict[str, Any] | None:
        if v is None:
            return None
        return {
            "version_id": v.version_id,
            "version_number": v.version_number,
            "is_active": bool(v.is_active),
            "source": v.source,
            "created_at": v.created_at,
        }

    # ① target 版本 + 该版本 topics.json 里 target_topic_id 的议题内容
    target_version_id = fb.get("target_version_id")
    target_version_summary: dict[str, Any] | None = None
    target_topic: dict[str, Any] | None = None
    if target_version_id:
        tv = await get_version(db, target_version_id)
        target_version_summary = _version_summary(tv)
        if tv:
            topics_data = read_l3_json(
                root, tv.group_id, tv.date, "topics", version_number=tv.version_number
            )
            wanted = fb.get("target_topic_id")
            for t in (topics_data or {}).get("topics", []):
                if isinstance(t, dict) and (not wanted or t.get("topic_id") == wanted):
                    target_topic = t
                    break

    # ② produced 版本 + daily.json
    produced_version_id = fb.get("produced_version_id")
    produced_version_summary: dict[str, Any] | None = None
    produced_daily: dict[str, Any] | None = None
    if produced_version_id:
        pv = await get_version(db, produced_version_id)
        produced_version_summary = _version_summary(pv)
        if pv:
            produced_daily = read_l3_json(
                root, pv.group_id, pv.date, "daily", version_number=pv.version_number
            )

    # ③ memos 双节点（activated + archived）—— best-effort，MemOS 不可用则 None
    activated_memory: dict[str, Any] | None = None
    archived_memory: dict[str, Any] | None = None
    group_id = fb.get("group_id")
    activated_id = fb.get("memos_node_id")
    archived_id = fb.get("archived_memos_id")
    if activated_id or archived_id:
        try:
            from z_winnow.memory.factory import create_memos_adapter

            adapter = create_memos_adapter()
            if activated_id:
                r = await adapter.get_memory(activated_id, group_id=group_id)
                if r:
                    activated_memory = {
                        "memory": r.memory,
                        "metadata": r.metadata,
                        "score": r.score,
                    }
            if archived_id:
                r = await adapter.get_memory(archived_id, group_id=group_id)
                if r:
                    archived_memory = {"memory": r.memory, "metadata": r.metadata, "score": r.score}
        except Exception as exc:
            logger.warning("get_feedback_provenance: memos lookup failed — %s", exc)

    return {
        "feedback": fb,
        "target_version": target_version_summary,
        "target_topic": target_topic,
        "produced_version": produced_version_summary,
        "produced_daily": produced_daily,
        "memos": {
            "cube_id": fb.get("memos_cube_id"),
            "activated_node_id": activated_id,
            "archived_node_id": archived_id,
            "activated_memory": activated_memory,
            "archived_memory": archived_memory,
        },
    }


async def consume_feedback(
    db: aiosqlite.Connection,
    feedback_id: str,
    *,
    consumed_by: str = "api",
) -> bool:
    """Mark a feedback event as consumed.

    Sets ``consumed_at`` to current UTC time and ``consumed_by`` to the
    provided identifier.  Only succeeds if the event exists and is not
    already consumed.

    Args:
        db: Async SQLite connection.
        feedback_id: Primary key of the feedback event.
        consumed_by: Identifier of the consumer (default: "api").

    Returns:
        True if exactly one row was updated, False otherwise.
    """
    # A008
    success: bool = False
    try:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = await db.execute(
            """UPDATE feedback_events
               SET consumed_at = ?, consumed_by = ?
               WHERE feedback_id = ? AND consumed_at IS NULL""",
            (now, consumed_by, feedback_id),
        )
        await db.commit()
        success = cursor.rowcount > 0
    except Exception:
        logger.exception("feedback_service.consume_feedback failed for id=%s", feedback_id)
    return success


async def rollback_feedback(
    db: aiosqlite.Connection,
    feedback_id: str,
) -> bool:
    """Roll back a consumed feedback event to unconsumed state.

    Clears ``consumed_at`` and ``consumed_by``.  Only succeeds if the
    event exists and is currently consumed (consumed_at IS NOT NULL).

    Args:
        db: Async SQLite connection.
        feedback_id: Primary key of the feedback event.

    Returns:
        True if exactly one row was updated, False otherwise.
    """
    # A008
    success: bool = False
    try:
        cursor = await db.execute(
            """UPDATE feedback_events
               SET consumed_at = NULL, consumed_by = NULL
               WHERE feedback_id = ? AND consumed_at IS NOT NULL""",
            (feedback_id,),
        )
        await db.commit()
        success = cursor.rowcount > 0
    except Exception:
        logger.exception("feedback_service.rollback_feedback failed for id=%s", feedback_id)
    return success
