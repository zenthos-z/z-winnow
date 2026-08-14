"""Tests for T-W12-3: compose_json + render_markdown in output_composer.

Phase E (compose_json): unified_reporter output -> 4 L3 JSON files.
Phase H (render_markdown): L3 JSON files -> Jinja2 Markdown report.

T-W13: Updated for unified topics[] system — no topic_reports parameter,
topics use lifecycle classification (user_defined, sustained, emerging).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from z_winnow.subagents.output_composer import compose_json, render_markdown

# ============================================================
# Fixtures
# ============================================================

SAMPLE_UNIFIED_REPORT: dict[str, Any] = {
    "overview": "Today was a productive day with several interesting discussions.",
    "important_notice": "Server maintenance scheduled for tonight.",
    "topics": [
        {
            "topic_id": "T001",
            "topic_name": "LangGraph Architecture",
            "lifecycle": "sustained",
            "status": "active",
            "weight": 0.8,
            "conclusion": "Decided on modular StateGraph approach",
            "description": "Discussion about LangGraph node architecture",
            "trend": "increasing",
            "participants": ["Alice", "Bob"],
            "source_server_ids": ["msg_001", "msg_002"],
            "first_seen": "20260520T10:00:00",
            "last_seen": "20260520T14:30:00",
        },
        {
            "topic_id": "T002",
            "topic_name": "RAG Pipeline Optimization",
            "lifecycle": "emerging",
            "status": "active",
            "weight": 0.6,
            "conclusion": "Exploring hybrid retrieval strategies",
            "description": "New discussion on RAG pipeline improvements",
            "trend": "stable",
            "participants": ["Charlie"],
            "source_server_ids": ["msg_003"],
            "first_seen": "20260520T11:00:00",
            "last_seen": "20260520T11:30:00",
        },
    ],
    "trend_summary": "Architecture discussions trending up, RAG pipeline is emerging",
    "trend_analysis": {
        "current_phase": "peak discussion",
        "pending_issues": "None",
    },
    "highlights": [
        "Great insight on vector search optimization",
        "Interesting approach to chunking strategies",
    ],
    "resources": [
        {
            "time_range": "10:00-12:00",
            "resource_type": "article",
            "summary": "LangGraph best practices guide",
            "content": "https://example.com/langgraph-guide",
        },
        {
            "time_range": "14:00-15:00",
            "resource_type": "tool",
            "summary": "Prompt engineering toolkit",
            "content": "https://example.com/prompt-toolkit",
        },
    ],
    "resource_count_by_type": {"article": 1, "tool": 1},
    "custom_tables": {
        "engineering": {
            "issues": [
                {
                    "datetime": "20260520T10:30:00",
                    "group": "dev-team",
                    "description": "Database connection pool exhaustion",
                    "solution": "Increased pool size from 10 to 50",
                    "status": "resolved",
                    "source_members": "Alice, Bob",
                },
            ],
            "group_summary": {"dev-team": "Discussed database and caching improvements"},
        }
    },
    "model_used": "test-model-v1",
}


DATE = "20260520"


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Create a temporary output directory for L3 JSON files."""
    d = tmp_path / "processed" / DATE
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# B2: compose_json produces 4 JSON files
# ============================================================


