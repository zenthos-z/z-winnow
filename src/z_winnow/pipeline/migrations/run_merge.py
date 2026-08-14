"""Migrate legacy runs table into canonical pipeline_runs + create async_tasks DDL.

T-W14-8: Idempotent migration merging the legacy ``runs`` table (owned by
``web/pages/run_control.py``) into the canonical ``pipeline_runs`` table
(owned by ``graph/progress.py``), plus a new ``async_tasks`` table for the
upcoming task-queue service.

P052: Idempotent via ``PRAGMA table_info`` per-column existence checks.
P014: NEVER-throw — migration failures log warning only, never propagate.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Columns that exist in legacy `runs` but NOT in `pipeline_runs`.
# These are added as nullable columns via ALTER TABLE + PRAGMA guard.
_LEGACY_ONLY_COLUMNS: list[tuple[str, str]] = [
    ("report_types", "TEXT"),
    ("nodes_completed", "INTEGER"),
    ("nodes_total", "INTEGER"),
]


async def migrate_runs_merge(db: aiosqlite.Connection) -> None:
    """Merge legacy runs table into pipeline_runs. P052 + P014.

    Steps:
    1. Check if legacy ``runs`` table exists — skip if absent (fresh DB).
    2. Add legacy-only columns to ``pipeline_runs`` via PRAGMA guard (P052).
    3. Copy data using ``INSERT OR IGNORE`` to avoid duplicate-key errors.
       Map ``runs.progress`` → ``pipeline_runs.progress_pct``.
    4. Safe to run N times — idempotent at every step.

    L005: Preserves run_id as PK exactly, no new IDs generated.
    """

    # P014: NEVER-throw — entire body wrapped in try/except
    try:
        # Step 1: Ensure pipeline_runs has the legacy-only columns (always)
        # Spec: unified schema must be a superset regardless of runs table existence.
        await _add_legacy_columns(db)

        # Step 2: Check if legacy `runs` table exists — skip data copy if absent
        cursor: aiosqlite.Cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        )
        runs_exists_row: tuple[Any, ...] | None = await cursor.fetchone()
        if not runs_exists_row:
            logger.debug("migrate_runs_merge: no legacy runs table found — data copy skipped")
            return

        # Step 3: Copy data from runs → pipeline_runs
        # Uses INSERT OR IGNORE to handle re-runs (duplicate run_id PK).
        # A008: Pre-initialize result variables
        await db.execute(
            """INSERT OR IGNORE INTO pipeline_runs
               (run_id, component, status, started_at, completed_at,
                message_count, error_message, current_node, progress_pct,
                node_history, group_id, date, created_at,
                report_types, nodes_completed, nodes_total)
               SELECT
                   run_id,
                   'web_run_control' AS component,
                   status,
                   started_at,
                   completed_at,
                   0 AS message_count,
                   error_message,
                   NULL AS current_node,
                   progress AS progress_pct,
                   NULL AS node_history,
                   group_id,
                   date,
                   created_at,
                   report_types,
                   nodes_completed,
                   nodes_total
               FROM runs"""
        )
        await db.commit()

        # Log how many rows were migrated
        count_cursor = await db.execute("SELECT COUNT(*) FROM runs")
        count_row = await count_cursor.fetchone()
        source_count: int = count_row[0] if count_row else 0

        logger.info(
            "migrate_runs_merge: processed %d rows from legacy runs table",
            source_count,
        )
    except Exception as exc:
        # P014: NEVER-throw — log warning only
        logger.warning("migrate_runs_merge: migration skipped — %s", exc)


async def _add_legacy_columns(db: aiosqlite.Connection) -> None:
    """Add legacy-only columns to pipeline_runs if absent. P052 idempotent.

    Uses ``PRAGMA table_info`` per-column guard because SQLite does not
    support ``ALTER TABLE ADD COLUMN IF NOT EXISTS``.
    """
    # P014 guard
    try:
        cursor = await db.execute("PRAGMA table_info(pipeline_runs)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        for col_name, col_type in _LEGACY_ONLY_COLUMNS:
            if col_name not in existing_cols:
                await db.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {col_name} {col_type}")
                logger.info(
                    "migrate_runs_merge: added %s column to pipeline_runs",
                    col_name,
                )
    except Exception as exc:
        logger.warning("migrate_runs_merge: _add_legacy_columns skipped — %s", exc)


async def create_async_tasks_table(db: aiosqlite.Connection) -> None:
    """Create async_tasks table for task queue. P052 + P014.

    DDL:
        task_id     TEXT PK
        task_type   TEXT NOT NULL
        status      TEXT NOT NULL DEFAULT 'pending'
        result      TEXT          (JSON)
        error       TEXT
        created_at  TEXT
        updated_at  TEXT

    Uses ``CREATE TABLE IF NOT EXISTS`` for natural idempotency.
    """
    # P014: NEVER-throw
    try:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS async_tasks (
                task_id    TEXT PRIMARY KEY,
                task_type  TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                result     TEXT,
                error      TEXT,
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        await db.commit()
        logger.debug("create_async_tasks_table: async_tasks table ensured")
    except Exception as exc:
        logger.warning("create_async_tasks_table: migration skipped — %s", exc)


async def drop_weekly_report_enabled_column(db: aiosqlite.Connection) -> None:
    """Drop the ``weekly_report_enabled`` column from ``groups``.

    Destructive migration — weekly report functionality was removed, so the
    per-group toggle is now dead. P052 idempotent: only drops if the column
    still exists (PRAGMA table_info guard). P014 NEVER-throw.

    Requires SQLite >= 3.35.0 (DROP COLUMN); on older runtimes the
    NEVER-throw guard logs a warning and skips.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(groups)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}
        if "weekly_report_enabled" in existing_cols:
            await db.execute("ALTER TABLE groups DROP COLUMN weekly_report_enabled")
            await db.commit()
            logger.info("drop_weekly_report_enabled_column: dropped column from groups")
        else:
            logger.debug("drop_weekly_report_enabled_column: column already absent")
    except Exception as exc:
        logger.warning("drop_weekly_report_enabled_column: migration skipped — %s", exc)


