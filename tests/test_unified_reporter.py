"""Tests for unified_reporter — Topic Unification: unified topics[] with lifecycle.

Verifies:
  - UnifiedReporterOutput model has unified `topics` field (not topic_sections/topic_tracking)
  - UnifiedReporterOutput model includes trend_summary field
  - Mock output includes topics with all three lifecycle types
  - prompt.py exposes SYSTEM_PROMPT with three tasks (not four)
  - parse_json_output handles unified topics field
  - build_user_prompt includes topics reference and lifecycle instructions
"""

from __future__ import annotations

import json

# ============================================================
# Model verification
# ============================================================


class TestUnifiedReporterOutputModel:
    """Verify UnifiedReporterOutput has unified topics field."""

    def test_model_has_topics_field(self):
        """UnifiedReporterOutput has topics field."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        fields = UnifiedReporterOutput.model_fields
        assert "topics" in fields, "topics field missing from UnifiedReporterOutput"

    def test_model_has_trend_summary_field(self):
        """UnifiedReporterOutput has trend_summary field."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        fields = UnifiedReporterOutput.model_fields
        assert "trend_summary" in fields, "trend_summary field missing from UnifiedReporterOutput"

    def test_model_topics_default_empty_list(self):
        """topics defaults to empty list."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        obj = UnifiedReporterOutput(
            overview="test",
            trend_analysis="test",
        )
        assert obj.topics == []

    def test_model_trend_summary_default_empty_str(self):
        """trend_summary defaults to empty string."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        obj = UnifiedReporterOutput(
            overview="test",
            trend_analysis="test",
        )
        assert obj.trend_summary == ""

    def test_model_does_not_have_topic_sections(self):
        """UnifiedReporterOutput does NOT have topic_sections (removed in unification)."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        fields = UnifiedReporterOutput.model_fields
        assert "topic_sections" not in fields, (
            "topic_sections should not exist — replaced by unified topics"
        )

    def test_model_does_not_have_topic_tracking(self):
        """UnifiedReporterOutput does NOT have topic_tracking (removed in unification)."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        fields = UnifiedReporterOutput.model_fields
        assert "topic_tracking" not in fields, (
            "topic_tracking should not exist — replaced by unified topics"
        )

    def test_model_accepts_full_output_with_unified_topics(self):
        """UnifiedReporterOutput accepts complete output with unified topics[]."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        data = {
            "overview": "Test overview",
            "important_notice": "",  # W16-A1: str default '' (was None; schema rejects None)
            "topics": [
                {
                    "topic_id": "tp_a1b2c3d4",
                    "topic_name": "Test Topic",
                    "lifecycle": "user_defined",
                    "status": "active",
                    "weight": 0.85,
                    "conclusion": "Background: testing. Discussion: verified. Conclusion: works.",
                    "description": "Test topic description with inclusion boundary.",
                    "trend": "This topic has been actively discussed since early January.",
                    "participants": ["Alice", "Bob"],
                    "source_server_ids": ["msg_001"],
                    "first_seen": "2025-01-15",
                    "last_seen": "2025-01-20",
                },
                {
                    "topic_id": "tp_c3d4e5f6",
                    "topic_name": "Sustained Topic",
                    "lifecycle": "sustained",
                    "status": "discussion",
                    "weight": 0.7,
                    "conclusion": "Background: ongoing. Discussion: continued. Conclusion: progressing.",
                    "description": "Sustained topic with inclusion boundary.",
                    "trend": "This topic appeared last week and continues today.",
                    "participants": ["Charlie"],
                    "source_server_ids": ["msg_002"],
                    "first_seen": "2025-01-10",
                    "last_seen": "2025-01-20",
                },
                {
                    "topic_id": "tp_e5f6g7h8",
                    "topic_name": "Emerging Topic",
                    "lifecycle": "emerging",
                    "status": "discussion",
                    "weight": 0.5,
                    "conclusion": "Background: new. Discussion: introduced. Conclusion: needs exploration.",
                    "description": "First appearance of this topic.",
                    "trend": "Today is the first time this topic appeared.",
                    "participants": ["Dave"],
                    "source_server_ids": ["msg_003"],
                    "first_seen": "2025-01-20",
                    "last_seen": "2025-01-20",
                },
            ],
            "trend_analysis": "Trend analysis text",
            "trend_summary": "Test trend summary",
            "highlights": ["Test highlight"],
            "resources": [],
            "resource_count_by_type": {},
            "custom_tables": {},
            "model_used": "test",
        }
        obj = UnifiedReporterOutput.model_validate(data)
        assert len(obj.topics) == 3
        assert obj.trend_summary == "Test trend summary"
        assert obj.topics[0].lifecycle == "user_defined"
        assert obj.topics[1].lifecycle == "sustained"
        assert obj.topics[2].lifecycle == "emerging"

    def test_model_rejects_topic_sections(self):
        """UnifiedReporterOutput rejects topic_sections (extra='forbid')."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        data = {
            "overview": "Test",
            "trend_analysis": "Test",
            "topic_sections": [{"topic_name": "T"}],
        }
        try:
            UnifiedReporterOutput.model_validate(data)
            raise AssertionError(
                "Should have raised validation error for extra field topic_sections"
            )
        except Exception as e:
            assert "topic_sections" in str(e) or "Extra inputs" in str(e)

    def test_model_rejects_topic_tracking(self):
        """UnifiedReporterOutput rejects topic_tracking (extra='forbid')."""
        from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

        data = {
            "overview": "Test",
            "trend_analysis": "Test",
            "topic_tracking": [{"topic_id": "tp_12345678"}],
        }
        try:
            UnifiedReporterOutput.model_validate(data)
            raise AssertionError(
                "Should have raised validation error for extra field topic_tracking"
            )
        except Exception as e:
            assert "topic_tracking" in str(e) or "Extra inputs" in str(e)


