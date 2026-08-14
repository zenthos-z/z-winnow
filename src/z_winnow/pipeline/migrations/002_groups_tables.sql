-- Migration 002: groups + group_members tables
-- Design doc §3.2.4 — 多群聊管理的元数据中心
-- Idempotent: uses CREATE TABLE IF NOT EXISTS
--
-- groups: 每行一个群聊配置（14 字段）
-- group_members: 每群的人员配置，多对多角色
--   - UNIQUE(group_id, wxid) 防止重复
--   - ON DELETE CASCADE: 删除 groups 行 → 自动清理其 members

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
    group_id              TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    chatroom_id           TEXT NOT NULL,
    output_dir            TEXT,
    feishu_enabled        INTEGER DEFAULT 0,
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
