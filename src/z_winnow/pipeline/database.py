"""T-A3: SQLite 三层 Schema + 迁移框架.

建立 draft-v5 定义的三层数据表并实现初始化/溯源函数:
1. raw_messages — CipherTalk 原始消息 (serverID PK)
2. parsed_contexts — 按天/按主题组装的上下文块 (server_ids JSON 溯源)
3. topic_summaries — 议题级结构化总结 (全链路 JOIN)

Usage:
    async with aiosqlite.connect("data/winnow.db") as db:
        await init_database_in_conn(db)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

from z_winnow.config.settings import get_settings
from z_winnow.pipeline.migrations import (
    create_async_tasks_table,
    drop_weekly_report_enabled_column,
    migrate_feedback_provenance,
    migrate_groups_add_custom_tables_blob,
    migrate_report_versions_add_cover_and_judge,
    migrate_runs_merge,
)

logger = logging.getLogger(__name__)

# ============================================================
# Schema DDL — 三层数据表 + 索引
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_messages (
    serverID TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    group_id TEXT DEFAULT '',
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    msg_type TEXT DEFAULT 'text',
    image_path TEXT,
    sanitized INTEGER DEFAULT 0,
    raw_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS parsed_contexts (
    context_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    group_id TEXT DEFAULT '',
    server_ids TEXT NOT NULL,
    context_text TEXT NOT NULL,
    token_count INTEGER,
    source_subagent TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS topic_summaries (
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
    background TEXT DEFAULT '',
    process TEXT DEFAULT '',
    conclusion TEXT DEFAULT '',
    description TEXT DEFAULT '',
    participants TEXT DEFAULT '',
    trend TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    started_at TEXT,
    completed_at TEXT,
    message_count INTEGER DEFAULT 0,
    error_message TEXT,
    current_node TEXT,
    progress_pct INTEGER,
    node_history TEXT,
    group_id TEXT,
    date TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_raw_date ON raw_messages(date);
CREATE INDEX IF NOT EXISTS idx_raw_sender ON raw_messages(sender);
CREATE INDEX IF NOT EXISTS idx_context_date ON parsed_contexts(date);
CREATE INDEX IF NOT EXISTS idx_summary_date ON topic_summaries(date);
CREATE INDEX IF NOT EXISTS idx_summary_topic ON topic_summaries(topic_name);
-- idempotent topic identity: one row per (date, group_id, topic_name)
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_identity ON topic_summaries(date, group_id, topic_name);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_component ON pipeline_runs(component);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_group_status ON pipeline_runs(status, component);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_group_date ON pipeline_runs(group_id, date);
"""


async def init_database(db_path: str | None = None) -> None:
    """初始化 SQLite 数据库：自动创建 data/ 目录、建表、建索引。

    Args:
        db_path: SQLite 数据库文件路径，默认从 Settings.db_path 读取

    Raises:
        OSError: 无法创建 data/ 目录时
        aiosqlite.Error: SQL 执行失败时
    """
    # S7: Read default path from Settings instead of hardcoded literal
    if db_path is None:
        db_path = get_settings().db_path
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await init_database_in_conn(db)


# ============================================================
# Web Dashboard tables (W9 migrations — groups, members, core_topics, feedback)
# Added to init_database_in_conn so tests can run without external migration tooling.
# Uses CREATE TABLE IF NOT EXISTS for idempotency.
# ============================================================

WEB_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
    group_id              TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    chatroom_id           TEXT NOT NULL,
    output_dir            TEXT,
    feishu_enabled        INTEGER DEFAULT 0,
    -- Feishu Bitable target (per-group). One Base per group, up to 4 data tables.
    feishu_base_token             TEXT,
    feishu_table_summary          TEXT,
    feishu_table_topics           TEXT,
    feishu_table_resources        TEXT,
    feishu_table_engineering      TEXT,
    feishu_framework_initialized  INTEGER DEFAULT 0,
    feishu_engineering_enabled    INTEGER DEFAULT 1,
    -- #7.1: independent engineering content toggle (saved via saveConfig, not init).
    engineering_enabled           INTEGER DEFAULT 1,
    -- #9.4: per-group pluggable table set blob {kind: {enabled, table_id}} (JSON).
    feishu_tables                 TEXT,
    -- CT-1: custom_tables blob {kind: {enabled, config}} — single source of truth for
    -- custom-table enable/disable (feishu_tables above is derived from it on write).
    custom_tables                 TEXT,
    custom_prompt_hints   TEXT,
    is_active             INTEGER DEFAULT 1,
    daily_report_enabled  INTEGER DEFAULT 1,
    daily_schedule_cron   TEXT,
    created_at            TEXT DEFAULT (datetime('now')),
    updated_at            TEXT DEFAULT (datetime('now')),
    created_by            TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    member_id   TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    wxid        TEXT,
    role        TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    note        TEXT,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(group_id, wxid)
);

CREATE TABLE IF NOT EXISTS core_topics (
    core_topic_id    TEXT PRIMARY KEY,
    group_id         TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT,
    keywords         TEXT,
    priority         INTEGER DEFAULT 1,
    is_active        INTEGER DEFAULT 1,
    last_matched_date TEXT,
    match_count      INTEGER DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    created_by       TEXT DEFAULT 'admin'
);

CREATE INDEX IF NOT EXISTS idx_core_topics_group ON core_topics(group_id, is_active);

