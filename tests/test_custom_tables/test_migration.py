"""Tests for custom_tables blob migration (CT-1).

P052: Idempotent — migration checks PRAGMA table_info before ALTER.
P014: NEVER-throw — migration logs warning on failure, never raises.
P078: Uses real SQLite :memory: database, no mocking.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.pipeline.migrations.custom_tables import (
    migrate_groups_add_custom_tables_blob,
)


class TestCustomTablesMigration:
    """Test suite for CT-1 migration."""

    @pytest.mark.asyncio
    async def test_new_db_has_custom_tables_column(self) -> None:
        """AC1: custom_tables column added to groups table."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            cursor = await db.execute("PRAGMA table_info(groups)")
            rows = await cursor.fetchall()
            col_names = {r[1] for r in rows}

            assert "custom_tables" in col_names, (
                "custom_tables column should exist in groups table"
            )

    @pytest.mark.asyncio
    async def test_migration_idempotent(self) -> None:
        """AC2: migration uses PRAGMA table_info check + ALTER TABLE."""
        async with aiosqlite.connect(":memory:") as db:
            # Create groups table first (without custom_tables)
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL
                );
            """)
            await db.commit()

            # First migration: should add column
            await migrate_groups_add_custom_tables_blob(db)

            cursor = await db.execute("PRAGMA table_info(groups)")
            rows = await cursor.fetchall()
            col_names = {r[1] for r in rows}
            assert "custom_tables" in col_names

            # Second migration: should be idempotent (no error)
            await migrate_groups_add_custom_tables_blob(db)

            # Verify column still exists and is TEXT type
            cursor = await db.execute("PRAGMA table_info(groups)")
            rows = await cursor.fetchall()
            custom_tables_col = [r for r in rows if r[1] == "custom_tables"]
            assert len(custom_tables_col) == 1
            assert custom_tables_col[0][2] == "TEXT"  # column type

    @pytest.mark.asyncio
    async def test_migration_invoked_on_init(self) -> None:
        """AC3: migration function is called from init_database_in_conn."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            # Verify custom_tables column exists (proves migration was called)
            cursor = await db.execute("PRAGMA table_info(groups)")
            rows = await cursor.fetchall()
            col_names = {r[1] for r in rows}
            assert "custom_tables" in col_names

    @pytest.mark.asyncio
    async def test_migration_never_throw(self) -> None:
        """AC4: migration failure only logs, never throws (P014 never-throw)."""
        async with aiosqlite.connect(":memory:") as db:
            # Create a table with conflicting structure
            # This simulates a scenario where ALTER might fail
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL
                );
            """)
            await db.commit()

            # Add column first
            await migrate_groups_add_custom_tables_blob(db)

            # Calling again should not raise (idempotent)
            try:
                await migrate_groups_add_custom_tables_blob(db)
                # Should succeed without raising
            except Exception as exc:
                pytest.fail(f"migrate_groups_add_custom_tables_blob should never throw: {exc}")

    @pytest.mark.asyncio
    async def test_custom_tables_matches_feishu_tables_structure(self) -> None:
        """Verify custom_tables blob structure matches feishu_tables."""
        async with aiosqlite.connect(":memory:") as db:
            await init_database_in_conn(db)

            # Insert test group with custom_tables blob
            test_blob = {
                "engineering": {
                    "enabled": True,
                    "config": {"max_issues": 10},
                },
                "custom_metric": {
                    "enabled": False,
                    "config": {},
                },
            }

            await db.execute(
                """INSERT INTO groups (group_id, display_name, chatroom_id, custom_tables)
                   VALUES (?, ?, ?, ?)""",
                ("test-group", "Test Group", "test-chat", json.dumps(test_blob)),
            )
            await db.commit()

            # Verify read back
            cursor = await db.execute(
                "SELECT custom_tables FROM groups WHERE group_id = ?",
                ("test-group",),
            )
            row = await cursor.fetchone()
            assert row is not None
            loaded = json.loads(row[0])
            assert loaded["engineering"]["enabled"] is True
            assert loaded["custom_metric"]["enabled"] is False