# ============================================================
# Mock verification
# ============================================================


class TestMockOutput:
    """Verify mock output includes unified topics with lifecycle."""

    def test_mock_has_topics(self):
        """Mock output includes topics data."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        assert len(result.topics) > 0, "Mock topics is empty"

    def test_mock_has_trend_summary(self):
        """Mock output includes trend_summary."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        assert result.trend_summary, "Mock trend_summary is empty"

    def test_mock_topics_have_lifecycle(self):
        """Mock topics include lifecycle field with valid values."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        valid_lifecycles = {"user_defined", "sustained", "emerging"}
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        for topic in result.topics:
            assert hasattr(topic, "lifecycle"), f"topic missing 'lifecycle': {topic}"
            assert topic.lifecycle in valid_lifecycles, (
                f"topic has invalid lifecycle '{topic.lifecycle}': {topic}"
            )

    def test_mock_topics_have_required_fields(self):
        """Mock topics items have all required fields."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        required_fields = {
            "topic_id",
            "topic_name",
            "lifecycle",
            "status",
            "weight",
            "background",
            "process",
            "conclusion",
            "description",
            "trend",
            "participants",
            "source_server_ids",
            "first_seen",
            "last_seen",
        }
        for topic in result.topics:
            for field in required_fields:
                assert hasattr(topic, field), f"topic missing '{field}': {topic}"

    def test_mock_topics_include_all_lifecycle_types(self):
        """Mock topics cover all three lifecycle types."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        lifecycles = {t.lifecycle for t in result.topics}
        assert "user_defined" in lifecycles, "Mock missing user_defined topic"
        assert "sustained" in lifecycles, "Mock missing sustained topic"
        assert "emerging" in lifecycles, "Mock missing emerging topic"

    def test_mock_does_not_have_topic_sections(self):
        """Mock output does not have topic_sections (removed)."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        assert not hasattr(result, "topic_sections"), "Mock output should not have topic_sections"

    def test_mock_does_not_have_topic_tracking(self):
        """Mock output does not have topic_tracking (removed)."""
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        assert not hasattr(result, "topic_tracking"), "Mock output should not have topic_tracking"


# ============================================================
# Prompt verification
# ============================================================


