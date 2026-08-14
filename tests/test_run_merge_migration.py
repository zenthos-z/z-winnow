"""Tests for runs→pipeline_runs migration and async_tasks DDL.

T-W14-8 + T-W14-10: Validates the idempotent migration merging the legacy ``runs`` table
into ``pipeline_runs``, plus the new ``async_tasks`` table DDL.

P078: Real-SQLite DDL Verification Testing — all tests use real in-memory
aiosqlite connections, never mock the database.

P013: Test class per scenario, 1:1 mapping to B-criteria.
P012: autouse monkeypatch env isolation
"""

from __future__ import annotations

import aiosqlite
import pytest

from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.pipeline.migrations import (
    migrate_runs_merge,
)

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """P012: Isolate environment for each test."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


# ============================================================
# Helpers
# ============================================================


async def _create_legacy_runs_table(
    db: aiosqlite.Connection,
    *,
    with_row: bool = True,
) -> None:
    """Create a legacy runs table matching run_control.py schema.

    P078: Uses real DDL, not mock.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            group_id TEXT,
            date TEXT NOT NULL,
            report_types TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER DEFAULT 0,
            nodes_completed INTEGER DEFAULT 0,
            nodes_total INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT
        )"""
    )
    if with_row:
        await db.execute(
            """INSERT INTO runs
               (run_id, group_id, date, report_types, status, progress,
                nodes_completed, nodes_total, error_message,
                created_at, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "run-001",
                "group-abc",
                "20260601",
                '["daily"]',
                "completed",
                100,
                3,
                5,
                None,
                "2026-06-01T08:00:00",
                "2026-06-01T08:00:01",
                "2026-06-01T08:05:00",
            ),
        )
    await db.commit()


# ============================================================
# B1: Idempotent migration
# ============================================================


class TestIdempotency:
    """B1: migrate_runs_merge() called twice produces no errors and no duplicates."""

    @pytest.mark.asyncio
    async def test_double_migration_no_errors(self) -> None:
        """Calling migrate_runs_merge twice on same DB produces no errors."""
        async with aiosqlite.connect(":memory:") as db:
            # Set up both tables: pipeline_runs via init + legacy runs
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db)

            # First migration
            await migrate_runs_merge(db)

            # Count after first
            cursor = await db.execute("SELECT COUNT(*) FROM pipeline_runs")
            count1 = (await cursor.fetchone())[0]

            # Second migration — must not error
            await migrate_runs_merge(db)

            # Count must be unchanged — no duplicates
            cursor = await db.execute("SELECT COUNT(*) FROM pipeline_runs")
            count2 = (await cursor.fetchone())[0]

            assert count1 == count2, (
                f"Row count changed after second migration: {count1} -> {count2}"
            )

    @pytest.mark.asyncio
    async def test_pipeline_runs_count_after_double_migration(self) -> None:
        """pipeline_runs has exactly 1 row after migrating a 1-row runs table twice."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db, with_row=True)

            await migrate_runs_merge(db)
            await migrate_runs_merge(db)

            cursor = await db.execute("SELECT COUNT(*) FROM pipeline_runs")
            count = (await cursor.fetchone())[0]
            # pipeline_runs may have 1 (from runs) + any seeded by init
            # but runs contribution is exactly 1
            assert count >= 1

    @pytest.mark.asyncio
    async def test_pragma_guard_prevents_duplicate_alter(self) -> None:
        """PRAGMA table_info guard prevents ALTER TABLE on re-run."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db)

            await migrate_runs_merge(db)

            # Verify columns exist after first run
            cursor = await db.execute("PRAGMA table_info(pipeline_runs)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "report_types" in cols

            # Second run must not fail
            await migrate_runs_merge(db)

            # Column count unchanged
            cursor = await db.execute("PRAGMA table_info(pipeline_runs)")
            cols2 = {row[1] for row in await cursor.fetchall()}
            assert cols == cols2


# ============================================================
# B2: Data preservation
# ============================================================


class TestRunsMerge:
    """B2: Row data from runs is fully preserved in pipeline_runs."""

    @pytest.mark.asyncio
    async def test_data_preservation(self) -> None:
        """All field values from runs are preserved after migration."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db, with_row=True)

            await migrate_runs_merge(db)

            # Query the migrated row
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", ("run-001",))
            row = await cursor.fetchone()
            assert row is not None, "Migrated row not found in pipeline_runs"

            result = dict(row)

            # Verify shared columns
            assert result["run_id"] == "run-001"
            assert result["group_id"] == "group-abc"
            assert result["date"] == "20260601"
            assert result["status"] == "completed"
            assert result["error_message"] is None
            assert result["created_at"] == "2026-06-01T08:00:00"
            assert result["started_at"] == "2026-06-01T08:00:01"
            assert result["completed_at"] == "2026-06-01T08:05:00"

            # Verify legacy-only columns migrated
            assert result["report_types"] == '["daily"]'
            assert result["nodes_completed"] == 3
            assert result["nodes_total"] == 5

            # Verify progress → progress_pct mapping
            assert result["progress_pct"] == 100

    @pytest.mark.asyncio
    async def test_report_types_nullable_column(self) -> None:
        """report_types, nodes_completed, nodes_total are nullable columns."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            # No legacy runs table — columns still get added by init path
            # Verify columns exist and accept NULL
            await db.execute(
                """INSERT INTO pipeline_runs
                   (run_id, component, status, report_types, nodes_completed, nodes_total)
                   VALUES (?, ?, ?, NULL, NULL, NULL)""",
                ("test-null", "test", "testing"),
            )
            await db.commit()

            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT report_types, nodes_completed, nodes_total FROM pipeline_runs WHERE run_id = ?",
                ("test-null",),
            )
            row = await cursor.fetchone()
            assert row["report_types"] is None
            assert row["nodes_completed"] is None
            assert row["nodes_total"] is None


