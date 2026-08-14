"""T-W12-13: Test incremental_reprocess — feedback-driven incremental corrections.

P011: AC 1:1 test mapping — each B criterion has a dedicated test function.

Tests:
  B1: Incremental reprocessing only updates affected L3 records
  B2: L3->L2 mapping via source_server_ids works correctly
  B3: feedback_events state machine complete (unconsumed/consumed/rollback)
  B4: report_versions new version source = incremental_fix
  B5: L1/L2 data not modified by incremental reprocessing

No mocking of database — all tests use in-memory SQLite.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import aiosqlite
import pytest

from z_winnow.graph.builder import incremental_reprocess
from z_winnow.pipeline.database import (
    get_contexts_by_date,
    get_raw_messages_by_date,
    get_topics_by_date,
    init_database_in_conn,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_settings():
    """Mock settings to point to in-memory DB and disable real LLM/data source API."""
    with patch.dict(
        os.environ,
        {
            "WINNOW_SQLITE_DB_PATH": ":memory:",
            "WINNOW_DB_PATH": ":memory:",
            "WINNOW_REAL_LLM": "false",
            "WINNOW_MEMOS_ENABLED": "false",
            "WINNOW_FEISHU_ENABLED": "false",
        },
    ):
        # Reset settings singleton so new values take effect
        from z_winnow.config.settings import reset_settings

        reset_settings()
        yield
        reset_settings()


@pytest.fixture
async def seeded_db(mock_settings):
    """Create a seeded in-memory database for testing.

    Seeds:
      - L1: 3 raw messages
      - L2: 2 parsed_contexts
      - L3: 3 topic_summaries (only sum_001 has feedback)
      - feedback_events: 2 unconsumed (targeting sum_001 and sum_002)
    """

    # We need a file-based temp DB since incremental_reprocess opens its own connection
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        with patch.dict(
            os.environ,
            {
                "WINNOW_SQLITE_DB_PATH": db_path,
                "WINNOW_DB_PATH": db_path,
            },
        ):
            from z_winnow.config.settings import reset_settings

            reset_settings()

            async with aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)

                # L1: Raw messages
                for i, sid in enumerate(["msg_001", "msg_002", "msg_003"], 1):
                    await db.execute(
                        """INSERT INTO raw_messages
                           (serverID, date, sender, content, msg_type, sanitized, raw_json, group_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            sid,
                            "20260520",
                            f"user_{i}",
                            f"Message {i}",
                            "text",
                            0,
                            json.dumps({"server_id": sid, "content": f"Message {i}"}),
                            "test-group",
                        ),
                    )

                # L2: Parsed contexts
                await db.execute(
                    """INSERT INTO parsed_contexts
                       (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "ctx_001",
                        "20260520",
                        '["msg_001", "msg_002"]',
                        "Context about topic A and B",
                        100,
                        "daily_reporter",
                        "test-group",
                    ),
                )
                await db.execute(
                    """INSERT INTO parsed_contexts
                       (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "ctx_002",
                        "20260520",
                        '["msg_003"]',
                        "Context about topic C",
                        50,
                        "daily_reporter",
                        "test-group",
                    ),
                )

                # L3: Topic summaries
                for sid, name, sids, text in [
                    (
                        "sum_001",
                        "Topic A",
                        '["msg_001"]',
                        '{"topic_name": "Topic A", "summary": "Original A"}',
                    ),
                    (
                        "sum_002",
                        "Topic B",
                        '["msg_002"]',
                        '{"topic_name": "Topic B", "summary": "Original B"}',
                    ),
                    (
                        "sum_003",
                        "Topic C",
                        '["msg_003"]',
                        '{"topic_name": "Topic C", "summary": "Original C"}',
                    ),
                ]:
                    await db.execute(
                        """INSERT INTO topic_summaries
                           (summary_id, date, topic_name, summary_text, context_ids,
                            source_server_ids, confidence, model_used, group_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (sid, "20260520", name, text, "[]", sids, 0.9, "mock", "test-group"),
                    )

                # Feedback events (unconsumed)
                await db.execute(
                    """INSERT INTO feedback_events
                       (feedback_id, group_id, date, report_id, target_type, target_id,
                        signal, severity, tags, correction_mode, original_text,
                        corrected_text, correction_note, reporter)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "fb-001",
                        "test-group",
                        "20260520",
                        "test-group-20260520",
                        "topic",
                        "sum_001",
                        "negative",
                        "info",
                        '["fact_error"]',
                        "free_text",
                        "Original A",
                        "Corrected A",
                        "Fix A",
                        "admin",
                    ),
                )

                await db.commit()

            yield db_path

            reset_settings()


