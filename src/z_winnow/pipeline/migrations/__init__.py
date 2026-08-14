"""Pipeline database migrations — idempotent schema evolution.

P052: Python-level idempotent SQLite migration framework.
P014: NEVER-throw — all migrations log warnings on failure, never propagate exceptions.

Each migration function takes an aiosqlite.Connection and uses PRAGMA table_info
for per-column existence guards, making them safe to run N times on the same DB.
"""

from .custom_tables import migrate_groups_add_custom_tables_blob
from .run_merge import (
    create_async_tasks_table,
    drop_weekly_report_enabled_column,
    migrate_feedback_provenance,
    migrate_report_versions_add_cover_and_judge,
    migrate_runs_merge,
)

__all__ = [
    "create_async_tasks_table",
    "drop_weekly_report_enabled_column",
    "migrate_feedback_provenance",
    "migrate_groups_add_custom_tables_blob",
    "migrate_report_versions_add_cover_and_judge",
    "migrate_runs_merge",
]