class TestPromptContent:
    """Verify prompt content uses three tasks and unified topics."""

    def test_system_prompt_has_three_tasks(self):
        """SYSTEM_PROMPT describes three tasks (not four)."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert "三个" in SYSTEM_PROMPT or "three" in SYSTEM_PROMPT.lower()

    def test_system_prompt_does_not_have_four_tasks(self):
        """SYSTEM_PROMPT does NOT reference four tasks."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert "四个任务" not in SYSTEM_PROMPT
        assert "任务 4" not in SYSTEM_PROMPT
        assert "Task 4" not in SYSTEM_PROMPT

    def test_user_prompt_template_references_topics(self):
        """USER_PROMPT_TEMPLATE references topics (not topic_sections or topic_tracking)."""
        from z_winnow.subagents.unified_reporter.prompt import USER_PROMPT_TEMPLATE

        assert "topics" in USER_PROMPT_TEMPLATE

    def test_user_prompt_template_does_not_reference_four_tasks(self):
        """USER_PROMPT_TEMPLATE does NOT reference four tasks."""
        from z_winnow.subagents.unified_reporter.prompt import USER_PROMPT_TEMPLATE

        assert "四个任务" not in USER_PROMPT_TEMPLATE
        assert "四个" not in USER_PROMPT_TEMPLATE

    def test_system_prompt_describes_lifecycle_rules(self):
        """SYSTEM_PROMPT describes lifecycle classification rules."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert "user_defined" in SYSTEM_PROMPT
        assert "sustained" in SYSTEM_PROMPT
        assert "emerging" in SYSTEM_PROMPT

    def test_system_prompt_does_not_use_old_lifecycle_values(self):
        """SYSTEM_PROMPT does NOT use old lifecycle values (core, continuous, new)."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        # The old values should not appear as standalone lifecycle terms.
        # Note: "new" as a word appears in normal text, so we check for the
        # specific lifecycle context patterns.
        assert '"core"' not in SYSTEM_PROMPT
        assert '"continuous"' not in SYSTEM_PROMPT

    def test_system_prompt_output_format_includes_topics(self):
        """SYSTEM_PROMPT output format includes topics field."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert '"topics"' in SYSTEM_PROMPT

    def test_system_prompt_output_format_does_not_include_topic_tracking(self):
        """SYSTEM_PROMPT output format does NOT include topic_tracking field."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert '"topic_tracking"' not in SYSTEM_PROMPT

    def test_system_prompt_output_format_does_not_include_topic_sections(self):
        """SYSTEM_PROMPT output format does NOT include topic_sections field."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert '"topic_sections"' not in SYSTEM_PROMPT

    def test_system_prompt_output_format_includes_trend_summary(self):
        """SYSTEM_PROMPT output format includes trend_summary field."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert '"trend_summary"' in SYSTEM_PROMPT

    def test_system_prompt_mentions_unified_topics_instruction(self):
        """SYSTEM_PROMPT explicitly says to use topics (not topic_sections/topic_tracking)."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert "不要输出 topic_sections 或 topic_tracking" in SYSTEM_PROMPT
        assert "统一使用 topics" in SYSTEM_PROMPT


# ============================================================
# Parse verification
# ============================================================


class TestParseJsonOutput:
    """Verify parse_json_output handles unified topics field."""

    def test_parse_json_with_unified_topics(self):
        """parse_json_output correctly parses output with unified topics[]."""
        from z_winnow.subagents.unified_reporter.agent import parse_json_output

        data = json.dumps(
            {
                "overview": "Test",
                "trend_analysis": "Test trend",
                "topics": [
                    {
                        "topic_id": "tp_12345678",
                        "topic_name": "Unified Topic",
                        "lifecycle": "user_defined",
                        "status": "active",
                        "weight": 0.8,
                        "conclusion": "Background: test. Discussion: test. Conclusion: test.",
                        "description": "A unified topic",
                        "trend": "This topic started today.",
                        "participants": ["Alice"],
                        "source_server_ids": ["msg_001"],
                        "first_seen": "2025-01-20",
                        "last_seen": "2025-01-20",
                    },
                    {
                        "topic_id": "tp_aabbccdd",
                        "topic_name": "Sustained Topic",
                        "lifecycle": "sustained",
                        "status": "discussion",
                        "weight": 0.6,
                        "conclusion": "Background: ongoing. Discussion: continued. Conclusion: progressing.",
                        "description": "A sustained topic",
                        "trend": "Continuing from previous days.",
                        "participants": ["Bob"],
                        "source_server_ids": ["msg_002"],
                        "first_seen": "2025-01-10",
                        "last_seen": "2025-01-20",
                    },
                ],
                "trend_summary": "Summary",
                "resources": [],
                "resource_count_by_type": {},
                "custom_tables": {},
            }
        )
        result = parse_json_output(data)
        assert len(result.topics) == 2
        assert result.trend_summary == "Summary"
        assert result.topics[0].lifecycle == "user_defined"
        assert result.topics[1].lifecycle == "sustained"

    def test_parse_json_without_topics_defaults_to_empty(self):
        """parse_json_output handles output without topics (backward compat)."""
        from z_winnow.subagents.unified_reporter.agent import parse_json_output

        data = json.dumps(
            {
                "overview": "Test",
                "trend_analysis": "Test trend",
                "resources": [],
                "resource_count_by_type": {},
                "custom_tables": {},
            }
        )
        result = parse_json_output(data)
        assert result.topics == []
        assert result.trend_summary == ""

    def test_parse_json_rejects_topic_sections(self):
        """parse_json_output rejects topic_sections (extra='forbid')."""
        from z_winnow.subagents.unified_reporter.agent import (
            OutputParseError,
            parse_json_output,
        )

        data = json.dumps(
            {
                "overview": "Test",
                "trend_analysis": "Test trend",
                "topic_sections": [{"topic_name": "T", "lifecycle": "emerging"}],
                "resources": [],
                "resource_count_by_type": {},
                "custom_tables": {},
            }
        )
        try:
            parse_json_output(data)
            raise AssertionError("Should have raised OutputParseError for topic_sections")
        except OutputParseError:
            pass  # Expected

    def test_parse_json_rejects_topic_tracking(self):
        """parse_json_output rejects topic_tracking (extra='forbid')."""
        from z_winnow.subagents.unified_reporter.agent import (
            OutputParseError,
            parse_json_output,
        )

        data = json.dumps(
            {
                "overview": "Test",
                "trend_analysis": "Test trend",
                "topic_tracking": [{"topic_id": "tp_12345678", "topic_name": "T"}],
                "resources": [],
                "resource_count_by_type": {},
                "custom_tables": {},
            }
        )
        try:
            parse_json_output(data)
            raise AssertionError("Should have raised OutputParseError for topic_tracking")
        except OutputParseError:
            pass  # Expected

    def test_parse_json_with_all_lifecycle_types(self):
        """parse_json_output handles topics with all three lifecycle types."""
        from z_winnow.subagents.unified_reporter.agent import parse_json_output

        data = json.dumps(
            {
                "overview": "Test",
                "trend_analysis": "Test",
                "topics": [
                    {
                        "topic_id": "tp_11111111",
                        "topic_name": "User Defined",
                        "lifecycle": "user_defined",
                        "status": "active",
                        "weight": 0.9,
                        "conclusion": "Background. Discussion. Conclusion.",
                        "description": "desc",
                        "trend": "trend text",
                        "participants": [],
                        "source_server_ids": ["m1"],
                        "first_seen": "2025-01-01",
                        "last_seen": "2025-01-20",
                    },
                    {
                        "topic_id": "tp_22222222",
                        "topic_name": "Sustained",
                        "lifecycle": "sustained",
                        "status": "discussion",
                        "weight": 0.6,
                        "conclusion": "Background. Discussion. Conclusion.",
                        "description": "desc",
                        "trend": "trend text",
                        "participants": [],
                        "source_server_ids": ["m2"],
                        "first_seen": "2025-01-10",
                        "last_seen": "2025-01-20",
                    },
                    {
                        "topic_id": "tp_33333333",
                        "topic_name": "Emerging",
                        "lifecycle": "emerging",
                        "status": "discussion",
                        "weight": 0.3,
                        "conclusion": "Background. Discussion. Conclusion.",
                        "description": "desc",
                        "trend": "trend text",
                        "participants": [],
                        "source_server_ids": ["m3"],
                        "first_seen": "2025-01-20",
                        "last_seen": "2025-01-20",
                    },
                ],
                "resources": [],
                "resource_count_by_type": {},
                "custom_tables": {},
            }
        )
        result = parse_json_output(data)
        assert len(result.topics) == 3
        lifecycles = [t.lifecycle for t in result.topics]
        assert lifecycles == ["user_defined", "sustained", "emerging"]
