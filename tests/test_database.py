"""T-W12-13: Test new database functions for incremental reprocessing.

Tests the 5 new database functions added for S5 incremental reprocessing:
  - get_unconsumed_feedback
  - get_l2_contexts_by_server_ids
  - mark_feedback_consumed
  - mark_feedback_rollback
  - update_topic_summary_text

Uses in-memory SQLite (aiosqlite.connect(":memory:")) for isolation.
No mocking of database — real SQL operations on in-memory DB.
"""

from __future__ import annotations

import aiosqlite
import pytest

from z_winnow.pipeline.database import (
    get_l2_contexts_by_server_ids,
    get_unconsumed_feedback,
    init_database_in_conn,
    mark_feedback_consumed,
    mark_feedback_rollback,
    update_topic_summary_text,
)


@pytest.fixture
async def db():
    """Create an in-memory SQLite database with full schema initialized."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        yield conn


# ============================================================
# Helper: seed test data
# ============================================================


async def _seed_feedback(db: aiosqlite.Connection, **overrides) -> str:
    """Insert a test feedback event and return the feedback_id."""
    import uuid

    fb_id = overrides.pop("feedback_id", f"fb-test-{uuid.uuid4().hex[:8]}")
    row = {
        "feedback_id": fb_id,
        "group_id": "test-group",
        "date": "20260520",
        "report_id": "test-group-20260520",
        "target_type": "topic",
        "target_id": "sum_001",
        "target_path": None,
        "signal": "negative",
        "severity": "info",
        "rating": None,
        "tags": '["fact_error"]',
        "correction_mode": "free_text",
        "original_text": "Original content",
        "corrected_text": "Corrected content",
        "correction_note": "Fix factual error",
        "reporter": "admin",
        "consumed_at": None,
        "consumed_by": None,
    }
    row.update(overrides)
    await db.execute(
        """INSERT INTO feedback_events
           (feedback_id, group_id, date, report_id, target_type, target_id,
            target_path, signal, severity, rating, tags, correction_mode,
            original_text, corrected_text, correction_note, reporter,
            consumed_at, consumed_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["feedback_id"],
            row["group_id"],
            row["date"],
            row["report_id"],
            row["target_type"],
            row["target_id"],
            row["target_path"],
            row["signal"],
            row["severity"],
            row["rating"],
            row["tags"],
            row["correction_mode"],
            row["original_text"],
            row["corrected_text"],
            row["correction_note"],
            row["reporter"],
            row["consumed_at"],
            row["consumed_by"],
        ),
    )
    await db.commit()
    return fb_id


async def _seed_parsed_context(db: aiosqlite.Connection, **overrides) -> str:
    """Insert a test parsed_context and return the context_id."""
    import uuid

    ctx_id = overrides.pop("context_id", f"ctx_{uuid.uuid4().hex[:8]}")
    await db.execute(
        """INSERT INTO parsed_contexts
           (context_id, date, server_ids, context_text, token_count, source_subagent)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            ctx_id,
            overrides.get("date", "20260520"),
            overrides.get("server_ids", '["msg_001", "msg_002"]'),
            overrides.get("context_text", "Test context content"),
            overrides.get("token_count", 100),
            overrides.get("source_subagent", "daily_reporter"),
        ),
    )
    await db.commit()
    return ctx_id


async def _seed_topic_summary(db: aiosqlite.Connection, **overrides) -> str:
    """Insert a test topic_summary and return the summary_id."""
    import uuid

    sum_id = overrides.pop("summary_id", f"sum_{uuid.uuid4().hex[:8]}")
    await db.execute(
        """INSERT INTO topic_summaries
           (summary_id, date, topic_name, summary_text, context_ids,
            source_server_ids, confidence, model_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sum_id,
            overrides.get("date", "20260520"),
            overrides.get("topic_name", "Test Topic"),
            overrides.get("summary_text", '{"topic_name": "Test", "summary": "Test summary"}'),
            overrides.get("context_ids", "[]"),
            overrides.get("source_server_ids", '["msg_001", "msg_002"]'),
            overrides.get("confidence", 0.9),
            overrides.get("model_used", "mock"),
        ),
    )
    await db.commit()
    return sum_id


# ============================================================
# Tests: get_unconsumed_feedback
# ============================================================


@pytest.mark.asyncio
async def test_get_unconsumed_feedback_empty(db):
    """No feedback events -> empty list."""
    result = await get_unconsumed_feedback(db, "test-group", "20260520")
    assert result == []