CREATE TABLE IF NOT EXISTS feedback_events (
    feedback_id     TEXT PRIMARY KEY,
    created_at      TEXT DEFAULT (datetime('now')),
    group_id        TEXT NOT NULL,
    date            TEXT NOT NULL,
    report_id       TEXT,
    target_type     TEXT NOT NULL,
    target_id       TEXT,
    target_path     TEXT,
    signal          TEXT NOT NULL,
    severity        TEXT DEFAULT 'info',
    rating          TEXT,
    tags            TEXT DEFAULT '[]',
    correction_mode TEXT,
    original_text   TEXT,
    corrected_text  TEXT,
    correction_note TEXT,
    reporter        TEXT DEFAULT 'admin',
    consumed_at     TEXT,
    consumed_by     TEXT,
    -- M4 feedback provenance + version linkage (溯源四元组)
    target_version_id   TEXT,                  -- 被反馈版本 "{report_id}-v{n}"
    target_topic_id     TEXT,                   -- 被反馈议题 id（议题级反馈）
    produced_version_id TEXT,                   -- 反馈产出新版本（regenerate 回填）
    memos_cube_id       TEXT,                   -- 被纠正节点所在 cube（如 winnow:{gid}:topics）
    memos_node_id       TEXT,                   -- feedback_memory 写入的新(activated)节点 id — 当前生效
    archived_memos_id   TEXT,                   -- feedback_memory 归档的旧节点 id — 版本链回溯
    status              TEXT DEFAULT 'active',  -- active | rolled_back
    rolled_back_at      TEXT,
    rolled_back_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_group_date ON feedback_events(group_id, date);
CREATE INDEX IF NOT EXISTS idx_feedback_target ON feedback_events(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_feedback_unconsumed ON feedback_events(consumed_at) WHERE consumed_at IS NULL;
-- idx_feedback_produced / idx_feedback_status 依赖 migration 才加的列
-- (produced_version_id / status)，改在 migrate_feedback_provenance 内创建，
-- 避免旧库 executescript 阶段先建索引撞 "no such column"。

-- M4 经验家园：从反馈派生、群绑定、可编辑、跨天的可召回经验句（L3，不进 MemOS）。
CREATE TABLE IF NOT EXISTS group_experiences (
    experience_id    TEXT PRIMARY KEY,          -- exp-{date}-{seq}
    group_id         TEXT NOT NULL,             -- 绑定群聊
    topic_name       TEXT,                      -- 关联议题锚点（精准召回），nullable
    target_type      TEXT,                      -- 来源反馈类型 topic/resource/trend/report/{table_id}
    lesson           TEXT NOT NULL,             -- 可编辑经验句（家园）
    origin_feedback_id TEXT,                    -- 溯源反馈事件
    origin_version_id  TEXT,                    -- 溯源版本
    status           TEXT DEFAULT 'active',     -- active | archived | superseded
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT,
    updated_by       TEXT
);
CREATE INDEX IF NOT EXISTS idx_gexp_group_status ON group_experiences(group_id, status);
CREATE INDEX IF NOT EXISTS idx_gexp_topic ON group_experiences(group_id, topic_name);
"""

# ============================================================
# W10 memos_sync_queue table — write-through async sync (T-W10-E-c)
# Schema per wave9-memos-design.md §11.1
# ============================================================

MEMOS_SYNC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memos_sync_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
    op_type TEXT NOT NULL,                       -- add_topic | update_topic | add_feedback | add_edge | update_status
    cube_id TEXT NOT NULL,
    payload TEXT NOT NULL,                       -- JSON
    status TEXT DEFAULT 'pending',               -- pending | processing | done | failed
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_msq_pending ON memos_sync_queue(status) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_msq_cube ON memos_sync_queue(cube_id);
"""

# ============================================================
# W10 report_versions table — tracks report generation/regeneration
# ============================================================

REPORT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS report_versions (
    version_id TEXT PRIMARY KEY,          -- "{report_id}-v{n}"
    report_id TEXT NOT NULL,              -- 报告 ID
    group_id TEXT NOT NULL,
    date TEXT NOT NULL,
    version_number INTEGER NOT NULL,      -- 1, 2, 3...
    content TEXT,                         -- P022: nullable — NULL until stage H export (L0-1)
    content_changed BOOLEAN DEFAULT FALSE, -- vs 上一版有变更
    source TEXT NOT NULL,                 -- "daily_run" | "incremental_fix" | "manual_regen" (L0-2)
    build_duration_s REAL,                -- 构建耗时
    cover_generated INTEGER DEFAULT 0,    -- 配图是否已生成 (0/1)
    judge_result TEXT,                    -- LLM-as-judge JSON 评分结果 (nullable)
    feishu_pushed_at TEXT,                -- 最后一次成功推送飞书的时间 ISO8601 (nullable)
    is_active INTEGER DEFAULT 1,          -- M4: 当前生效版本（回滚=重指）。每 report 仅一行=1
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rv_report ON report_versions(report_id, version_number);
-- idx_rv_active 依赖 migration 才加的 is_active 列，改在 migrate_feedback_provenance 内创建。
"""

# ============================================================
# Batch generation tables — 批量日报生成任务调度
# batch_jobs: 批量任务主表
# batch_job_items: 明细表，每条=群×日期
# ============================================================

BATCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_id      TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|running|completed|cancelled|partial_failed
    total_groups  INTEGER NOT NULL DEFAULT 0,
    total_days    INTEGER NOT NULL DEFAULT 0,
    total_items   INTEGER NOT NULL DEFAULT 0,
    completed     INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    skipped_empty INTEGER NOT NULL DEFAULT 0,
    max_parallel  INTEGER NOT NULL DEFAULT 3,
    started_at    TEXT,
    completed_at  TEXT,
    error_message TEXT,
    created_by    TEXT
);

CREATE TABLE IF NOT EXISTS batch_job_items (
    item_id       TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES batch_jobs(batch_id),
    group_id      TEXT NOT NULL,
    date          TEXT NOT NULL,
    run_id        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed|skipped_empty|cancelled
    progress_pct  INTEGER DEFAULT 0,
    error_message TEXT,
    started_at    TEXT,
    completed_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id),
    UNIQUE(batch_id, group_id, date)
);

CREATE INDEX IF NOT EXISTS idx_bji_batch ON batch_job_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_bji_status ON batch_job_items(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_bji_group_date ON batch_job_items(group_id, date);
"""


# ============================================================
# Daily-report scheduler state — heartbeat written by DailyScheduler each tick.
# Single-row key/value: the status board reads 'last_tick' to show daemon liveness.
# (engine._write_heartbeat also creates this defensively, so old DBs self-heal.)
# ============================================================
SCHEDULER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""


async def init_database_in_conn(db: aiosqlite.Connection) -> None:
    """在已有连接上执行建表 DDL（用于测试内存数据库）。

    Runs base schema + web dashboard tables for W9 test support.
    All CREATE statements use IF NOT EXISTS for idempotency.

    Args:
        db: aiosqlite 数据库连接
    """
    await db.executescript(SCHEMA_SQL)
    await db.executescript(WEB_SCHEMA_SQL)
    await db.executescript(MEMOS_SYNC_SCHEMA_SQL)
    await db.executescript(REPORT_SCHEMA_SQL)
    await db.executescript(BATCH_SCHEMA_SQL)
    await db.executescript(SCHEDULER_SCHEMA_SQL)
    await db.commit()
    # Parallel safety: WAL mode + busy timeout for concurrent writes
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA wal_autocheckpoint=1000")
    # T-W12-1: 迁移 — 纠偏 report_versions.content nullable + pipeline_runs 索引
    await migrate_report_versions_ddl_fix(db)
    await migrate_pipeline_runs_index_fix(db)
    await migrate_l1_l2_l3_group_id(db)
    await migrate_topic_summaries_topic_id(db)
    # Split conclusion into background/process/conclusion (因果链三段)
    await migrate_topic_summaries_split_conclusion(db)
    await migrate_raw_messages_timestamp(db)
    # T-W14-8: Merge legacy runs table + create async_tasks table
    await migrate_runs_merge(db)
    await create_async_tasks_table(db)
    # Drop dead weekly_report_enabled column (weekly feature removed)
    await drop_weekly_report_enabled_column(db)
    # Feishu Bitable target columns on groups (one Base per group, 3 data tables)
    await migrate_groups_add_bitable_columns(db)
    # #9.4: per-group pluggable table-set blob (backfills from legacy 4 columns).
    await migrate_groups_add_feishu_tables_blob(db)
    # #7.1: independent engineering content toggle (migration idempotent).
    await migrate_groups_add_engineering_enabled(db)
    # CT-1: custom_tables blob column for custom table configurations.
    await migrate_groups_add_custom_tables_blob(db)
    # Image persistence + LLM judge result persistence on report_versions.
    await migrate_report_versions_add_cover_and_judge(db)
    # M4: feedback provenance + report_versions.is_active + group_experiences.
    await migrate_feedback_provenance(db)
    logger.debug("Database schema initialized successfully.")


# ============================================================
# T-W12-1 L0-1: report_versions.content nullable 迁移
# P052: Idempotent migration — checks notnull flag before acting.
# SQLite 不支持 ALTER COLUMN，需重建表。
# ============================================================


async def migrate_report_versions_ddl_fix(db: aiosqlite.Connection) -> None:
    """Migrate report_versions: make content column nullable (L0-1).

    P022: Storage/Formatting Layer Separation — content=NULL until stage H
    export writes the rendered Markdown.

    P052: Idempotent — checks PRAGMA table_info before acting.
    P014: NEVER-throw — migration failure only logs, never blocks caller.

    For new databases, REPORT_SCHEMA_SQL already defines content as TEXT
    (nullable). This migration only affects existing databases where the
    column was created with NOT NULL.

    SQLite does not support ALTER COLUMN, so we recreate the table.

    Args:
        db: aiosqlite database connection.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(report_versions)")
        rows = await cursor.fetchall()
        content_col = [r for r in rows if r[1] == "content"]
        if not content_col:
            return  # table doesn't exist or no content column — nothing to do

        notnull_flag: int = content_col[0][3]  # 1 = NOT NULL, 0 = nullable
        if notnull_flag == 0:
            return  # already nullable — migration not needed

        # L0-1: Recreate report_versions with nullable content
        # P052: Idempotent — only runs when NOT NULL constraint detected
        await db.executescript("""
            ALTER TABLE report_versions RENAME TO _report_versions_old;
            CREATE TABLE report_versions (
                version_id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                date TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                content TEXT,
                content_changed BOOLEAN DEFAULT FALSE,
                source TEXT NOT NULL,
                build_duration_s REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO report_versions SELECT * FROM _report_versions_old;
            DROP TABLE _report_versions_old;
            CREATE INDEX IF NOT EXISTS idx_rv_report
                ON report_versions(report_id, version_number);
        """)
        await db.commit()
        logger.info("migrate_report_versions_ddl_fix: content column made nullable")
    except Exception as exc:
        logger.warning("migrate_report_versions_ddl_fix: migration skipped — %s", exc)


# ============================================================
# T-W12-1 L0-3: pipeline_runs composite index migration
# P052: Idempotent — CREATE INDEX IF NOT EXISTS is naturally safe.
# ============================================================


async def migrate_pipeline_runs_index_fix(db: aiosqlite.Connection) -> None:
    """Ensure idx_pipeline_runs_group_date exists (L0-3).

    P052: Idempotent — CREATE INDEX IF NOT EXISTS is naturally safe for
    both new and existing databases.

    For new databases, SCHEMA_SQL already includes this index.
    This migration ensures existing databases also get it.

    Args:
        db: aiosqlite database connection.
    """
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_group_date "
            "ON pipeline_runs(group_id, date)"
        )
        await db.commit()
        logger.debug("migrate_pipeline_runs_index_fix: idx_pipeline_runs_group_date ensured")
    except Exception as exc:
        logger.warning("migrate_pipeline_runs_index_fix: migration skipped — %s", exc)


async def migrate_l1_l2_l3_group_id(db: aiosqlite.Connection) -> None:
    """Add group_id column to raw_messages, parsed_contexts, topic_summaries.

    P052: Idempotent — checks PRAGMA table_info before each ALTER TABLE.
    P014: NEVER-throw — migration failure only logs, never blocks caller.

    Existing rows get group_id = '' (empty string) via DEFAULT.
    SQLite applies the default at ALTER time without a separate UPDATE.

    Args:
        db: aiosqlite database connection.
    """
    tables = ["raw_messages", "parsed_contexts", "topic_summaries"]
    try:
        for table in tables:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            existing_cols: set[str] = {row[1] for row in rows}

            if "group_id" not in existing_cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN group_id TEXT DEFAULT ''")
                logger.info("migrate: added group_id column to %s", table)

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_group_date ON raw_messages(group_id, date)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_context_group_date ON parsed_contexts(group_id, date)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_summary_group_date ON topic_summaries(group_id, date)"
        )
        await db.commit()
        logger.debug("migrate_l1_l2_l3_group_id: indexes ensured")
    except Exception as exc:
        logger.warning("migrate_l1_l2_l3_group_id: migration skipped — %s", exc)


async def migrate_groups_add_bitable_columns(db: aiosqlite.Connection) -> None:
    """Add Feishu Bitable target columns to the groups table.

    Adds per-group Base targeting: ``feishu_base_token`` (the Base app_token),
    one ``feishu_table_<kind>`` ID per data table, ``feishu_framework_initialized``
    (whether the Base's table framework has been created), and
    ``feishu_engineering_enabled`` (some groups omit the engineering table).

    P052: Idempotent — checks PRAGMA table_info before each ALTER TABLE.
    P014: NEVER-throw — migration failure only logs, never blocks caller.
    """
    new_cols = {
        "feishu_base_token": "TEXT",
        "feishu_table_summary": "TEXT",
        "feishu_table_topics": "TEXT",
        "feishu_table_resources": "TEXT",
        "feishu_table_engineering": "TEXT",
        "feishu_framework_initialized": "INTEGER DEFAULT 0",
        "feishu_engineering_enabled": "INTEGER DEFAULT 1",
    }
    try:
        cursor = await db.execute("PRAGMA table_info(groups)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        for col, decl in new_cols.items():
            if col not in existing_cols:
                await db.execute(f"ALTER TABLE groups ADD COLUMN {col} {decl}")
                logger.info("migrate: added %s column to groups", col)

        await db.commit()
        logger.debug("migrate_groups_add_bitable_columns: columns ensured")
    except Exception as exc:
        logger.warning("migrate_groups_add_bitable_columns: migration skipped — %s", exc)


async def migrate_groups_add_engineering_enabled(db: aiosqlite.Connection) -> None:
    """Add the independent ``engineering_enabled`` column (#7.1).

    This replaces the blob-level engineering toggle with a first-class column
    that the pipeline reads directly. Defaults to 1 (enabled) for backward
    compatibility.

    P052: Idempotent — checks PRAGMA table_info before ALTER TABLE.
    P014: NEVER-throw — migration failure only logs, never blocks caller.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(groups)")
        rows = await cursor.fetchall()
        existing_cols = {r[1] for r in rows}
        if "engineering_enabled" not in existing_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN engineering_enabled INTEGER DEFAULT 1")
            logger.info("migrate: added engineering_enabled column to groups")
        await db.commit()
    except Exception as exc:
        logger.warning("migrate_groups_add_engineering_enabled: migration skipped — %s", exc)


async def migrate_groups_add_feishu_tables_blob(db: aiosqlite.Connection) -> None:
    """Add the per-group ``feishu_tables`` JSON blob column (#9.4) + backfill.

    The blob ``{kind: {enabled, table_id}}`` is the single source of truth for
    which tables a group uses. For groups persisted before this column existed,
    synthesize the blob from the legacy ``feishu_table_<kind>`` columns +
    ``feishu_engineering_enabled`` so existing frameworks keep working unchanged.

    P052: Idempotent — adds the column only if missing; backfills only rows where
    the blob is NULL/empty.
    P014: NEVER-throw — migration failure only logs, never blocks caller.
    """
    import json

    try:
        cursor = await db.execute("PRAGMA table_info(groups)")
        rows = await cursor.fetchall()
        existing_cols = {r[1] for r in rows}
        if "feishu_tables" not in existing_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN feishu_tables TEXT")
            logger.info("migrate: added feishu_tables column to groups")

        # Backfill: rows with null/empty blob but legacy columns populated.
        cursor = await db.execute(
            """SELECT group_id, feishu_table_summary, feishu_table_topics,
                      feishu_table_resources, feishu_table_engineering,
                      feishu_engineering_enabled, feishu_tables
               FROM groups
               WHERE feishu_tables IS NULL OR feishu_tables = ''"""
        )
        for r in await cursor.fetchall():
            gid, summ, topics, resources, eng_tbl, eng_en, _ = r
            blob = {
                "summary": {"enabled": True, "table_id": summ or ""},
                "topics": {"enabled": True, "table_id": topics or ""},
                "resources": {"enabled": True, "table_id": resources or ""},
                "engineering": {
                    "enabled": bool(eng_en) if eng_en is not None else True,
                    "table_id": eng_tbl or "",
                },
            }
            await db.execute(
                "UPDATE groups SET feishu_tables = ? WHERE group_id = ?",
                (json.dumps(blob, ensure_ascii=False), gid),
            )
        await db.commit()
        logger.debug("migrate_groups_add_feishu_tables_blob: blob ensured + backfilled")
    except Exception as exc:
        logger.warning("migrate_groups_add_feishu_tables_blob: migration skipped — %s", exc)


async def migrate_raw_messages_timestamp(db: aiosqlite.Connection) -> None:
    """Add timestamp column to raw_messages for message send time.

    P052: Idempotent — checks PRAGMA table_info before ALTER TABLE.
    P014: NEVER-throw — migration failure only logs, never blocks caller.

    The timestamp (ms) is computed from CipherTalk API fields:
    sortSeq > timestamp > createTime*1000.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(raw_messages)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        if "timestamp" not in existing_cols:
            await db.execute("ALTER TABLE raw_messages ADD COLUMN timestamp INTEGER DEFAULT 0")
            await db.commit()
            logger.info("migrate: added timestamp column to raw_messages")
    except Exception as exc:
        logger.warning("migrate_raw_messages_timestamp: migration skipped — %s", exc)


async def migrate_topic_summaries_topic_id(db: aiosqlite.Connection) -> None:
    """Add topic_id column to topic_summaries + unique identity index.

    P052: Idempotent — checks PRAGMA table_info before ALTER TABLE.
    P014: NEVER-throw — migration failure only logs, never blocks caller.

    The (date, group_id, topic_name) unique index enables INSERT OR REPLACE
    for idempotent daily re-runs.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(topic_summaries)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        if "topic_id" not in existing_cols:
            await db.execute("ALTER TABLE topic_summaries ADD COLUMN topic_id TEXT DEFAULT ''")
            logger.info("migrate: added topic_id column to topic_summaries")

        # Unique index for (date, group_id, topic_name) — idempotent writes
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_identity "
            "ON topic_summaries(date, group_id, topic_name)"
        )
        await db.commit()
        logger.debug("migrate_topic_summaries_topic_id: topic_id + unique index ensured")
    except Exception as exc:
        logger.warning("migrate_topic_summaries_topic_id: migration skipped — %s", exc)


# ============================================================
# 议题因果链三段拆分：conclusion → background / process / conclusion
# P052: Idempotent — checks PRAGMA table_info before ALTER TABLE.
# P014: NEVER-throw — migration failure only logs, never blocks caller.
# 旧数据不迁移（决策）：旧行保留原合并 conclusion，background/process 为空。
# ============================================================


async def migrate_topic_summaries_split_conclusion(db: aiosqlite.Connection) -> None:
    """Add ``background`` / ``process`` columns to ``topic_summaries``.

    Splits the legacy single ``conclusion`` causal-chain string into three
    fields — background / process / conclusion — to match the frontend
    ``reports.html`` 「背景/过程/结论」三槽. P052 idempotent (PRAGMA check
    before ALTER); P014 never-throw. Existing rows keep their combined
    conclusion text — no backfill (decision: old reports stay as-is).
    """
    try:
        cursor = await db.execute("PRAGMA table_info(topic_summaries)")
        rows = await cursor.fetchall()
        existing_cols: set[str] = {row[1] for row in rows}

        if "background" not in existing_cols:
            await db.execute("ALTER TABLE topic_summaries ADD COLUMN background TEXT DEFAULT ''")
            logger.info("migrate: added background column to topic_summaries")
        if "process" not in existing_cols:
            await db.execute("ALTER TABLE topic_summaries ADD COLUMN process TEXT DEFAULT ''")
            logger.info("migrate: added process column to topic_summaries")
        await db.commit()
        logger.debug("migrate_topic_summaries_split_conclusion: background/process ensured")
    except Exception as exc:
        logger.warning("migrate_topic_summaries_split_conclusion: migration skipped — %s", exc)


# ============================================================
# 便捷查询函数
# ============================================================


async def get_message_count(
    db: aiosqlite.Connection,
    date: str | None = None,
    *,
    group_id: str | None = None,
) -> int:
    """获取消息数量，可按日期和群组过滤。

    Args:
        db: 数据库连接
        date: 可选日期 YYYYMMDD
        group_id: 可选群组标识符

    Returns:
        消息数量
    """
    if date and group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM raw_messages WHERE date = ? AND group_id = ?",
            (date, group_id),
        )
    elif date:
        cursor = await db.execute("SELECT COUNT(*) FROM raw_messages WHERE date = ?", (date,))
    elif group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM raw_messages WHERE group_id = ?", (group_id,)
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM raw_messages")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_context_count(
    db: aiosqlite.Connection,
    date: str | None = None,
    *,
    group_id: str | None = None,
) -> int:
    """获取上下文块数量。

    Args:
        db: 数据库连接
        date: 可选日期
        group_id: 可选群组标识符

    Returns:
        上下文块数量
    """
    if date and group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM parsed_contexts WHERE date = ? AND group_id = ?",
            (date, group_id),
        )
    elif date:
        cursor = await db.execute("SELECT COUNT(*) FROM parsed_contexts WHERE date = ?", (date,))
    elif group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM parsed_contexts WHERE group_id = ?", (group_id,)
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM parsed_contexts")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_topic_count(
    db: aiosqlite.Connection,
    date: str | None = None,
    *,
    group_id: str | None = None,
) -> int:
    """获取议题总结数量。

    Args:
        db: 数据库连接
        date: 可选日期
        group_id: 可选群组标识符

    Returns:
        议题总结数量
    """
    if date and group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM topic_summaries WHERE date = ? AND group_id = ?",
            (date, group_id),
        )
    elif date:
        cursor = await db.execute("SELECT COUNT(*) FROM topic_summaries WHERE date = ?", (date,))
    elif group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM topic_summaries WHERE group_id = ?", (group_id,)
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM topic_summaries")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_raw_message(db: aiosqlite.Connection, server_id: str) -> dict | None:
    """按 serverID 获取单条原始消息。

    Args:
        db: 数据库连接
        server_id: 微信 serverId

    Returns:
        消息字典或 None
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM raw_messages WHERE serverID = ?", (server_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_raw_messages_by_date(
    db: aiosqlite.Connection,
    date: str,
    *,
    group_id: str | None = None,
) -> list[dict]:
    """按日期获取所有原始消息。

    Args:
        db: 数据库连接
        date: 日期 YYYYMMDD
        group_id: 可选群组标识符

    Returns:
        消息列表
    """
    db.row_factory = aiosqlite.Row
    if group_id:
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE date = ? AND group_id = ? ORDER BY serverID",
            (date, group_id),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE date = ? ORDER BY serverID",
            (date,),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_contexts_by_date(
    db: aiosqlite.Connection,
    date: str,
    *,
    group_id: str | None = None,
) -> list[dict]:
    """按日期获取所有上下文块。

    Args:
        db: 数据库连接
        date: 日期 YYYYMMDD
        group_id: 可选群组标识符

    Returns:
        上下文块列表
    """
    db.row_factory = aiosqlite.Row
    if group_id:
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE date = ? AND group_id = ? ORDER BY context_id",
            (date, group_id),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE date = ? ORDER BY context_id",
            (date,),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_topics_by_date(
    db: aiosqlite.Connection,
    date: str,
    *,
    group_id: str | None = None,
) -> list[dict]:
    """按日期获取所有议题总结。

    Args:
        db: 数据库连接
        date: 日期 YYYYMMDD
        group_id: 可选群组标识符

    Returns:
        议题总结列表
    """
    db.row_factory = aiosqlite.Row
    if group_id:
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE date = ? AND group_id = ? ORDER BY summary_id",
            (date, group_id),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE date = ? ORDER BY summary_id",
            (date,),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ============================================================
# 批量写入函数 (供 ingest pipeline 使用)
# ============================================================


async def insert_raw_messages(
    db: aiosqlite.Connection,
    messages: list[dict],
    date: str,
    group_id: str = "",
) -> int:
    """批量 INSERT OR REPLACE 原始消息到 raw_messages 表。

    对每条消息执行 INSERT OR REPLACE（serverID 去重）。

    Args:
        db: 数据库连接
        messages: 消息字典列表，每条含 server_id, sender, content 等字段
        date: 日期 YYYYMMDD
        group_id: 群组标识符（groups 表 PK）

    Returns:
        成功写入的消息数量
    """
    import json

    count = 0
    for msg in messages:
        server_id = msg.get("server_id", "")
        if not server_id:
            continue
        await db.execute(
            """INSERT OR REPLACE INTO raw_messages
               (serverID, date, sender, content, msg_type, image_path, sanitized,
                raw_json, group_id, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                server_id,
                date,
                msg.get("account_name", "") or msg.get("sender", ""),
                msg.get("original_content", msg.get("content", "")),
                msg.get("msg_type", "text"),
                msg.get("media_url", ""),
                1 if msg.get("sanitized") else 0,
                msg.get("raw_json") or json.dumps(msg, ensure_ascii=False),
                group_id,
                msg.get("timestamp", 0),
            ),
        )
        count += 1
    await db.commit()
    return count


