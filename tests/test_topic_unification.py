"""test_topic_unification.py — structural validation for unified topics[].

Verifies that the data model, mock data, output composer, and renderer
all correctly handle the unified `topics` list with lifecycle classification.

All tests run in mock mode — no API key required.
"""

from __future__ import annotations

import json

import pytest

from z_winnow.subagents.output_composer.merger import ComposedData
from z_winnow.subagents.unified_reporter.mock import _mock_generate_unified_report
from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

# ── UnifiedReporterOutput model ──────────────────────────────────────────


class TestUnifiedReporterOutput:
    """Verify the Pydantic model accepts unified topics and rejects old fields."""

    def test_accepts_topics_field(self):
        out = UnifiedReporterOutput(
            overview="test overview",
            trend_analysis="trend",
            topics=[
                {
                    "topic_id": "tp_aabbccdd",
                    "topic_name": "Test Topic",
                    "lifecycle": "emerging",
                    "status": "active",
                    "weight": 0.5,
                    "conclusion": "A → B → C",
                    "source_server_ids": ["123"],
                }
            ],
        )
        assert len(out.topics) == 1
        assert out.topics[0].lifecycle == "emerging"

    @pytest.mark.parametrize("old_field", ["topic_sections", "topic_tracking"])
    def test_rejects_old_field(self, old_field: str):
        with pytest.raises((ValueError, TypeError)):
            UnifiedReporterOutput(
                overview="test overview",
                trend_analysis="trend",
                **{old_field: []},
            )

    def test_topics_dict_accepts_any_lifecycle(self):
        """lifecycle is a free-form str on Topic (no Pydantic enum constraint),
        so any string is accepted at the schema level. Lifecycle value
        validation happens at the LLM prompt level, not the Pydantic schema."""
        out = UnifiedReporterOutput(
            overview="test overview",
            trend_analysis="trend",
            topics=[{"lifecycle": "core"}],
        )
        # Topic.lifecycle is str (no enum) — any string accepted
        assert out.topics[0].lifecycle == "core"

    @pytest.mark.parametrize("lifecycle", ["user_defined", "sustained", "emerging"])
    def test_accepts_all_lifecycle_values(self, lifecycle: str):
        out = UnifiedReporterOutput(
            overview="test overview",
            trend_analysis="trend",
            topics=[
                {
                    "topic_id": "tp_00000000",
                    "topic_name": f"Topic-{lifecycle}",
                    "lifecycle": lifecycle,
                    "status": "active",
                    "weight": 0.5,
                    "conclusion": "test",
                    "source_server_ids": ["123"],
                }
            ],
        )
        assert out.topics[0].lifecycle == lifecycle

    def test_trend_summary_default_empty_str(self):
        out = UnifiedReporterOutput(overview="x" * 10, trend_analysis="trend")
        assert out.trend_summary == ""

    def test_model_forbids_extra_fields(self):
        with pytest.raises((ValueError, TypeError)):
            UnifiedReporterOutput(
                overview="test overview",
                trend_analysis="trend",
                unknown_field="should fail",
            )


# ── Mock data ────────────────────────────────────────────────────────────