# ============================================================
# B1: Only affected L3 records are updated
# ============================================================


@pytest.mark.asyncio
async def test_b1_only_affected_l3_updated(seeded_db):
    """B1: Incremental reprocessing only updates affected L3 records.

    Setup: 3 L3 topics, 1 feedback targeting sum_001.
    Expect: sum_001 updated, sum_002 and sum_003 unchanged.
    """
    result = await incremental_reprocess("test-group", "20260520")
    assert result["processed"] == 1
    assert result["succeeded"] == 1

    # Verify only sum_001 was updated
    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        db.row_factory = aiosqlite.Row

        topics = await get_topics_by_date(db, "20260520")
        topic_map = {t["summary_id"]: t for t in topics}

        # sum_001 should have been updated
        assert "Incremental correction applied" in topic_map["sum_001"]["summary_text"]

        # sum_002 and sum_003 should be unchanged
        assert (
            topic_map["sum_002"]["summary_text"]
            == '{"topic_name": "Topic B", "summary": "Original B"}'
        )
        assert (
            topic_map["sum_003"]["summary_text"]
            == '{"topic_name": "Topic C", "summary": "Original C"}'
        )


# ============================================================
# B2: L3->L2 mapping via source_server_ids
# ============================================================


@pytest.mark.asyncio
async def test_b2_l3_to_l2_mapping(seeded_db):
    """B2: L3->L2 mapping via source_server_ids works correctly.

    Verify that source_server_ids from topic_summaries are used to
    find corresponding parsed_contexts.
    """
    from z_winnow.pipeline.database import get_l2_contexts_by_server_ids

    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)

        # sum_001 has source_server_ids = ["msg_001"]
        # ctx_001 has server_ids = ["msg_001", "msg_002"]
        contexts = await get_l2_contexts_by_server_ids(db, ["msg_001"])
        assert len(contexts) >= 1
        assert any(c["context_id"] == "ctx_001" for c in contexts)


# ============================================================
# B3: feedback state machine (covered in test_database.py, also verify here)
# ============================================================


@pytest.mark.asyncio
async def test_b3_feedback_consumed_after_reprocess(seeded_db):
    """B3: Feedback events are marked consumed after successful reprocessing.

    The feedback event for sum_001 should have consumed_at set after
    incremental_reprocess succeeds.
    """
    result = await incremental_reprocess("test-group", "20260520")
    assert result["succeeded"] == 1

    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT consumed_at, consumed_by FROM feedback_events WHERE feedback_id = 'fb-001'"
        )
        row = await cursor.fetchone()
        assert row["consumed_at"] is not None
        assert row["consumed_by"] == "incremental_reprocess"


@pytest.mark.asyncio
async def test_b3_feedback_stays_unconsumed_on_failure(mock_settings):
    """B3: Feedback stays unconsumed when processing fails.

    When target_id is not found, the feedback remains unconsumed.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        with patch.dict(
            os.environ,
            {
                "WINNOW_SQLITE_DB_PATH": db_path,
                "WINNOW_DB_PATH": db_path,
            },
        ):
            from z_winnow.config.settings import reset_settings

            reset_settings()

            async with aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)
                # Only feedback, no matching topic
                await db.execute(
                    """INSERT INTO feedback_events
                       (feedback_id, group_id, date, target_type, target_id,
                        signal, severity, tags, correction_mode, reporter)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "fb-no-target",
                        "grp",
                        "20260520",
                        "topic",
                        "sum_nonexistent",
                        "negative",
                        "info",
                        "[]",
                        "free_text",
                        "admin",
                    ),
                )
                await db.commit()

            result = await incremental_reprocess("grp", "20260520")
            assert result["processed"] == 1
            assert result["failed"] == 1
            assert result["succeeded"] == 0

            # Verify feedback is still unconsumed
            async with aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT consumed_at FROM feedback_events WHERE feedback_id = 'fb-no-target'"
                )
                row = await cursor.fetchone()
                assert row["consumed_at"] is None

            reset_settings()


# ============================================================
# B4: report_versions source = incremental_fix
# ============================================================