async def insert_parsed_contexts(
    db: aiosqlite.Connection,
    contexts: list[dict[str, Any]],
    date: str,
    group_id: str = "",
) -> int:
    """批量 INSERT OR REPLACE 解析后的上下文块到 parsed_contexts 表 (L2).

    T-W12-7: L2 写入函数 — 在 content_enrich 节点末尾调用。
    对每条上下文执行 INSERT OR REPLACE（context_id 去重）。

    P022: 独立的存储层写入函数，与节点业务逻辑零耦合。

    Args:
        db: 数据库连接
        contexts: 上下文字典列表，每条含 context_id, server_ids,
                  context_text, token_count, source_subagent
        date: 日期 YYYYMMDD
        group_id: 群组标识符（groups 表 PK）

    Returns:
        成功写入的上下文块数量
    """
    import json as _json

    count = 0
    for ctx in contexts:
        context_id = ctx.get("context_id", "")
        if not context_id:
            continue
        await db.execute(
            """INSERT OR REPLACE INTO parsed_contexts
               (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                context_id,
                date,
                _json.dumps(ctx.get("server_ids", []), ensure_ascii=False),
                ctx.get("context_text", ""),
                ctx.get("token_count"),
                ctx.get("source_subagent", "content_enrich"),
                group_id,
            ),
        )
        count += 1
    await db.commit()
    return count


async def get_message_stats(
    db: aiosqlite.Connection,
    date: str | None = None,
    *,
    group_id: str | None = None,
) -> dict[str, int]:
    """获取消息统计信息。

    Args:
        db: 数据库连接
        date: 可选日期 YYYYMMDD
        group_id: 可选群组标识符

    Returns:
        Dict with keys: total, sanitized
    """
    if date and group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) as total, SUM(sanitized) as sanitized "
            "FROM raw_messages WHERE date = ? AND group_id = ?",
            (date, group_id),
        )
    elif date:
        cursor = await db.execute(
            "SELECT COUNT(*) as total, SUM(sanitized) as sanitized "
            "FROM raw_messages WHERE date = ?",
            (date,),
        )
    elif group_id:
        cursor = await db.execute(
            "SELECT COUNT(*) as total, SUM(sanitized) as sanitized "
            "FROM raw_messages WHERE group_id = ?",
            (group_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT COUNT(*) as total, SUM(sanitized) as sanitized FROM raw_messages"
        )
    row = await cursor.fetchone()
    total = row[0] if row else 0
    sanitized = row[1] if row and row[1] is not None else 0
    return {"total": total, "sanitized": sanitized}


# ============================================================
# T-W10-E-c: memos_sync_queue write-through functions
# ============================================================


async def enqueue_sync_job(
    db: aiosqlite.Connection,
    op_type: str,
    cube_id: str,
    payload: dict[str, Any],
    priority: int | None = None,
    dedupe_key: str | None = None,
) -> int:
    """Enqueue a sync job into memos_sync_queue for async MemOS sync.

    P022: Storage/Formatting Layer Separation — stores complete JSON payload
    as-is, formatting for MemOS happens only in sync_ops.dispatch_op().

    P009: Backward compatible optional params — priority and dedupe_key
    default to None and are cascaded through the full call chain.

    Args:
        db: aiosqlite database connection.
        op_type: Operation type — add_topic|update_topic|add_feedback|add_edge|update_status.
        cube_id: Target MemCube ID.
        payload: Full JSON payload dict (contains dedupe_key, data, etc.).
        priority: Optional priority hint (reserved, not yet used by worker).
        dedupe_key: Optional dedupe key override; if None, read from payload['dedupe_key'].

    Returns:
        The auto-incremented queue_id of the new row.
    """
    import json as _json

    # P009: dedupe_key cascade — use explicit arg, else payload field, else None
    _dedupe = dedupe_key or payload.get("dedupe_key")
    if _dedupe:
        payload["dedupe_key"] = _dedupe

    cursor = await db.execute(
        """INSERT INTO memos_sync_queue (op_type, cube_id, payload, status)
           VALUES (?, ?, ?, 'pending')""",
        (op_type, cube_id, _json.dumps(payload, ensure_ascii=False)),
    )
    await db.commit()
    queue_id: int = cursor.lastrowid or 0
    logger.debug(
        "enqueue_sync_job: queue_id=%d op=%s cube=%s dedupe=%s",
        queue_id,
        op_type,
        cube_id,
        _dedupe,
    )
    return queue_id


async def fetch_pending_jobs(
    db: aiosqlite.Connection,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Fetch pending jobs from memos_sync_queue ordered by enqueued_at.

    P022: Returns raw rows with JSON payload string — caller (sync_worker)
    handles deserialization and formatting.

    Backlog detection per spec:
      - > 5000 pending → WARNING log
      - > 10000 pending → ERROR log

    Args:
        db: aiosqlite database connection.
        limit: Maximum number of jobs to fetch (default 20 per batch spec).

    Returns:
        List of dict rows with keys: queue_id, enqueued_at, op_type, cube_id,
        payload, status, retry_count, last_error, processed_at.
    """
    # Backlog detection: count total pending first
    cursor = await db.execute("SELECT COUNT(*) FROM memos_sync_queue WHERE status = 'pending'")
    row = await cursor.fetchone()
    pending_count: int = row[0] if row else 0
    if pending_count > 10000:
        logger.error("memos_sync_queue backlog CRITICAL: %d pending jobs", pending_count)
    elif pending_count > 5000:
        logger.warning("memos_sync_queue backlog HIGH: %d pending jobs", pending_count)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT queue_id, enqueued_at, op_type, cube_id, payload, status,
                  retry_count, last_error, processed_at
           FROM memos_sync_queue
           WHERE status = 'pending'
           ORDER BY enqueued_at ASC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def mark_processing(db: aiosqlite.Connection, queue_id: int) -> None:
    """Mark a sync queue job as 'processing'.

    Args:
        db: aiosqlite database connection.
        queue_id: The queue row ID to mark.
    """
    await db.execute(
        "UPDATE memos_sync_queue SET status = 'processing' WHERE queue_id = ?",
        (queue_id,),
    )
    await db.commit()


async def mark_done(db: aiosqlite.Connection, queue_id: int) -> None:
    """Mark a sync queue job as 'done'.

    Args:
        db: aiosqlite database connection.
        queue_id: The queue row ID to mark.
    """
    await db.execute(
        """UPDATE memos_sync_queue
           SET status = 'done', processed_at = datetime('now')
           WHERE queue_id = ?""",
        (queue_id,),
    )
    await db.commit()


async def mark_failed(
    db: aiosqlite.Connection,
    queue_id: int,
    error: str,
    retry_count: int,
) -> str:
    """Mark a sync queue job as failed or retry.

    - If retry_count < 3: resets status to 'pending' for retry
    - If retry_count >= 3: marks status as 'failed' permanently

    Args:
        db: aiosqlite database connection.
        queue_id: The queue row ID to mark.
        error: Error message to record.
        retry_count: Current retry count (before this attempt).

    Returns:
        New status string ('pending' or 'failed').
    """
    new_retry: int = retry_count + 1
    if new_retry < 3:
        await db.execute(
            """UPDATE memos_sync_queue
               SET status = 'pending', retry_count = ?, last_error = ?
               WHERE queue_id = ?""",
            (new_retry, error, queue_id),
        )
        await db.commit()
        logger.warning(
            "memos_sync_queue: job %d retry %d/3 — %s",
            queue_id,
            new_retry,
            error,
        )
        return "pending"
    else:
        await db.execute(
            """UPDATE memos_sync_queue
               SET status = 'failed', retry_count = ?, last_error = ?,
                   processed_at = datetime('now')
               WHERE queue_id = ?""",
            (new_retry, error, queue_id),
        )
        await db.commit()
        logger.error(
            "memos_sync_queue: job %d permanently failed after %d retries — %s",
            queue_id,
            new_retry,
            error,
        )
        return "failed"


async def get_sync_queue_stats(db: aiosqlite.Connection) -> dict[str, int]:
    """Get memos_sync_queue statistics.

    Args:
        db: aiosqlite database connection.

    Returns:
        Dict with keys: pending, processing, done, failed, total.
    """
    cursor = await db.execute(
        """SELECT status, COUNT(*) as cnt
           FROM memos_sync_queue
           GROUP BY status"""
    )
    rows = await cursor.fetchall()
    stats: dict[str, int] = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0}
    for row in rows:
        status_val: str = row[0]
        cnt: int = row[1]
        stats[status_val] = cnt
        stats["total"] += cnt
    return stats


