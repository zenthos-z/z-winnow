"""T-W12-10: test_markdown_export — verify export_markdown entry point.

B3: export_markdown works end-to-end: L3 JSON -> Jinja2 -> .md -> update content.
R1: Real pipeline verification — no mock filesystem.

Tests:
1. export_markdown raises FileNotFoundError when L3 JSON missing
2. export_markdown renders Markdown from L3 JSON and writes .md
3. export_markdown updates report_versions.content in SQLite
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

# ============================================================
# Fixtures
# ============================================================

DATE = "20260520"
GROUP_ID = "test-group"

SAMPLE_DAILY: dict[str, Any] = {
    "date": DATE,
    "overview": "Test overview for export",
    "topic_sections": [
        {"topic_name": "Testing", "status": "active", "trend": "stable"},
    ],
    "highlights": ["Test highlight"],
    "trend_analysis": {"current_phase": "testing"},
    "important_notice": "",
}

SAMPLE_RESOURCES: dict[str, Any] = {
    "date": DATE,
    "resources": [
        {"resource_type": "link", "summary": "Test resource", "content": "https://example.com"},
    ],
    "total_count": 1,
    "count_by_type": {"link": 1},
}

SAMPLE_ENGINEERING: dict[str, Any] = {
    "date": DATE,
    "engineering_issues": [],
    "group_summary": {},
    "model_used": "test",
}

SAMPLE_TOPICS: dict[str, Any] = {
    "date": DATE,
    "topics": [],
    "total_active": 0,
}


@pytest.fixture
def l3_json_dir(tmp_path: Path) -> Path:
    """Create a temporary L3 JSON directory with sample data."""
    json_dir = tmp_path / "data" / "processed" / GROUP_ID / DATE
    json_dir.mkdir(parents=True, exist_ok=True)

    (json_dir / "daily.json").write_text(
        json.dumps(SAMPLE_DAILY, ensure_ascii=False), encoding="utf-8"
    )
    (json_dir / "resources.json").write_text(
        json.dumps(SAMPLE_RESOURCES, ensure_ascii=False), encoding="utf-8"
    )
    (json_dir / "engineering.json").write_text(
        json.dumps(SAMPLE_ENGINEERING, ensure_ascii=False), encoding="utf-8"
    )
    (json_dir / "topics.json").write_text(
        json.dumps(SAMPLE_TOPICS, ensure_ascii=False), encoding="utf-8"
    )

    return json_dir


@pytest.fixture
def db_with_version(tmp_path: Path) -> str:
    """Create a temporary SQLite DB with a report_versions entry (content=NULL)."""
    import aiosqlite

    db_path = str(tmp_path / "test.db")

    async def _setup():
        from z_winnow.pipeline.database import init_database_in_conn
        from z_winnow.pipeline.report_version import create_version

        async with aiosqlite.connect(db_path) as db:
            await init_database_in_conn(db)
            await create_version(
                db,
                report_id=f"{GROUP_ID}-{DATE}",
                group_id=GROUP_ID,
                date=DATE,
                content=None,  # Phase E: NULL
                source="daily_run",
            )

    import asyncio

    asyncio.run(_setup())
    return db_path


# ============================================================
# B3/B4 tests — export_markdown
# ============================================================


class TestExportMarkdown:
    """Tests for T-W12-10: export_markdown Phase H entry point."""

    @pytest.mark.asyncio
    async def test_export_raises_on_missing_l3_json(self, tmp_path: Path):
        """export_markdown raises FileNotFoundError when L3 JSON directory missing."""
        from z_winnow.config.settings import reset_settings
        from z_winnow.graph.builder import export_markdown

        # Point to non-existent directory
        os.environ["LAYER3_OUTPUT_DIR"] = str(tmp_path / "nonexistent")
        reset_settings()
        try:
            with pytest.raises(FileNotFoundError, match="L3 JSON directory not found"):
                await export_markdown(date="20260101", group_id="missing-group")
        finally:
            os.environ.pop("LAYER3_OUTPUT_DIR", None)
            reset_settings()

    @pytest.mark.asyncio
    async def test_export_renders_markdown_file(self, tmp_path: Path, l3_json_dir: Path):
        """B3: export_markdown renders .md file from L3 JSON."""
        from z_winnow.config.settings import reset_settings
        from z_winnow.graph.builder import export_markdown

        # Set correct LAYER3_OUTPUT_DIR to our temp dir
        os.environ["LAYER3_OUTPUT_DIR"] = str(tmp_path / "data" / "processed")
        reset_settings()

        # Point DB to non-existent path (no version to update, but export still works)
        os.environ["SQLITE_DB_PATH"] = str(tmp_path / "nonexistent.db")

        try:
            md_path = await export_markdown(date=DATE, group_id=GROUP_ID)
            assert md_path.exists(), "Markdown file should exist"
            assert md_path.name == "report.md"

            content = md_path.read_text(encoding="utf-8")
            assert len(content) > 0, "Markdown content should not be empty"
            # Should contain the overview text
            assert "Test overview for export" in content
        finally:
            os.environ.pop("LAYER3_OUTPUT_DIR", None)
            os.environ.pop("SQLITE_DB_PATH", None)
            reset_settings()

    @pytest.mark.asyncio
    async def test_export_updates_report_version_content(
        self, tmp_path: Path, l3_json_dir: Path, db_with_version: str
    ):
        """B3/R1: export_markdown updates report_versions.content in DB."""
        import aiosqlite

        from z_winnow.config.settings import reset_settings
        from z_winnow.graph.builder import export_markdown
        from z_winnow.pipeline.database import init_database_in_conn
        from z_winnow.pipeline.report_version import get_latest_version

        os.environ["LAYER3_OUTPUT_DIR"] = str(tmp_path / "data" / "processed")
        reset_settings()
        os.environ["SQLITE_DB_PATH"] = db_with_version

        try:
            md_path = await export_markdown(date=DATE, group_id=GROUP_ID)
            assert md_path.exists()

            # Verify DB was updated
            async with aiosqlite.connect(db_with_version) as db:
                await init_database_in_conn(db)
                version = await get_latest_version(db, f"{GROUP_ID}-{DATE}")

            assert version is not None, "Version should exist"
            assert version.content is not None, "Content should no longer be NULL"
            assert len(version.content) > 0, "Content should have Markdown text"
            assert "Test overview for export" in version.content
        finally:
            os.environ.pop("LAYER3_OUTPUT_DIR", None)
            os.environ.pop("SQLITE_DB_PATH", None)
            reset_settings()

    @pytest.mark.asyncio
    async def test_export_returns_path_object(self, tmp_path: Path, l3_json_dir: Path):
        """B3: export_markdown returns a Path object."""
        from z_winnow.config.settings import reset_settings
        from z_winnow.graph.builder import export_markdown

        os.environ["LAYER3_OUTPUT_DIR"] = str(tmp_path / "data" / "processed")
        reset_settings()
        os.environ["SQLITE_DB_PATH"] = str(tmp_path / "test_export.db")

        try:
            md_path = await export_markdown(date=DATE, group_id=GROUP_ID)
            assert isinstance(md_path, Path)
        finally:
            os.environ.pop("LAYER3_OUTPUT_DIR", None)
            os.environ.pop("SQLITE_DB_PATH", None)
            reset_settings()


def test_resource_template_renders_image_file_type_labels() -> None:
    """resource_extraction.j2 的 type_labels 含 image/file —— 防 type_labels 漏加回归。

    若 type_labels 漏了 image/file，模板会 fallback 显示英文 "image"/"file"，
    中文用户看到英文。本测试守住中文标签「图片」「文件」。
    """
    from z_winnow.templates import render_resources

    data: dict[str, Any] = {
        "date": DATE,
        "resources": [
            {
                "time_range": "09:00-10:00",
                "resource_type": "image",
                "resource_title": "架构图",
                "summary": "高密度架构图",
                "content": "Send API 数据流架构图",
                "shared_by": "Alice",
            },
            {
                "time_range": "10:00-11:00",
                "resource_type": "file",
                "resource_title": "纪要",
                "summary": "会议纪要 PDF",
                "content": "会议纪要 PDF",
                "shared_by": "Bob",
            },
        ],
    }
    md = render_resources(data)
    assert "图片" in md
    assert "文件" in md
