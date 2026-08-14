-- Migration 003: core_topics table
-- Design doc §3.2.4b — 核心议题追踪表（多群聊 topic matching）
-- W9: schema only, W10-A writes last_matched_date and match_count
-- Idempotent: uses CREATE TABLE IF NOT EXISTS
--
-- core_topics: 每行一个核心议题定义（12 字段）
--   - core_topic_id: 'core-{group_id}-{seq}' 格式
--   - group_id FK → groups(group_id) ON DELETE CASCADE
--   - keywords: JSON 数组，用于 W10-A 关键词匹配
--   - is_active: 软删开关 (1=active, 0=deleted)
--   - priority: 1=普通, 2=最高

PRAGMA foreign_keys = ON;

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