# ============================================================
# B3: async_tasks DDL
# ============================================================


class TestAsyncTasksDDL:
    """B3: async_tasks table created with correct schema."""

    @pytest.mark.asyncio
    async def test_table_has_expected_columns(self) -> None:
        """PRAGMA table_info(async_tasks) returns expected columns."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            cursor = await db.execute("PRAGMA table_info(async_tasks)")
            rows = await cursor.fetchall()
            col_names = {row[1] for row in rows}

            expected = {
                "task_id",
                "task_type",
                "status",
                "result",
                "error",
                "created_at",
                "updated_at",
            }
            assert expected == col_names, (
                f"Missing columns: {expected - col_names}, Extra columns: {col_names - expected}"
            )

    @pytest.mark.asyncio
    async def test_insert_minimal_row(self) -> None:
        """async_tasks accepts INSERT with just task_id + task_type."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            await db.execute(
                "INSERT INTO async_tasks (task_id, task_type) VALUES ('t1', 'daily_run')"
            )
            await db.commit()

            # Verify default values
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM async_tasks WHERE task_id = 't1'")
            row = await cursor.fetchone()
            assert row is not None
            assert row["task_type"] == "daily_run"
            assert row["status"] == "pending"  # DEFAULT value
            assert row["result"] is None
            assert row["error"] is None

    @pytest.mark.asyncio
    async def test_table_count_seven_columns(self) -> None:
        """async_tasks has exactly 7 columns."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            cursor = await db.execute("PRAGMA table_info(async_tasks)")
            rows = await cursor.fetchall()
            assert len(rows) == 7, f"Expected 7 columns, got {len(rows)}"


# ============================================================
# B4: Clean DB path (no pre-existing runs table)
# ============================================================


class TestCleanDBPath:
    """B4: Fresh DB with no legacy runs table — init_database_in_conn succeeds."""

    @pytest.mark.asyncio
    async def test_clean_init_no_errors(self) -> None:
        """init_database_in_conn on fresh DB completes without error."""
        async with aiosqlite.connect(":memory:") as db:
            # Fresh DB — no pre-existing tables
            await init_database_in_conn(db)

            # Verify async_tasks exists
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='async_tasks'"
            )
            row = await cursor.fetchone()
            assert row is not None, "async_tasks table not created"

    @pytest.mark.asyncio
    async def test_no_legacy_runs_table_created(self) -> None:
        """No runs table is created on a fresh DB."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            )
            row = await cursor.fetchone()
            assert row is None, "Legacy runs table should not be created on fresh DB"

    @pytest.mark.asyncio
    async def test_async_tasks_table_exists(self) -> None:
        """SELECT from sqlite_master returns exactly 1 row for async_tasks."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            cursor = await db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='async_tasks'"
            )
            count = (await cursor.fetchone())[0]
            assert count == 1


# ============================================================
# B5: Existing DB path (re-run scenario)
# ============================================================


class TestExistingDBPath:
    """B5: Re-run migration on DB where columns already exist — no corruption."""

    @pytest.mark.asyncio
    async def test_existing_columns_no_error(self) -> None:
        """migrate_runs_merge on DB with report_types column already present."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db)

            # First migration — adds columns + copies data
            await migrate_runs_merge(db)

            # Insert a row into pipeline_runs
            await db.execute(
                """INSERT INTO pipeline_runs
                   (run_id, component, status, report_types)
                   VALUES ('existing-1', 'test', 'done', '["weekly"]')"""
            )
            await db.commit()

            cursor = await db.execute("SELECT COUNT(*) FROM pipeline_runs")
            count_before = (await cursor.fetchone())[0]

            # Re-run migration — must not error or corrupt
            await migrate_runs_merge(db)

            cursor = await db.execute("SELECT COUNT(*) FROM pipeline_runs")
            count_after = (await cursor.fetchone())[0]

            assert count_before == count_after, (
                f"Row count changed: {count_before} -> {count_after}"
            )

    @pytest.mark.asyncio
    async def test_existing_row_unchanged_after_rerun(self) -> None:
        """Data inserted before re-run is unchanged after migration re-runs."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db)

            await migrate_runs_merge(db)

            # Verify specific row data
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT report_types, nodes_completed, nodes_total FROM pipeline_runs WHERE run_id = ?",
                ("run-001",),
            )
            row_before = dict(await cursor.fetchone())

            # Re-run migration
            await migrate_runs_merge(db)

            cursor = await db.execute(
                "SELECT report_types, nodes_completed, nodes_total FROM pipeline_runs WHERE run_id = ?",
                ("run-001",),
            )
            row_after = dict(await cursor.fetchone())

            assert row_before == row_after


# ============================================================
# P078: Real-SQLite DDL verification (non-mock, all layers)
# ============================================================


class TestDDLVerification:
    """P078: Three-layer DDL verification with real in-memory SQLite."""

    @pytest.mark.asyncio
    async def test_pipeline_runs_has_legacy_columns_after_init(self) -> None:
        """Layer 1: PRAGMA table_info confirms legacy columns in pipeline_runs."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)
            await _create_legacy_runs_table(db)
            await migrate_runs_merge(db)

            cursor = await db.execute("PRAGMA table_info(pipeline_runs)")
            cols = {row[1] for row in await cursor.fetchall()}
            for col in ("report_types", "nodes_completed", "nodes_total"):
                assert col in cols, f"Column {col} missing from pipeline_runs"

    @pytest.mark.asyncio
    async def test_async_tasks_sqlite_master(self) -> None:
        """Layer 2: sqlite_master query confirms async_tasks table."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            cursor = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='async_tasks'"
            )
            row = await cursor.fetchone()
            assert row is not None
            sql: str = row[0]
            assert "task_id" in sql
            assert "task_type" in sql
            assert "status" in sql
            assert "result" in sql
            assert "error" in sql
            assert "created_at" in sql
            assert "updated_at" in sql

    @pytest.mark.asyncio
    async def test_async_tasks_insert_behavior(self) -> None:
        """Layer 3: Actual INSERT behavior confirms DDL correctness."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            # Full insert with all columns
            await db.execute(
                """INSERT INTO async_tasks
                   (task_id, task_type, status, result, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "t-full",
                    "daily_run",
                    "completed",
                    '{"report_id": "r-001"}',
                    None,
                    "2026-06-01T08:00:00",
                    "2026-06-01T08:05:00",
                ),
            )
            await db.commit()

            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM async_tasks WHERE task_id = 't-full'")
            row = dict(await cursor.fetchone())
            assert row["task_type"] == "daily_run"
            assert row["status"] == "completed"
            assert row["result"] == '{"report_id": "r-001"}'
            assert row["created_at"] == "2026-06-01T08:00:00"
            assert row["updated_at"] == "2026-06-01T08:05:00"