@pytest.mark.asyncio
async def test_get_unconsumed_feedback_returns_unconsumed(db):
    """Returns only unconsumed feedback events."""
    await _seed_feedback(db, feedback_id="fb-001")
    await _seed_feedback(db, feedback_id="fb-002")
    # Mark one as consumed
    await db.execute(
        "UPDATE feedback_events SET consumed_at = '2026-05-20' WHERE feedback_id = 'fb-001'"
    )
    await db.commit()

    result = await get_unconsumed_feedback(db, "test-group", "20260520")
    assert len(result) == 1
    assert result[0]["feedback_id"] == "fb-002"
    assert result[0]["consumed_at"] is None


@pytest.mark.asyncio
async def test_get_unconsumed_feedback_filters_by_group_date(db):
    """Only returns feedback for the specified group and date."""
    await _seed_feedback(db, feedback_id="fb-a", group_id="group-a", date="20260520")
    await _seed_feedback(db, feedback_id="fb-b", group_id="group-b", date="20260520")
    await _seed_feedback(db, feedback_id="fb-c", group_id="group-a", date="20260519")

    result = await get_unconsumed_feedback(db, "group-a", "20260520")
    assert len(result) == 1
    assert result[0]["feedback_id"] == "fb-a"


# ============================================================
# Tests: get_l2_contexts_by_server_ids
# ============================================================


@pytest.mark.asyncio
async def test_get_l2_contexts_empty_server_ids(db):
    """Empty server_ids list -> empty result."""
    result = await get_l2_contexts_by_server_ids(db, [])
    assert result == []


@pytest.mark.asyncio
async def test_get_l2_contexts_finds_matching(db):
    """Finds parsed_contexts containing the specified server_ids."""
    await _seed_parsed_context(
        db,
        context_id="ctx_001",
        server_ids='["msg_001", "msg_002"]',
    )
    await _seed_parsed_context(
        db,
        context_id="ctx_002",
        server_ids='["msg_003", "msg_004"]',
    )

    result = await get_l2_contexts_by_server_ids(db, ["msg_001"])
    assert len(result) == 1
    assert result[0]["context_id"] == "ctx_001"


@pytest.mark.asyncio
async def test_get_l2_contexts_deduplicates(db):
    """L005: Deduplicates results when multiple server_ids match same context."""
    await _seed_parsed_context(
        db,
        context_id="ctx_001",
        server_ids='["msg_001", "msg_002"]',
    )

    result = await get_l2_contexts_by_server_ids(db, ["msg_001", "msg_002"])
    assert len(result) == 1  # Same context returned only once
    assert result[0]["context_id"] == "ctx_001"


@pytest.mark.asyncio
async def test_get_l2_contexts_no_match(db):
    """No matching contexts -> empty list."""
    await _seed_parsed_context(db, context_id="ctx_001", server_ids='["msg_001"]')

    result = await get_l2_contexts_by_server_ids(db, ["msg_999"])
    assert result == []


# ============================================================
# Tests: mark_feedback_consumed
# ============================================================


@pytest.mark.asyncio
async def test_mark_feedback_consumed_success(db):
    """Successfully marks unconsumed feedback as consumed."""
    fb_id = await _seed_feedback(db, feedback_id="fb-consume-001")
    result = await mark_feedback_consumed(db, fb_id)
    assert result is True

    # Verify in DB
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT consumed_at, consumed_by FROM feedback_events WHERE feedback_id = ?",
        (fb_id,),
    )
    row = await cursor.fetchone()
    assert row["consumed_at"] is not None
    assert row["consumed_by"] == "incremental_reprocess"


@pytest.mark.asyncio
async def test_mark_feedback_consumed_already_consumed(db):
    """Attempting to consume already-consumed feedback returns False."""
    fb_id = await _seed_feedback(db, feedback_id="fb-consume-002")
    await mark_feedback_consumed(db, fb_id)

    # Second call should return False (already consumed)
    result = await mark_feedback_consumed(db, fb_id)
    assert result is False


@pytest.mark.asyncio
async def test_mark_feedback_consumed_not_found(db):
    """Non-existent feedback_id returns False."""
    result = await mark_feedback_consumed(db, "fb-nonexistent")
    assert result is False


# ============================================================
# Tests: mark_feedback_rollback
# ============================================================


@pytest.mark.asyncio
async def test_mark_feedback_rollback_success(db):
    """Successfully rolls back consumed feedback to unconsumed."""
    fb_id = await _seed_feedback(db, feedback_id="fb-rollback-001")
    await mark_feedback_consumed(db, fb_id)

    # Rollback
    result = await mark_feedback_rollback(db, fb_id)
    assert result is True

    # Verify in DB
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT consumed_at, consumed_by FROM feedback_events WHERE feedback_id = ?",
        (fb_id,),
    )
    row = await cursor.fetchone()
    assert row["consumed_at"] is None
    assert row["consumed_by"] is None