class TestComposeJson:
    """Tests for Phase E: compose_json."""

    @pytest.mark.asyncio
    async def test_compose_json_produces_4_files(self, output_dir: Path):
        """B2: compose_json generates 4 JSON files."""
        result = await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        assert len(result) == 4
        assert set(result.keys()) == {"daily", "resources", "engineering", "topics"}

        # All paths should exist
        for name, path in result.items():
            assert path.exists(), f"{name}.json was not written"
            assert path.name == f"{name}.json"

    @pytest.mark.asyncio
    async def test_compose_json_daily_content(self, output_dir: Path):
        """R1: daily.json has overview + topics + highlights + trend_summary."""
        result = await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        daily = json.loads(result["daily"].read_text(encoding="utf-8"))
        assert daily["date"] == DATE
        assert daily["overview"] == SAMPLE_UNIFIED_REPORT["overview"]
        assert daily["important_notice"] == SAMPLE_UNIFIED_REPORT["important_notice"]
        assert len(daily["topics"]) == 2
        assert daily["topics"][0]["topic_name"] == "LangGraph Architecture"
        assert daily["topics"][0]["lifecycle"] == "sustained"
        assert len(daily["highlights"]) == 2
        assert daily["trend_analysis"] == SAMPLE_UNIFIED_REPORT["trend_analysis"]
        assert daily["trend_summary"] == SAMPLE_UNIFIED_REPORT["trend_summary"]

    @pytest.mark.asyncio
    async def test_compose_json_resources_content(self, output_dir: Path):
        """R1: resources.json has resource list with correct counts."""
        result = await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        resources = json.loads(result["resources"].read_text(encoding="utf-8"))
        assert resources["date"] == DATE
        assert len(resources["resources"]) == 2
        assert resources["total_count"] == 2
        assert resources["count_by_type"]["article"] == 1
        assert resources["count_by_type"]["tool"] == 1

    @pytest.mark.asyncio
    async def test_compose_json_engineering_content(self, output_dir: Path):
        """R1: engineering.json has engineering_issues and group_summary."""
        result = await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        engineering = json.loads(result["engineering"].read_text(encoding="utf-8"))
        assert engineering["date"] == DATE
        assert len(engineering["issues"]) == 1
        assert engineering["issues"][0]["description"] == "Database connection pool exhaustion"
        assert (
            engineering["group_summary"]["dev-team"]
            == "Discussed database and caching improvements"
        )
        assert engineering["model_used"] == "test-model-v1"

    @pytest.mark.asyncio
    async def test_compose_json_topics_content(self, output_dir: Path):
        """R1: topics.json has unified topic data with lifecycle classification."""
        result = await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        topics = json.loads(result["topics"].read_text(encoding="utf-8"))
        assert topics["date"] == DATE
        assert len(topics["topics"]) == 2
        assert topics["total_count"] == 2
        assert topics["total_active"] == 2
        assert topics["topics"][0]["topic_name"] == "LangGraph Architecture"
        assert topics["topics"][0]["lifecycle"] == "sustained"
        assert topics["lifecycle_counts"]["sustained"] == 1
        assert topics["lifecycle_counts"]["emerging"] == 1
        assert topics["trend_summary"] == SAMPLE_UNIFIED_REPORT["trend_summary"]

    @pytest.mark.asyncio
    async def test_compose_json_topics_placeholder_when_empty(self, output_dir: Path):
        """topics.json has empty topics list when unified report has no topics."""
        report_no_topics = {**SAMPLE_UNIFIED_REPORT, "topics": [], "trend_summary": ""}
        result = await compose_json(
            unified_report_output=report_no_topics,
            output_dir=output_dir,
            date=DATE,
        )

        topics = json.loads(result["topics"].read_text(encoding="utf-8"))
        assert topics["date"] == DATE
        assert topics["topics"] == []
        assert topics["total_count"] == 0
        assert topics["total_active"] == 0
        assert topics["lifecycle_counts"] == {}

    @pytest.mark.asyncio
    async def test_compose_json_none_input(self, output_dir: Path):
        """compose_json handles None input gracefully -- writes placeholders."""
        result = await compose_json(
            unified_report_output=None,
            output_dir=output_dir,
            date=DATE,
        )

        assert len(result) == 4
        for _name, path in result.items():
            assert path.exists()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data.get("placeholder") is True or data.get("date") == DATE

    @pytest.mark.asyncio
    async def test_compose_json_empty_unified(self, output_dir: Path):
        """compose_json handles empty unified_report dict."""
        result = await compose_json(
            unified_report_output={},
            output_dir=output_dir,
            date=DATE,
        )

        assert len(result) == 4
        daily = json.loads(result["daily"].read_text(encoding="utf-8"))
        assert daily["overview"] == ""
        assert daily["topics"] == []

    @pytest.mark.asyncio
    async def test_compose_json_returns_path_objects(self, output_dir: Path):
        """compose_json returns Path objects, not strings."""
        result = await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        for path in result.values():
            assert isinstance(path, Path)

    @pytest.mark.asyncio
    async def test_compose_json_topic_lifecycle_counts(self, output_dir: Path):
        """topics.json correctly counts topics by lifecycle category."""
        report_with_mixed = {
            **SAMPLE_UNIFIED_REPORT,
            "topics": [
                {
                    "topic_id": "T001",
                    "topic_name": "Topic A",
                    "lifecycle": "user_defined",
                    "status": "active",
                    "weight": 0.9,
                    "conclusion": "",
                    "description": "",
                    "trend": "increasing",
                    "participants": [],
                    "source_server_ids": [],
                    "first_seen": "20260520T10:00:00",
                    "last_seen": "20260520T10:00:00",
                },
                {
                    "topic_id": "T002",
                    "topic_name": "Topic B",
                    "lifecycle": "sustained",
                    "status": "active",
                    "weight": 0.7,
                    "conclusion": "",
                    "description": "",
                    "trend": "stable",
                    "participants": [],
                    "source_server_ids": [],
                    "first_seen": "20260520T10:00:00",
                    "last_seen": "20260520T10:00:00",
                },
                {
                    "topic_id": "T003",
                    "topic_name": "Topic C",
                    "lifecycle": "emerging",
                    "status": "discussion",
                    "weight": 0.3,
                    "conclusion": "",
                    "description": "",
                    "trend": "stable",
                    "participants": [],
                    "source_server_ids": [],
                    "first_seen": "20260520T10:00:00",
                    "last_seen": "20260520T10:00:00",
                },
            ],
        }
        result = await compose_json(
            unified_report_output=report_with_mixed,
            output_dir=output_dir,
            date=DATE,
        )

        topics = json.loads(result["topics"].read_text(encoding="utf-8"))
        assert topics["lifecycle_counts"]["user_defined"] == 1
        assert topics["lifecycle_counts"]["sustained"] == 1
        assert topics["lifecycle_counts"]["emerging"] == 1
        assert topics["total_count"] == 3


# ============================================================
# Phase H: render_markdown
# ============================================================


class TestRenderMarkdown:
    """Tests for Phase H: render_markdown."""

    @pytest.mark.asyncio
    async def test_render_markdown_from_json(self, output_dir: Path):
        """render_markdown reads L3 JSON and produces a Markdown file."""
        # First: write the JSON files
        await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        # Then: render Markdown
        md_path = render_markdown(json_dir=output_dir)

        assert md_path.exists()
        assert md_path.name == "report.md"

        content = md_path.read_text(encoding="utf-8")
        assert len(content) > 0
        # Should contain overview text
        assert "productive" in content

    @pytest.mark.asyncio
    async def test_render_markdown_output_is_valid_text(self, output_dir: Path):
        """render_markdown produces valid UTF-8 text."""
        await compose_json(
            unified_report_output=SAMPLE_UNIFIED_REPORT,
            output_dir=output_dir,
            date=DATE,
        )

        md_path = render_markdown(json_dir=output_dir)

        content = md_path.read_text(encoding="utf-8")
        # Should be valid UTF-8 without errors
        assert isinstance(content, str)
        assert len(content) > 0