@pytest.mark.asyncio
async def test_b4_report_versions_source_incremental_fix(seeded_db):
    """B4: After incremental reprocess, report_versions has source=incremental_fix.

    Code inspection test: verifies the source field is correctly set.
    """
    result = await incremental_reprocess("test-group", "20260520")
    assert result["succeeded"] == 1

    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT source FROM report_versions
               WHERE report_id = 'test-group-20260520'
               ORDER BY version_number DESC LIMIT 1"""
        )
        row = await cursor.fetchone()
        assert row is not None, "report_versions should have a new entry"
        assert row["source"] == "incremental_fix"


# ============================================================
# B5: L1/L2 data not modified
# ============================================================


@pytest.mark.asyncio
async def test_b5_l1_l2_unchanged(seeded_db):
    """B5: L1/L2 data is not modified by incremental reprocessing.

    Captures L1/L2 state before reprocess, then compares after.
    """
    # Capture L1/L2 state before
    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        l1_before = await get_raw_messages_by_date(db, "20260520")
        l2_before = await get_contexts_by_date(db, "20260520")

    # Run incremental reprocess
    result = await incremental_reprocess("test-group", "20260520")
    assert result["succeeded"] == 1

    # Verify L1/L2 unchanged
    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        l1_after = await get_raw_messages_by_date(db, "20260520")
        l2_after = await get_contexts_by_date(db, "20260520")

    # L1 comparison
    assert len(l1_before) == len(l1_after)
    for before, after in zip(l1_before, l1_after, strict=False):
        assert before["serverID"] == after["serverID"]
        assert before["content"] == after["content"]
        assert before["sender"] == after["sender"]

    # L2 comparison
    assert len(l2_before) == len(l2_after)
    for before, after in zip(l2_before, l2_after, strict=False):
        assert before["context_id"] == after["context_id"]
        assert before["context_text"] == after["context_text"]
        assert before["server_ids"] == after["server_ids"]


# ============================================================
# Additional: dry_run mode
# ============================================================


@pytest.mark.asyncio
async def test_dry_run_does_not_modify_db(seeded_db):
    """dry_run=True should not modify any database records."""
    result = await incremental_reprocess("test-group", "20260520", dry_run=True)
    assert result["processed"] == 1
    assert result["succeeded"] == 1

    # Verify topic NOT updated
    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT summary_text FROM topic_summaries WHERE summary_id = 'sum_001'"
        )
        row = await cursor.fetchone()
        assert row["summary_text"] == '{"topic_name": "Topic A", "summary": "Original A"}'

    # Verify feedback NOT consumed
    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT consumed_at FROM feedback_events WHERE feedback_id = 'fb-001'"
        )
        row = await cursor.fetchone()
        assert row["consumed_at"] is None


# ============================================================
# Additional: no unconsumed feedback -> no-op
# ============================================================


@pytest.mark.asyncio
async def test_no_unconsumed_feedback_noop(seeded_db):
    """When no unconsumed feedback exists, returns empty result."""
    # Mark the existing feedback as consumed
    async with aiosqlite.connect(seeded_db) as db:
        await init_database_in_conn(db)
        await db.execute(
            "UPDATE feedback_events SET consumed_at = '2026-05-20' WHERE feedback_id = 'fb-001'"
        )
        await db.commit()

    result = await incremental_reprocess("test-group", "20260520")
    assert result["processed"] == 0
    assert result["succeeded"] == 0
    assert result["failed"] == 0


# ============================================================
# Additional: L014 — single record failure does not block others
# ============================================================


@pytest.mark.asyncio
async def test_l014_single_failure_does_not_block(mock_settings):
    """L014: asyncio.gather exception handling — single failure doesn't block."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        with patch.dict(
            os.environ,
            {
                "WINNOW_SQLITE_DB_PATH": db_path,
                "WINNOW_DB_PATH": db_path,
            },
        ):
            from z_winnow.config.settings import reset_settings

            reset_settings()

            async with aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)

                # One valid topic
                await db.execute(
                    """INSERT INTO topic_summaries
                       (summary_id, date, topic_name, summary_text, context_ids,
                        source_server_ids, confidence, model_used, group_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "sum_valid",
                        "20260520",
                        "Valid Topic",
                        '{"summary": "Valid"}',
                        "[]",
                        "[]",
                        0.9,
                        "mock",
                        "grp",
                    ),
                )

                # Two feedback: one targeting valid, one targeting nonexistent
                for fb_id, target_id in [
                    ("fb-bad", "sum_nonexistent"),
                    ("fb-good", "sum_valid"),
                ]:
                    await db.execute(
                        """INSERT INTO feedback_events
                           (feedback_id, group_id, date, target_type, target_id,
                            signal, severity, tags, correction_mode, corrected_text, reporter)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            fb_id,
                            "grp",
                            "20260520",
                            "topic",
                            target_id,
                            "negative",
                            "info",
                            "[]",
                            "free_text",
                            "Fix",
                            "admin",
                        ),
                    )
                await db.commit()

            result = await incremental_reprocess("grp", "20260520")
            assert result["processed"] == 2
            assert result["succeeded"] == 1
            assert result["failed"] == 1

            reset_settings()
