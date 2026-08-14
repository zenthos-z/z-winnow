-- Migration 005: Extended pipeline_runs columns for graph progress tracking
-- Spec §3.6.1 — pipeline_runs needs group_id, date, current_node, progress_pct, node_history
-- Idempotent: uses ALTER TABLE with existence checks via try/catch semantics
-- (SQLite does not support IF NOT EXISTS for ALTER TABLE, so these are written as
--  separate statements that fail silently if column already exists.)

-- Note: These ALTER TABLE statements are executed by the application at startup;
-- for new databases, the columns are created directly by SCHEMA_SQL in database.py.

ALTER TABLE pipeline_runs ADD COLUMN group_id TEXT DEFAULT '';
ALTER TABLE pipeline_runs ADD COLUMN date TEXT DEFAULT '';
ALTER TABLE pipeline_runs ADD COLUMN current_node TEXT;
ALTER TABLE pipeline_runs ADD COLUMN progress_pct INTEGER DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN node_history TEXT DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_group_date ON pipeline_runs(group_id, date);