# ============================================================
# T-W12-13: Incremental reprocessing — feedback + L3->L2 mapping
# ============================================================


async def get_unconsumed_feedback(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
) -> list[dict[str, Any]]:
    """Get unconsumed feedback events for a group+date.

    T-W12-13: Reads feedback_events WHERE consumed_at IS NULL, ordered
    by created_at ASC (oldest first -- FIFO processing).

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier.
        date: Date string YYYYMMDD.

    Returns:
        List of dict rows for unconsumed feedback events.
    """
    # M4: 日期格式兼容——前端存 feedback_events.date 为 YYYY-MM-DD（normDate 后），
    # 而 report_versions.date 为 YYYYMMDD。两种都查，避免 regenerate 漏掉反馈
    # （既不消费也进不了 feedback_hints）。
    date_compact = date.replace("-", "") if date else date
    date_dashed = (
        f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"
        if date_compact and len(date_compact) == 8 and date_compact.isdigit()
        else date
    )
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT * FROM feedback_events
           WHERE group_id = ? AND date IN (?, ?) AND consumed_at IS NULL
           ORDER BY created_at ASC""",
        (group_id, date_compact, date_dashed),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_l2_contexts_by_server_ids(
    db: aiosqlite.Connection,
    server_ids: list[str],
) -> list[dict[str, Any]]:
    """Query parsed_contexts (L2) that contain any of the given server_ids.

    T-W12-13: L3->L2 mapping via source_server_ids. L005: server_ids
    are stored as JSON arrays in parsed_contexts.server_ids. We use
    JSON string matching for each server_id.

    L005: Preserves order and deduplicates results.

    Args:
        db: aiosqlite database connection.
        server_ids: List of serverID values from L3 source_server_ids.

    Returns:
        List of unique parsed_contexts dicts matching any server_id.
    """
    if not server_ids:
        return []

    db.row_factory = aiosqlite.Row
    # L005: JSON text search -- server_ids stored as JSON array strings.
    # Use LIKE for each server_id to find matching contexts.
    # Deduplicate via context_id ordering.
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for sid in server_ids:
        cursor = await db.execute(
            """SELECT * FROM parsed_contexts
               WHERE server_ids LIKE ?
               ORDER BY context_id""",
            (f'%"{sid}"%',),
        )
        rows = await cursor.fetchall()
        for row in rows:
            d = dict(row)
            ctx_id = d.get("context_id", "")
            if ctx_id and ctx_id not in seen_ids:
                seen_ids.add(ctx_id)
                results.append(d)

    return results


async def mark_feedback_consumed(
    db: aiosqlite.Connection,
    feedback_id: str,
    consumed_by: str = "incremental_reprocess",
) -> bool:
    """Mark a feedback event as consumed (successfully processed).

    T-W12-13: feedback state machine -- unconsumed -> consumed (success).
    Sets consumed_at to current timestamp and consumed_by to the processor.

    Args:
        db: aiosqlite database connection.
        feedback_id: The feedback_id to mark.
        consumed_by: Name of the consuming process.

    Returns:
        True if a row was updated, False if feedback_id not found.
    """
    cursor = await db.execute(
        """UPDATE feedback_events
           SET consumed_at = datetime('now'), consumed_by = ?
           WHERE feedback_id = ? AND consumed_at IS NULL""",
        (consumed_by, feedback_id),
    )
    await db.commit()
    return bool(cursor.rowcount > 0)


async def mark_feedback_rollback(
    db: aiosqlite.Connection,
    feedback_id: str,
) -> bool:
    """Rollback a consumed feedback event back to unconsumed.

    T-W12-13: feedback state machine -- consumed -> unconsumed (rollback).
    Sets consumed_at = NULL and consumed_by = NULL.

    Args:
        db: aiosqlite database connection.
        feedback_id: The feedback_id to rollback.

    Returns:
        True if a row was updated, False if feedback_id not found or
        already unconsumed.
    """
    cursor = await db.execute(
        """UPDATE feedback_events
           SET consumed_at = NULL, consumed_by = NULL
           WHERE feedback_id = ? AND consumed_at IS NOT NULL""",
        (feedback_id,),
    )
    await db.commit()
    return bool(cursor.rowcount > 0)


async def update_feedback_provenance(
    db: aiosqlite.Connection,
    feedback_id: str,
    *,
    target_version_id: str | None = None,
    target_topic_id: str | None = None,
    produced_version_id: str | None = None,
    memos_cube_id: str | None = None,
    memos_node_id: str | None = None,
    archived_memos_id: str | None = None,
    status: str | None = None,
) -> bool:
    """M4: 回填反馈事件的溯源四元组字段。

    仅更新非 None 的字段（其余保持）。供 regenerate 回填闭环与回滚联动使用。

    Args:
        feedback_id: 反馈事件 PK。
        target_version_id / target_topic_id / produced_version_id: 版本/议题定位。
        memos_cube_id / memos_node_id / archived_memos_id: MemOS 节点溯源。
        status: 'active' | 'rolled_back'。

    Returns:
        True 若有行被更新。
    """
    sets: list[str] = []
    params: list[Any] = []
    for col, val in (
        ("target_version_id", target_version_id),
        ("target_topic_id", target_topic_id),
        ("produced_version_id", produced_version_id),
        ("memos_cube_id", memos_cube_id),
        ("memos_node_id", memos_node_id),
        ("archived_memos_id", archived_memos_id),
        ("status", status),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return False
    params.append(feedback_id)
    cursor = await db.execute(
        f"UPDATE feedback_events SET {', '.join(sets)} WHERE feedback_id = ?",
        params,
    )
    await db.commit()
    return bool(cursor.rowcount > 0)


async def get_feedback(db: aiosqlite.Connection, feedback_id: str) -> dict[str, Any] | None:
    """M4: 取单条反馈事件（全字段）。供溯源/回填读取。"""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM feedback_events WHERE feedback_id = ?",
        (feedback_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_topic_summary_text(
    db: aiosqlite.Connection,
    summary_id: str,
    summary_text: str,
) -> bool:
    """Update the summary_text of a topic_summaries record.

    T-W12-13: Incremental reprocessing only updates affected L3 records.
    A002: Actually writes to disk (execute + commit), not just in-memory.

    Args:
        db: aiosqlite database connection.
        summary_id: The summary_id to update.
        summary_text: New summary text (JSON string).

    Returns:
        True if a row was updated, False if summary_id not found.
    """
    cursor = await db.execute(
        "UPDATE topic_summaries SET summary_text = ? WHERE summary_id = ?",
        (summary_text, summary_id),
    )
    await db.commit()
    return bool(cursor.rowcount > 0)
