"""Custom tables migration — add custom_tables blob column to groups.

CT-1: Database migration for per-group custom table configuration.
The custom_tables blob stores `{kind: {enabled: bool, config: dict}}`
matching the feishu_tables blob structure.

P052: Idempotent — uses PRAGMA table_info to check column existence.
P014: NEVER-throw — migration failure only logs, never blocks caller.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate_groups_add_custom_tables_blob(db: aiosqlite.Connection) -> None:
    """Add the per-group ``custom_tables`` JSON blob column.

    The blob ``{kind: {enabled: bool, config: dict}}`` matches the
    feishu_tables blob structure for consistency.

    P052: Idempotent — checks PRAGMA table_info before ALTER TABLE.
    P014: NEVER-throw — migration failure only logs, never blocks caller.

    Args:
        db: aiosqlite database connection.
    """
    try:
        cursor = await db.execute("PRAGMA table_info(groups)")
        rows = await cursor.fetchall()
        existing_cols = {r[1] for r in rows}
        if "custom_tables" not in existing_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN custom_tables TEXT")
            logger.info("migrate: added custom_tables column to groups")
        await db.commit()
        logger.debug("migrate_groups_add_custom_tables_blob: column ensured")
    except Exception as exc:
        logger.warning("migrate_groups_add_custom_tables_blob: migration skipped — %s", exc)
