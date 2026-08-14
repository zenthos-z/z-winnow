"""Tests for CT-3: output_composer L3 JSON dynamic writing.

Tests verify that compose_json dynamically decides which JSON files to write
based on custom_tables_config, replacing the hardcoded 4-file approach.

P078: Uses real filesystem (tmp_path), no mocking of I/O.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from z_winnow.subagents.output_composer import compose_json
from z_winnow.subagents.output_composer.merger import ComposedData

# ── Shared test data ──────────────────────────────────────────────

SAMPLE_UNIFIED_OUTPUT: dict = {
    "overview": "Test overview",
    "important_notice": "",
    "topics": [
        {"name": "topic-1", "lifecycle": "active", "status": "active"},
    ],
    "trend_analysis": "trend up",
    "trend_summary": "summary",
    "highlights": ["highlight-1"],
    "resources": [
        {"name": "resource-1", "type": "image"},
    ],
    "resource_count_by_type": {"image": 1},
    "custom_tables": {
        "engineering": {
            "issues": [
                {"title": "issue-1", "severity": "low"},
            ],
            "group_summary": {"total": 1},
        }
    },
    "model_used": "test-model",
}


def _read_json(path: Path) -> dict:
    """Read a JSON file and return parsed dict."""
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read())


def _assert_file_exists(path: Path, name: str) -> None:
    assert path.exists(), f"{name} should exist at {path}"


def _assert_file_not_exists(path: Path, name: str) -> None:
    assert not path.exists(), f"{name} should NOT exist at {path}"


# ── AC1 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compose_accepts_custom_tables_config(tmp_path: Path) -> None:
    """AC1: compose_json accepts custom_tables_config parameter."""
    output_dir = tmp_path / "output"

    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
        custom_tables_config={"engineering": {"enabled": True, "config": {}}},
    )

    assert isinstance(result, dict)
    # With engineering enabled, all 4 files should be written
    assert "daily" in result
    assert "resources" in result
    assert "engineering" in result
    assert "topics" in result


# ── AC2 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mandatory_files_always_written(tmp_path: Path) -> None:
    """AC2: mandatory tables (daily/resources/topics) are always written,
    even when engineering is disabled.
    """
    output_dir = tmp_path / "output"

    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
        custom_tables_config={"engineering": {"enabled": False, "config": {}}},
    )

    # Mandatory files must exist
    assert "daily" in result
    assert "resources" in result
    assert "topics" in result
    # Engineering must not exist
    assert "engineering" not in result

    _assert_file_exists(output_dir / "daily.json", "daily")
    _assert_file_exists(output_dir / "resources.json", "resources")
    _assert_file_exists(output_dir / "topics.json", "topics")
    _assert_file_not_exists(output_dir / "engineering.json", "engineering")

    # Verify content of mandatory files
    daily_data = _read_json(output_dir / "daily.json")
    assert daily_data["date"] == "20260714"
    assert daily_data["overview"] == "Test overview"


# ── AC3 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engineering_skipped_when_disabled(tmp_path: Path) -> None:
    """AC3: When custom_tables explicitly disables engineering,
    engineering.json is NOT written.
    """
    output_dir = tmp_path / "output"

    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
        custom_tables_config={"engineering": {"enabled": False, "config": {}}},
    )

    # Engineering should be absent from result and disk
    assert "engineering" not in result
    _assert_file_not_exists(output_dir / "engineering.json", "engineering")

    # But the other 3 files should still exist
    assert "daily" in result
    assert "resources" in result
    assert "topics" in result


# ── AC4 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backward_compat_engineering_written(tmp_path: Path) -> None:
    """AC4: When custom_tables_config is empty dict, engineering.json is
    still written (backward compatibility).
    """
    output_dir = tmp_path / "output"

    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
        custom_tables_config={},
    )

    # All 4 files should be written for backward compat
    assert "daily" in result
    assert "resources" in result
    assert "engineering" in result
    assert "topics" in result

    _assert_file_exists(output_dir / "engineering.json", "engineering")


# ── AC5 ───────────────────────────────────────────────────────────

def test_composed_data_has_custom_tables_field() -> None:
    """AC5: ComposedData includes custom_tables field."""
    ct_config = {"engineering": {"enabled": True, "config": {"max_issues": 10}}}

    composed = ComposedData(
        date="20260714",
        overview="test",
        topics=[],
        resources=[],
        issues=[],
        custom_tables=ct_config,
    )

    assert composed.custom_tables is not None
    assert composed.custom_tables == ct_config
    assert composed.custom_tables["engineering"]["enabled"] is True
    assert composed.custom_tables["engineering"]["config"]["max_issues"] == 10

    # Default is None
    composed_default = ComposedData()
    assert composed_default.custom_tables is None


# ── AC6 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_none_config_fallback(tmp_path: Path) -> None:
    """AC6: When custom_tables_config is None (not provided), all
    mandatory + engineering are written (default fallback).
    """
    output_dir = tmp_path / "output"

    # Call without custom_tables_config at all (defaults to None)
    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
    )

    # All 4 files should be written
    assert "daily" in result
    assert "resources" in result
    assert "engineering" in result
    assert "topics" in result


# ── AC7 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_none_output_placeholder(tmp_path: Path) -> None:
    """AC7: When unified_report_output is None, mandatory placeholder
    files are still written. Engineering placeholder only if enabled.
    """
    # Case A: engineering enabled → 4 placeholders
    output_dir_a = tmp_path / "output_a"
    result_a = await compose_json(
        unified_report_output=None,
        output_dir=output_dir_a,
        date="20260714",
        custom_tables_config={"engineering": {"enabled": True}},
    )

    assert "daily" in result_a
    assert "resources" in result_a
    assert "engineering" in result_a
    assert "topics" in result_a

    # Verify placeholder content
    daily_a = _read_json(output_dir_a / "daily.json")
    assert daily_a.get("placeholder") is True

    # Case B: engineering disabled → 3 placeholders (no engineering)
    output_dir_b = tmp_path / "output_b"
    result_b = await compose_json(
        unified_report_output=None,
        output_dir=output_dir_b,
        date="20260714",
        custom_tables_config={"engineering": {"enabled": False}},
    )

    assert "daily" in result_b
    assert "resources" in result_b
    assert "topics" in result_b
    assert "engineering" not in result_b

    _assert_file_not_exists(output_dir_b / "engineering.json", "engineering")


# ── Edge cases ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_custom_tables_config_with_unknown_table(tmp_path: Path) -> None:
    """Unknown table names in config should not affect mandatory writes.

    A non-empty config that doesn't enable engineering → engineering.json is NOT
    written (custom_tables is the explicit truth source; engineering must be
    enabled, not merely absent-from-disabled-list).
    """
    output_dir = tmp_path / "output"

    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
        custom_tables_config={"future_table": {"enabled": True}},
    )

    # Mandatory files always written
    assert "daily" in result
    assert "resources" in result
    assert "topics" in result
    # engineering not enabled in this config → not written
    assert "engineering" not in result


@pytest.mark.asyncio
async def test_custom_tables_config_empty_engineering_dict(tmp_path: Path) -> None:
    """engineering entry with no explicit enabled flag → NOT written.

    custom_tables is the explicit truth source: a table must carry
    ``enabled: True`` to be written (consistent with active_kinds /
    kind_enabled_for_report). An empty dict is treated as not-enabled.
    """
    output_dir = tmp_path / "output"

    result = await compose_json(
        unified_report_output=SAMPLE_UNIFIED_OUTPUT,
        output_dir=output_dir,
        date="20260714",
        custom_tables_config={"engineering": {}},  # no "enabled" key
    )

    assert "engineering" not in result
