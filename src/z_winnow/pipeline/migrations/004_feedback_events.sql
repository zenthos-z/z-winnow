-- P043: Migration 004 — feedback_events table per design doc §3.1.2
-- Stores administrator feedback events for the feedback loop (W9-C).
-- Idempotent: uses CREATE TABLE IF NOT EXISTS
--
-- feedback_events: 每行一个管理员反馈事件（19 字段）
--   - feedback_id: 'fb-{date}-{seq}' 格式
--   - group_id: 群聊隔离（决策 G2：per-group 上下文）
--   - target_type: report|topic|trend|resource|engineering
--   - target_path: JSONPath 定位到报告的哪个部分
--   - signal: positive|negative|partial
--   - tags: JSON 数组，枚举值见设计文档 §3.1.2 表（9 个）
--   - correction_mode: tag_only|free_text|inline_edit
--   - consumed_at: NULL=未消费，非 NULL=已被 prompt 闭环消费

CREATE TABLE IF NOT EXISTS feedback_events (
    feedback_id     TEXT PRIMARY KEY,                        -- 'fb-{date}-{seq}'
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),

    -- 定位：反馈针对什么
    group_id        TEXT NOT NULL,                           -- 群聊隔离（决策 G2）
    date            TEXT NOT NULL,                           -- YYYY-MM-DD（报告日期）
    report_id       TEXT NOT NULL,                           -- 报告唯一 id
    target_type     TEXT NOT NULL,                           -- report|topic|trend|resource|engineering
    target_id       TEXT,                                    -- 议题/资源 id；report 级为 NULL
    target_path     TEXT,                                    -- JSONPath，如 $.topic_sections[2].summary

    -- 信号：好或坏
    signal          TEXT NOT NULL,                           -- positive|negative|partial
    severity        INTEGER DEFAULT 1,                       -- 1=轻微 2=明显 3=严重
    rating          INTEGER,                                 -- 1-5 星，仅 L0 可用

    -- 语义纠偏（核心）：三元组（原文 + 修正 + 原因）
    tags                TEXT,                                -- JSON 数组 ["fact_error", "missing"]
    correction_mode     TEXT NOT NULL,                       -- tag_only|free_text|inline_edit
    original_text       TEXT,                                -- 被反馈的原文（系统自动捕获）
    corrected_text      TEXT,                                -- 修正文本（inline_edit/free_text 写入）
    correction_note     TEXT,                                -- 管理员补充说明（"为什么改"）

    -- 元数据
    reporter        TEXT NOT NULL DEFAULT 'admin',           -- 决策 D2: 单账号
    consumed_at     TEXT,                                    -- NULL=未消费；非 NULL=已被 prompt 闭环消费
    consumed_by     TEXT                                     -- 'unified_reporter'|'topic_tracker'|'llm_judge'
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_feedback_group_date
    ON feedback_events(group_id, date);

CREATE INDEX IF NOT EXISTS idx_feedback_target
    ON feedback_events(target_type, target_id);

-- B2: Partial index — only indexes unconsumed feedback rows
CREATE INDEX IF NOT EXISTS idx_feedback_unconsumed
    ON feedback_events(consumed_at) WHERE consumed_at IS NULL;
