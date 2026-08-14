"""Feishu adapter tests for custom_tables support.

Tests for CT-5: active_kinds custom_tables parameter support.
"""

import pytest

from z_winnow.pipeline.feishu import schema


class TestActiveKindsCustomTables:
    """Tests for active_kinds accepting custom_tables_config parameter."""

    def test_active_kinds_accepts_custom_tables(self):
        """AC1: active_kinds accepts custom_tables_config parameter."""
        # Basic call - should not raise
        result = schema.active_kinds(
            tables_config={"engineering": {"enabled": True, "table_id": ""}},
            custom_tables_config={"engineering": {"enabled": True}},
        )
        assert isinstance(result, list)

    def test_custom_tables_controls_engineering(self):
        """AC2: custom_tables.engineering.enabled correctly controls engineering table."""
        # custom_tables enabled → engineering in result
        result = schema.active_kinds(
            tables_config={},
            custom_tables_config={"engineering": {"enabled": True}},
        )
        assert "engineering" in result

        # custom_tables disabled → engineering not in result
        result = schema.active_kinds(
            tables_config={},
            custom_tables_config={"engineering": {"enabled": False}},
        )
        assert "engineering" not in result

    def test_legacy_engineering_enabled_fallback(self):
        """AC3: engineering_enabled True and custom_tables empty → engineering table still uploaded."""
        # When custom_tables_config is None, fall back to tables_config
        result = schema.active_kinds(
            tables_config={"engineering": {"enabled": True, "table_id": "xxx"}},
            custom_tables_config=None,
        )
        assert "engineering" in result

        # When custom_tables_config is empty dict, fall back to tables_config
        result = schema.active_kinds(
            tables_config={"engineering": {"enabled": True, "table_id": "xxx"}},
            custom_tables_config={},
        )
        assert "engineering" in result

    def test_custom_tables_priority_over_tables_config(self):
        """custom_tables_config takes priority over tables_config for same kind."""
        # tables_config says enabled, custom_tables says disabled → disabled wins
        result = schema.active_kinds(
            tables_config={"engineering": {"enabled": True, "table_id": "xxx"}},
            custom_tables_config={"engineering": {"enabled": False}},
        )
        assert "engineering" not in result

        # tables_config says disabled, custom_tables says enabled → enabled wins
        result = schema.active_kinds(
            tables_config={"engineering": {"enabled": False, "table_id": ""}},
            custom_tables_config={"engineering": {"enabled": True}},
        )
        assert "engineering" in result

    def test_mandatory_kinds_always_present(self):
        """Mandatory kinds (summary, topics, resources) always present regardless of config."""
        result = schema.active_kinds(
            tables_config={},
            custom_tables_config={},
        )
        assert "summary" in result
        assert "topics" in result
        assert "resources" in result

    def test_unknown_kinds_ignored(self):
        """Unknown kinds in custom_tables_config are ignored."""
        result = schema.active_kinds(
            tables_config={},
            custom_tables_config={"unknown_kind": {"enabled": True}},
        )
        assert "unknown_kind" not in result


class TestEnsureFrameworkCustomTables:
    """Tests for ensure_framework accepting custom_tables_config."""

    @pytest.mark.asyncio
    async def test_ensure_framework_accepts_custom_tables(self):
        """ensure_framework accepts custom_tables_config parameter."""
        from z_winnow.pipeline.feishu.uploader import ensure_framework

        # Basic call with custom_tables_config - should not raise
        result = await ensure_framework(
            base_name="Test Base",
            base_token="",
            tables_config={},
            custom_tables_config={"engineering": {"enabled": False}},
            mock=True,
        )
        assert "status" in result


class TestUploadGroupDayDeprecatedParam:
    """Tests for upload_group_day deprecated engineering_enabled parameter."""

    @pytest.mark.asyncio
    async def test_upload_group_day_deprecated_param(self):
        """AC4: upload_group_day engineering_enabled param deprecated but preserved."""
        from z_winnow.pipeline.feishu.uploader import upload_group_day

        # Call with legacy engineering_enabled param - should work
        result = await upload_group_day(
            base_token="test_token",
            tables_config={"engineering": {"enabled": True, "table_id": "xxx"}},
            l3_data={},
            date="20260714",
            engineering_enabled=False,  # Legacy param
            mock=True,
        )
        assert "status" in result

    @pytest.mark.asyncio
    async def test_upload_group_day_custom_tables_override(self):
        """custom_tables_config overrides engineering_enabled param."""
        from z_winnow.pipeline.feishu.uploader import upload_group_day

        # custom_tables disabled overrides engineering_enabled=True
        result = await upload_group_day(
            base_token="test_token",
            tables_config={"engineering": {"enabled": True, "table_id": "xxx"}},
            l3_data={},
            date="20260714",
            engineering_enabled=True,
            custom_tables_config={"engineering": {"enabled": False}},
            mock=True,
        )
        assert "status" in result


class TestEnsureFrameworkBackwardCompat:
    """Tests for ensure_framework backward compatibility."""

    @pytest.mark.asyncio
    async def test_ensure_framework_backward_compat(self):
        """AC5: ensure_framework creates tables correctly when custom_tables is empty."""
        from z_winnow.pipeline.feishu.uploader import ensure_framework

        # When custom_tables_config is None/empty, should use tables_config
        result = await ensure_framework(
            base_name="Test Base",
            base_token="",
            tables_config={"engineering": {"enabled": True, "table_id": ""}},
            custom_tables_config=None,
            mock=True,
        )
        assert result["status"] in ("ok", "skipped")
        # engineering should be in tables_config result
        assert "engineering" in result["tables_config"]