"""Tests for Web API schema backward compatibility (CT-6).

AC1: GroupOut retains engineering_enabled field
AC2: GroupOut has custom_tables field
AC3: GroupCreate and GroupUpdate have custom_tables
AC4: engineering_enabled has deprecated comment
AC5: group_service normalizes engineering_enabled → custom_tables
"""

from __future__ import annotations

import inspect

import aiosqlite
import pytest

from z_winnow.web.schemas.groups import GroupCreate, GroupOut, GroupUpdate
from z_winnow.web.services.group_service import (
    _normalize_custom_tables,
    get_group_detail,
    list_groups,
)


class TestApiSchemaBackwardCompat:
    """Test suite for CT-6 API schema backward compatibility."""

    # ------------------------------------------------------------------
    # AC1: GroupOut retains engineering_enabled
    # ------------------------------------------------------------------

    def test_group_out_retains_engineering_enabled(self) -> None:
        """AC1: GroupOut model still has engineering_enabled field."""
        assert "engineering_enabled" in GroupOut.model_fields, (
            "GroupOut must retain engineering_enabled for backward compatibility"
        )
        # Default value must be 1 (enabled)
        field = GroupOut.model_fields["engineering_enabled"]
        assert field.default == 1, (
            "engineering_enabled default should be 1 (enabled)"
        )

    # ------------------------------------------------------------------
    # AC2: GroupOut has custom_tables
    # ------------------------------------------------------------------

    def test_group_out_has_custom_tables(self) -> None:
        """AC2: GroupOut model has custom_tables field."""
        assert "custom_tables" in GroupOut.model_fields, (
            "GroupOut must have custom_tables field"
        )
        field = GroupOut.model_fields["custom_tables"]
        assert field.default is None, (
            "custom_tables default should be None"
        )

    # ------------------------------------------------------------------
    # AC3: GroupCreate and GroupUpdate have custom_tables
    # ------------------------------------------------------------------

    def test_group_create_update_has_custom_tables(self) -> None:
        """AC3: GroupCreate and GroupUpdate have custom_tables field."""
        assert "custom_tables" in GroupCreate.model_fields, (
            "GroupCreate must have custom_tables field"
        )
        assert "custom_tables" in GroupUpdate.model_fields, (
            "GroupUpdate must have custom_tables field"
        )

    # ------------------------------------------------------------------
    # AC4: engineering_enabled has deprecated comment
    # ------------------------------------------------------------------

    def test_engineering_enabled_deprecated_comment(self) -> None:
        """AC4: engineering_enabled field is marked deprecated via comment."""
        source = inspect.getsource(GroupOut)
        # The deprecated comment should appear on the line immediately before
        # the engineering_enabled field declaration in GroupOut
        assert "deprecated" in source.lower(), (
            "engineering_enabled in GroupOut must have a deprecated comment"
        )
        # Specifically check for "use custom_tables.engineering.enabled"
        assert "custom_tables.engineering.enabled" in source, (
            "deprecated comment must reference custom_tables.engineering.enabled"
        )

    # ------------------------------------------------------------------
    # AC5: group_service normalizes engineering_enabled → custom_tables
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_group_service_normalizes_custom_tables(self) -> None:
        """AC5: group_service read paths normalize engineering_enabled into custom_tables."""
        # --- Pure function test ---
        # GroupOut with default values (custom_tables=None, engineering_enabled=1)
        go = GroupOut(group_id="test-1", display_name="Test", chatroom_id="cid")
        assert go.custom_tables is None
        assert go.engineering_enabled == 1

        normalized = _normalize_custom_tables(go)
        assert normalized.custom_tables is not None
        assert isinstance(normalized.custom_tables, dict)
        assert "engineering" in normalized.custom_tables
        assert normalized.custom_tables["engineering"]["enabled"] is True

        # GroupOut with engineering_enabled=0
        go2 = GroupOut(
            group_id="test-2",
            display_name="Test2",
            chatroom_id="cid2",
            engineering_enabled=0,
        )
        normalized2 = _normalize_custom_tables(go2)
        assert normalized2.custom_tables["engineering"]["enabled"] is False

        # GroupOut with existing custom_tables (engineering missing)
        go3 = GroupOut(
            group_id="test-3",
            display_name="Test3",
            chatroom_id="cid3",
            engineering_enabled=1,
            custom_tables={"summary": {"enabled": True, "config": {}}},
        )
        normalized3 = _normalize_custom_tables(go3)
        assert "engineering" in normalized3.custom_tables
        assert normalized3.custom_tables["engineering"]["enabled"] is True
        assert "summary" in normalized3.custom_tables  # preserved

        # GroupOut with existing custom_tables (engineering already present)
        go4 = GroupOut(
            group_id="test-4",
            display_name="Test4",
            chatroom_id="cid4",
            engineering_enabled=0,
            custom_tables={"engineering": {"enabled": True, "config": {"foo": 1}}},
        )
        normalized4 = _normalize_custom_tables(go4)
        # engineering.enabled must NOT be overwritten (takes precedence)
        assert normalized4.custom_tables["engineering"]["enabled"] is True

    # ------------------------------------------------------------------
    # Integration: normalization through list_groups / get_group_detail
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_normalization_integration_list_groups(self) -> None:
        """Verify normalization is applied in the list_groups read path."""
        async with aiosqlite.connect(":memory:") as db:
            # Setup a minimal groups table matching the schema
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    chatroom_id TEXT NOT NULL DEFAULT '',
                    output_dir TEXT,
                    feishu_enabled INTEGER DEFAULT 0,
                    feishu_base_token TEXT,
                    feishu_table_summary TEXT,
                    feishu_table_topics TEXT,
                    feishu_table_resources TEXT,
                    feishu_table_engineering TEXT,
                    feishu_framework_initialized INTEGER DEFAULT 0,
                    feishu_engineering_enabled INTEGER DEFAULT 1,
                    engineering_enabled INTEGER DEFAULT 1,
                    feishu_tables TEXT,
                    custom_tables TEXT,
                    custom_prompt_hints TEXT,
                    is_active INTEGER DEFAULT 1,
                    daily_report_enabled INTEGER DEFAULT 1,
                    daily_schedule_cron TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    created_by TEXT
                );
            """)
            await db.commit()

            # Insert a group with custom_tables=NULL (legacy state)
            await db.execute(
                """INSERT INTO groups (group_id, display_name, chatroom_id, engineering_enabled)
                   VALUES (?, ?, ?, ?)""",
                ("g-legacy", "Legacy Group", "cid-legacy", 1),
            )
            await db.commit()

            result = await list_groups(db, page=1, page_size=10)
            assert result.total == 1
            group = result.items[0]
            assert group.engineering_enabled == 1
            # Normalization should have populated custom_tables
            assert group.custom_tables is not None
            assert isinstance(group.custom_tables, dict)
            assert "engineering" in group.custom_tables
            assert group.custom_tables["engineering"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_normalization_integration_get_group_detail(self) -> None:
        """Verify normalization is applied in the get_group_detail read path."""
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    chatroom_id TEXT NOT NULL DEFAULT '',
                    output_dir TEXT,
                    feishu_enabled INTEGER DEFAULT 0,
                    feishu_base_token TEXT,
                    feishu_table_summary TEXT,
                    feishu_table_topics TEXT,
                    feishu_table_resources TEXT,
                    feishu_table_engineering TEXT,
                    feishu_framework_initialized INTEGER DEFAULT 0,
                    feishu_engineering_enabled INTEGER DEFAULT 1,
                    engineering_enabled INTEGER DEFAULT 1,
                    feishu_tables TEXT,
                    custom_tables TEXT,
                    custom_prompt_hints TEXT,
                    is_active INTEGER DEFAULT 1,
                    daily_report_enabled INTEGER DEFAULT 1,
                    daily_schedule_cron TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    created_by TEXT
                );
            """)
            await db.commit()

            # Insert group with engineering_enabled=0, no custom_tables
            await db.execute(
                """INSERT INTO groups (group_id, display_name, chatroom_id, engineering_enabled)
                   VALUES (?, ?, ?, ?)""",
                ("g-disabled", "Disabled Eng", "cid-dis", 0),
            )
            await db.commit()

            group = await get_group_detail(db, "g-disabled")
            assert group is not None
            assert group.engineering_enabled == 0
            assert group.custom_tables is not None
            assert group.custom_tables["engineering"]["enabled"] is False

            # Group not found → None
            group_missing = await get_group_detail(db, "no-such-group")
            assert group_missing is None