class TestMockOutput:
    """Verify mock data covers all lifecycle types and required fields."""

    @pytest.fixture()
    def mock_output(self) -> UnifiedReporterOutput:
        return _mock_generate_unified_report(
            [{"serverId": f"msg_{i:03d}"} for i in range(10)],
            "20260523",
            "测试群",
        )

    def test_has_topics_not_old_fields(self, mock_output: UnifiedReporterOutput):
        assert hasattr(mock_output, "topics")
        assert not hasattr(mock_output, "topic_sections")
        assert not hasattr(mock_output, "topic_tracking")

    def test_topics_cover_all_lifecycle_types(self, mock_output: UnifiedReporterOutput):
        lifecycles = {t.lifecycle for t in mock_output.topics}
        assert lifecycles == {"user_defined", "sustained", "emerging"}

    def test_each_topic_has_required_fields(self, mock_output: UnifiedReporterOutput):
        required_keys = {
            "topic_id",
            "topic_name",
            "lifecycle",
            "status",
            "weight",
            "conclusion",
            "source_server_ids",
        }
        for t in mock_output.topics:
            missing = required_keys - set(type(t).model_fields.keys())
            assert not missing, f"Topic '{t.topic_name or '?'}' missing: {missing}"

    def test_each_topic_has_new_fields(self, mock_output: UnifiedReporterOutput):
        new_keys = {"background", "process", "conclusion", "description", "trend", "participants"}
        for t in mock_output.topics:
            for key in new_keys:
                assert hasattr(t, key), f"Topic '{t.topic_name or '?'}' missing: {key}"

    def test_participants_are_nicknames(self, mock_output: UnifiedReporterOutput):
        for t in mock_output.topics:
            participants = t.participants
            for p in participants:
                # Nicknames should not look like wxid
                assert not p.startswith("wxid_"), f"Participant looks like wxid: {p}"
                assert len(p) >= 2, f"Participant name too short: {p}"


# ── ComposedData (output composer merger) ────────────────────────────────


class TestComposedData:
    """Verify ComposedData uses topics and not old fields."""

    def test_has_topics_no_old_fields(self):
        cd = ComposedData(date="20260523")
        assert hasattr(cd, "topics")
        assert not hasattr(cd, "topic_sections")
        assert not hasattr(cd, "new_topics")
        assert not hasattr(cd, "updated_topics")

    def test_topics_default_empty(self):
        cd = ComposedData(date="20260523")
        assert cd.topics == []
        assert cd.trend_summary == ""

    def test_accepts_unified_topics(self):
        topics = [
            {"topic_name": "A", "lifecycle": "user_defined"},
            {"topic_name": "B", "lifecycle": "sustained"},
            {"topic_name": "C", "lifecycle": "emerging"},
        ]
        cd = ComposedData(date="20260523", topics=topics, trend_summary="summary")
        assert len(cd.topics) == 3
        assert cd.trend_summary == "summary"


# ── Prompt content ───────────────────────────────────────────────────────


class TestPromptContent:
    """Verify SYSTEM_PROMPT references unified topics and new lifecycle values."""

    @pytest.fixture()
    def system_prompt(self) -> str:
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        return SYSTEM_PROMPT

    def test_mentions_three_lifecycle_values(self, system_prompt: str):
        assert "user_defined" in system_prompt
        assert "sustained" in system_prompt
        assert "emerging" in system_prompt

    def test_mentions_unified_topics(self, system_prompt: str):
        assert "topics" in system_prompt

    def test_does_not_mention_old_fields(self, system_prompt: str):
        # These should NOT appear as output field names
        assert '"topic_sections"' not in system_prompt
        assert '"topic_tracking"' not in system_prompt


# ── JSON Schema validation ──────────────────────────────────────────────


class TestJsonSchema:
    """Verify topics.json schema has lifecycle and new fields."""

    def test_topics_schema_has_lifecycle_enum(self):
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "schemas" / "topics.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        topic_item = schema["$defs"]["TopicItem"]
        lifecycle = topic_item["properties"]["lifecycle"]
        assert set(lifecycle["enum"]) == {"user_defined", "sustained", "emerging"}

    def test_topics_schema_has_new_fields(self):
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "schemas" / "topics.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        props = schema["$defs"]["TopicItem"]["properties"]
        for field in (
            "background",
            "process",
            "conclusion",
            "description",
            "trend",
            "participants",
        ):
            assert field in props, f"Schema missing field: {field}"

    def test_daily_report_schema_has_unified_topics(self):
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "schemas" / "daily_report_v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        props = schema["properties"]
        assert "topics" in props
        # Should NOT have old fields
        assert "topic_sections" not in props
        assert "topic_tracking" not in props
        # TopicItem 应含因果链三段（background/process/conclusion）
        topic_props = schema["$defs"]["TopicItem"]["properties"]
        for field in ("background", "process", "conclusion"):
            assert field in topic_props, f"daily_report TopicItem missing: {field}"