@pytest.mark.asyncio
async def test_mark_feedback_rollback_already_unconsumed(db):
    """Attempting to rollback unconsumed feedback returns False."""
    fb_id = await _seed_feedback(db, feedback_id="fb-rollback-002")
    # Not consumed yet, rollback should return False
    result = await mark_feedback_rollback(db, fb_id)
    assert result is False


# ============================================================
# Tests: update_topic_summary_text
# ============================================================


@pytest.mark.asyncio
async def test_update_topic_summary_text_success(db):
    """A002: Actually writes to DB (execute + commit)."""
    sum_id = await _seed_topic_summary(
        db,
        summary_id="sum_update_001",
        summary_text='{"old": true}',
    )
    new_text = '{"topic_name": "Updated", "summary": "Updated content"}'

    result = await update_topic_summary_text(db, sum_id, new_text)
    assert result is True

    # Verify in DB
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT summary_text FROM topic_summaries WHERE summary_id = ?",
        (sum_id,),
    )
    row = await cursor.fetchone()
    assert row["summary_text"] == new_text


@pytest.mark.asyncio
async def test_update_topic_summary_text_not_found(db):
    """Non-existent summary_id returns False."""
    result = await update_topic_summary_text(db, "sum_nonexistent", "text")
    assert result is False


# ============================================================
# Tests: feedback state machine (B3 coverage)
# ============================================================


@pytest.mark.asyncio
async def test_feedback_state_machine_full_cycle(db):
    """B3: Full state machine cycle: unconsumed -> consumed -> rollback -> consumed."""
    fb_id = await _seed_feedback(db, feedback_id="fb-sm-001")

    # State 1: unconsumed
    events = await get_unconsumed_feedback(db, "test-group", "20260520")
    assert len(events) == 1

    # Transition: unconsumed -> consumed
    ok = await mark_feedback_consumed(db, fb_id)
    assert ok is True
    events = await get_unconsumed_feedback(db, "test-group", "20260520")
    assert len(events) == 0  # No longer unconsumed

    # Transition: consumed -> unconsumed (rollback)
    ok = await mark_feedback_rollback(db, fb_id)
    assert ok is True
    events = await get_unconsumed_feedback(db, "test-group", "20260520")
    assert len(events) == 1  # Back to unconsumed

    # Transition: unconsumed -> consumed again
    ok = await mark_feedback_consumed(db, fb_id)
    assert ok is True
    events = await get_unconsumed_feedback(db, "test-group", "20260520")
    assert len(events) == 0


# ============================================================
# migrate_topic_summaries_split_conclusion — 因果链三段拆分迁移
# P052 幂等 + P014 never-throw
# ============================================================


async def _topic_summary_columns(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute("PRAGMA table_info(topic_summaries)")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def test_split_conclusion_migration_adds_columns_on_fresh_db():
    """init_database_in_conn 在新库上应建出 background/process 列。"""
    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        cols = await _topic_summary_columns(conn)
        assert "background" in cols
        assert "process" in cols
        assert "conclusion" in cols


async def test_split_conclusion_migration_is_idempotent_on_legacy_table():
    """旧库（无 background/process 列）跑迁移应补列；再跑一次不报错（P052 幂等）。"""
    from z_winnow.pipeline.database import (
        migrate_topic_summaries_split_conclusion,
    )

    async with aiosqlite.connect(":memory:") as conn:
        # 模拟旧库：手动建一个只有 conclusion（无 background/process）的表
        await conn.execute(
            """
            CREATE TABLE topic_summaries (
                summary_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                group_id TEXT DEFAULT '',
                topic_name TEXT NOT NULL,
                topic_id TEXT DEFAULT '',
                summary_text TEXT NOT NULL,
                context_ids TEXT NOT NULL,
                source_server_ids TEXT NOT NULL,
                confidence REAL,
                model_used TEXT,
                lifecycle TEXT DEFAULT 'emerging',
                matched_core_topic_id TEXT,
                conclusion TEXT DEFAULT '',
                description TEXT DEFAULT '',
                participants TEXT DEFAULT '',
                trend TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await conn.commit()
        cols_before = await _topic_summary_columns(conn)
        assert "background" not in cols_before
        assert "process" not in cols_before

        # 第一次迁移：补列
        await migrate_topic_summaries_split_conclusion(conn)
        cols_after = await _topic_summary_columns(conn)
        assert "background" in cols_after
        assert "process" in cols_after

        # 第二次迁移：幂等，不抛异常
        await migrate_topic_summaries_split_conclusion(conn)
        cols_repeat = await _topic_summary_columns(conn)
        assert "background" in cols_repeat
        assert "process" in cols_repeat