async def migrate_report_versions_add_cover_and_judge(db: aiosqlite.Connection) -> None:
    """Add ``cover_generated`` and ``judge_result`` columns to ``report_versions``.

    P052 idempotent: uses PRAGMA table_info per-column guard.
    P014 NEVER-throw.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(report_versions)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        if "cover_generated" not in existing_cols:
            await db.execute(
                "ALTER TABLE report_versions ADD COLUMN cover_generated INTEGER DEFAULT 0"
            )
            logger.info("migrate_report_versions_add_cover_and_judge: added cover_generated")

        if "judge_result" not in existing_cols:
            await db.execute("ALTER TABLE report_versions ADD COLUMN judge_result TEXT")
            logger.info("migrate_report_versions_add_cover_and_judge: added judge_result")

        if "feishu_pushed_at" not in existing_cols:
            await db.execute("ALTER TABLE report_versions ADD COLUMN feishu_pushed_at TEXT")
            logger.info("migrate_report_versions_add_cover_and_judge: added feishu_pushed_at")

        await db.commit()
    except Exception as exc:
        logger.warning("migrate_report_versions_add_cover_and_judge: migration skipped — %s", exc)


# add_custom_tables_column removed — replaced by migrate_groups_add_custom_tables_blob from .custom_tables


async def migrate_feedback_provenance(db: aiosqlite.Connection) -> None:
    """M4: feedback provenance + version linkage + group_experiences.

    Adds provenance/version columns to ``feedback_events``, the ``is_active``
    column to ``report_versions`` (with one-time backfill of the latest version
    per report), and creates the ``group_experiences`` table (editable,
    group-bound experience store; L3, not MemOS).

    P052 idempotent: PRAGMA table_info per-column guard.
    P014 NEVER-throw: logs warning on failure, never blocks caller.
    """
    try:
        # ── feedback_events: provenance + version linkage columns ──
        cursor = await db.execute("PRAGMA table_info(feedback_events)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        feedback_new_cols = [
            ("target_version_id", "TEXT"),
            ("target_topic_id", "TEXT"),
            ("produced_version_id", "TEXT"),
            ("memos_cube_id", "TEXT"),
            ("memos_node_id", "TEXT"),
            ("archived_memos_id", "TEXT"),
            ("status", "TEXT DEFAULT 'active'"),
            ("rolled_back_at", "TEXT"),
            ("rolled_back_by", "TEXT"),
        ]
        for col_name, col_type in feedback_new_cols:
            if col_name not in existing_cols:
                await db.execute(f"ALTER TABLE feedback_events ADD COLUMN {col_name} {col_type}")
                logger.info("migrate_feedback_provenance: feedback_events +%s", col_name)

        # 依赖新增列（produced_version_id / status）的索引在此创建——不能放
        # WEB_SCHEMA_SQL，否则旧库（feedback_events 已存在但缺这些列）会在
        # executescript 阶段先于本 migration 建索引而 "no such column" 崩溃。
        await db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_feedback_produced
                ON feedback_events(produced_version_id) WHERE produced_version_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_feedback_status
                ON feedback_events(group_id, status);
            """
        )

        # ── report_versions: is_active column + backfill ──
        cursor = await db.execute("PRAGMA table_info(report_versions)")
        rows = await cursor.fetchall()
        rv_cols: set[str] = {row[1] for row in rows}
        if "is_active" not in rv_cols:
            await db.execute("ALTER TABLE report_versions ADD COLUMN is_active INTEGER DEFAULT 1")
            logger.info("migrate_feedback_provenance: report_versions +is_active")
            # Backfill: only the latest (MAX version_number) version per report is active.
            # DEFAULT 1 left all existing rows active; correct that for multi-version reports.
            await db.executescript(
                """
                UPDATE report_versions SET is_active = 0;
                UPDATE report_versions SET is_active = 1
                WHERE version_id IN (
                    SELECT rv.version_id FROM report_versions rv
                    WHERE rv.version_number = (
                        SELECT MAX(rv2.version_number)
                        FROM report_versions rv2
                        WHERE rv2.report_id = rv.report_id
                    )
                );
                """
            )
            logger.info("migrate_feedback_provenance: backfilled report_versions.is_active")
        # idx_rv_active 依赖 is_active 列——无论新旧库，在此幂等创建（旧库此前缺列，
        # REPORT_SCHEMA_SQL 的索引已移除避免 executescript 阶段崩溃）。
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rv_active "
            "ON report_versions(report_id, is_active) WHERE is_active=1"
        )

        # ── group_experiences: editable experience store (L3) ──
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_experiences (
                experience_id    TEXT PRIMARY KEY,
                group_id         TEXT NOT NULL,
                topic_name       TEXT,
                target_type      TEXT,
                lesson           TEXT NOT NULL,
                origin_feedback_id TEXT,
                origin_version_id  TEXT,
                status           TEXT DEFAULT 'active',
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT,
                updated_by       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_gexp_group_status ON group_experiences(group_id, status);
            CREATE INDEX IF NOT EXISTS idx_gexp_topic ON group_experiences(group_id, topic_name);
            """
        )
        await db.commit()
    except Exception as exc:
        logger.warning("migrate_feedback_provenance: migration skipped — %s", exc)
