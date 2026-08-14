"""Tests for source_server_ids validation — T-W12-12 (L4-3).

Verifies:
  B1: unified_reporter prompt template explicitly requires source_server_ids
      for topics, resources, engineering_issues (code_inspection).
  B2: parse-layer warns/rejects empty source_server_ids arrays (code_inspection).
  B3: Structural assertions for source_server_ids (P039 layered assertions).

Uses mock data — real LLM call verification (R1) is the evaluator's responsibility.
"""

from __future__ import annotations

import json
import logging

from z_winnow.subagents.unified_reporter.agent import (
    parse_json_output,
    validate_source_server_ids,
)
from z_winnow.subagents.unified_reporter.mock import (
    _mock_generate_unified_report,
)
from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput


def _ids_of(record: object) -> list:
    """Read source_server_ids from a Pydantic model OR a plain dict record."""
    if isinstance(record, dict):
        ids = record.get("source_server_ids")
        return ids if isinstance(ids, list) else []
    return getattr(record, "source_server_ids", [])


def _all_records(result: UnifiedReporterOutput) -> list:
    """Flatten all source_server_ids-bearing records (typed + custom_tables slot)."""
    records: list = list(result.topics) + list(result.resources)
    ct = result.custom_tables if isinstance(result.custom_tables, dict) else {}
    for table_data in ct.values():
        if isinstance(table_data, dict):
            items = table_data.get("items") or table_data.get("issues") or []
            if isinstance(items, list):
                records.extend(items)
    return records

# ============================================================
# B1: Prompt template source_server_ids instructions
# ============================================================


class TestPromptSourceServerIds:
    """B1: Verify prompt template explicitly requires source_server_ids."""

    def test_system_prompt_has_source_server_ids_for_topics(self):
        """SYSTEM_PROMPT mentions source_server_ids in topics."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        # topics section must mention source_server_ids
        assert "source_server_ids" in SYSTEM_PROMPT
        # Must be described as mandatory/required for topics
        assert "source_server_ids" in SYSTEM_PROMPT, "source_server_ids not found in SYSTEM_PROMPT"

    def test_system_prompt_has_source_server_ids_for_resources(self):
        """SYSTEM_PROMPT mentions source_server_ids in resources section."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        # Find the resources section (task 2) and verify source_server_ids
        task2_start = SYSTEM_PROMPT.find("任务 2")
        assert task2_start > 0, "Task 2 section not found in SYSTEM_PROMPT"

        # Look for source_server_ids in the resources section
        # (between task 2 and task 3)
        task3_start = SYSTEM_PROMPT.find("任务 3", task2_start)
        task2_section = SYSTEM_PROMPT[task2_start:task3_start]
        assert "source_server_ids" in task2_section, (
            "source_server_ids not found in resources section (task 2)"
        )

    def test_system_prompt_has_source_server_ids_for_engineering_issues(self):
        """The engineering skill prompt (registry-injected when enabled) requires
        source_server_ids. Task 3 is no longer hardcoded in SYSTEM_PROMPT — it is
        dynamically injected from the YAML skill via build_system_prompt."""
        from z_winnow.subagents.unified_reporter.prompt import build_system_prompt

        prompt = build_system_prompt(custom_tables={"engineering": {"enabled": True}})
        # The injected engineering skill fragment must require source_server_ids
        assert "source_server_ids" in prompt, (
            "source_server_ids not found in injected engineering skill prompt"
        )

    def test_system_prompt_mandatory_source_server_ids(self):
        """SYSTEM_PROMPT declares source_server_ids as mandatory (必填)."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        # Must have explicit mandatory language
        assert "必填" in SYSTEM_PROMPT or "必须非空" in SYSTEM_PROMPT, (
            "source_server_ids mandatory instruction (必填/必须非空) not found"
        )

    def test_system_prompt_server_id_actual_constraint(self):
        """SYSTEM_PROMPT requires serverId from actual messages (L005)."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert "实际" in SYSTEM_PROMPT or "禁止编造" in SYSTEM_PROMPT, (
            "L005 constraint (serverId must come from actual messages) not found"
        )

    def test_system_prompt_references_user_message_server_id_format(self):
        """SYSTEM_PROMPT references server ID format (XML or markdown svrid).

        T-W12-12 fix: prompt must reference the actual format used in chat context,
        now markdown svrid:{id} format instead of XML <user_message server_id="...">.
        """
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        assert '<user_message server_id="' in SYSTEM_PROMPT or "svrid:" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must reference server ID format that matches "
            "actual chat context (XML or markdown svrid)"
        )

    def test_output_format_includes_source_server_ids_in_resources(self):
        """Output format example includes source_server_ids for resources."""
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        # Find the JSON example with resources
        assert '"resources"' in SYSTEM_PROMPT
        # Check that resources example has source_server_ids
        resources_idx = SYSTEM_PROMPT.find('"resources"')
        # The resources JSON example should include source_server_ids
        snippet = SYSTEM_PROMPT[resources_idx : resources_idx + 500]
        assert "source_server_ids" in snippet, (
            "resources example in output format missing source_server_ids"
        )

    def test_output_format_includes_source_server_ids_in_engineering(self):
        """The engineering example in the output format includes source_server_ids.

        Engineering now lives under the custom_tables slot; its example record
        (inside custom_tables.engineering.issues) must carry source_server_ids.
        """
        from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

        # custom_tables slot example must be present
        assert '"custom_tables"' in SYSTEM_PROMPT
        ct_idx = SYSTEM_PROMPT.find('"custom_tables"')
        snippet = SYSTEM_PROMPT[ct_idx : ct_idx + 800]
        assert "source_server_ids" in snippet, (
            "custom_tables engineering example missing source_server_ids"
        )

    def test_user_prompt_includes_source_server_ids_instruction(self):
        """USER_PROMPT_TEMPLATE includes source_server_ids instruction."""
        from z_winnow.subagents.unified_reporter.prompt import (
            USER_PROMPT_TEMPLATE,
        )

        assert "source_server_ids" in USER_PROMPT_TEMPLATE, (
            "USER_PROMPT_TEMPLATE missing source_server_ids instruction"
        )


# ============================================================
# B2: Parse-layer source_server_ids validation
# ============================================================


class TestValidateSourceServerIds:
    """B2: Verify parse-layer warns on empty source_server_ids.

    W16-A1: topics/resources/engineering_issues are now strongly-typed models
    where source_server_ids is a list[str] with default_factory=list. The old
    "missing" (ids is None) and "not a list" branches are permanently false
    and were removed from validate_source_server_ids — their tests are removed
    here. Only the genuinely possible "empty list" failure mode is tested.
    """

    def test_validate_passes_on_valid_output(self):
        """validate_source_server_ids returns empty warnings for valid mock output."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        warnings = validate_source_server_ids(result)
        assert warnings == [], f"Unexpected warnings: {warnings}"

    def test_validate_warns_on_empty_topics_ids(self):
        """validate_source_server_ids warns when topics has empty ids."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        # Manually set empty source_server_ids
        result.topics[0].source_server_ids = []
        warnings = validate_source_server_ids(result)
        assert len(warnings) >= 1
        assert "topics[0]" in warnings[0]
        assert "empty" in warnings[0]

    def test_validate_warns_on_empty_resources_ids(self):
        """validate_source_server_ids warns when resources has empty ids."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        result.resources[0].source_server_ids = []
        warnings = validate_source_server_ids(result)
        resource_warnings = [w for w in warnings if "resources[" in w]
        assert len(resource_warnings) >= 1
        assert "empty" in resource_warnings[0]

    def test_validate_warns_on_empty_engineering_ids(self):
        """validate_source_server_ids warns when an engineering record has empty ids.

        Engineering records now live in the custom_tables slot (plain dicts);
        the generic validator iterates them via each table's records_key.
        """
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        result.custom_tables["engineering"]["issues"][0]["source_server_ids"] = []
        warnings = validate_source_server_ids(result)
        eng_warnings = [w for w in warnings if "custom_tables.engineering" in w]
        assert len(eng_warnings) >= 1
        assert "empty" in eng_warnings[0]

    def test_validate_does_not_crash_on_empty_output(self):
        """validate_source_server_ids handles empty output gracefully."""
        result = UnifiedReporterOutput(overview="test", trend_analysis="test")
        warnings = validate_source_server_ids(result)
        assert warnings == [], "Empty output should have no warnings"


# ============================================================
# P039: Structural assertions (layered)
# ============================================================


class TestStructuralAssertions:
    """P039: Layered structural assertions for source_server_ids."""

    def test_l1_field_exists_and_is_list(self):
        """L1: source_server_ids field exists and is a list in all mock records."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")

        for i, topic in enumerate(result.topics):
            assert hasattr(topic, "source_server_ids"), f"topics[{i}] missing source_server_ids"
            assert isinstance(topic.source_server_ids, list), (
                f"topics[{i}] source_server_ids is not a list"
            )

        for i, resource in enumerate(result.resources):
            assert hasattr(resource, "source_server_ids"), (
                f"resources[{i}] missing source_server_ids"
            )
            assert isinstance(resource.source_server_ids, list), (
                f"resources[{i}] source_server_ids is not a list"
            )

        eng_issues = result.custom_tables["engineering"]["issues"]
        for i, issue in enumerate(eng_issues):
            assert isinstance(issue, dict), f"engineering issue[{i}] is not a dict"
            assert "source_server_ids" in issue, (
                f"engineering issue[{i}] missing source_server_ids"
            )
            assert isinstance(issue["source_server_ids"], list), (
                f"engineering issue[{i}] source_server_ids is not a list"
            )

    def test_l2_non_empty_count(self):
        """L2: Count of records with non-empty source_server_ids."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")

        all_records = _all_records(result)
        non_empty_count = len([r for r in all_records if _ids_of(r)])
        total_count = len(all_records)

        # Mock data should have 100% fill rate
        assert non_empty_count == total_count, (
            f"Mock data fill rate: {non_empty_count}/{total_count} "
            f"({100 * non_empty_count / total_count:.0f}%), expected 100%"
        )

    def test_l3_fill_rate_calculation(self):
        """L3: Fill rate >= 80% (P039 pattern — test the calculation logic)."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")

        all_records = _all_records(result)
        non_empty = len([r for r in all_records if _ids_of(r)])
        total = len(all_records)

        if total > 0:
            fill_rate = non_empty / total
            assert fill_rate >= 0.8, f"Fill rate {fill_rate:.0%} < 80% ({non_empty}/{total})"


# ============================================================
# parse_json_output integration
# ============================================================


class TestParseJsonWithValidation:
    """Verify parse_json_output calls validate_source_server_ids."""

    def test_parse_valid_json_with_source_server_ids(self):
        """parse_json_output succeeds for valid JSON with source_server_ids."""
        data = {
            "overview": "Test",
            "trend_analysis": "Test",
            "topics": [
                {
                    "topic_id": "tp_test",
                    "topic_name": "Test",
                    "lifecycle": "user_defined",
                    "status": "active",
                    "weight": 0.5,
                    "conclusion": "Test conclusion",
                    "description": "Test description",
                    "trend": "Test trend",
                    "participants": ["Alice"],
                    "first_seen": "2025-01-20",
                    "last_seen": "2025-01-20",
                    "source_server_ids": ["msg_001"],
                }
            ],
            "resources": [
                {
                    "time_range": "09:00-10:00",
                    "resource_type": "link",
                    "summary": "Test",
                    "content": "https://example.com",
                    "source_server_ids": ["msg_002"],
                }
            ],
            "custom_tables": {
                "engineering": {
                    "issues": [
                        {
                            "description": "Test",
                            "source_server_ids": ["msg_003"],
                        }
                    ]
                }
            },
        }
        result = parse_json_output(json.dumps(data))
        assert len(result.topics) == 1
        assert result.topics[0].source_server_ids == ["msg_001"]

    def test_parse_json_with_empty_ids_logs_warning(self, caplog):
        """parse_json_output logs warning for empty source_server_ids."""
        data = {
            "overview": "Test",
            "trend_analysis": "Test",
            "topics": [
                {
                    "topic_id": "tp_test",
                    "topic_name": "Test",
                    "lifecycle": "emerging",
                    "status": "discussion",
                    "weight": 0.5,
                    "conclusion": "Test",
                    "source_server_ids": [],
                }
            ],
        }
        with caplog.at_level(
            logging.WARNING, logger="z_winnow.subagents.unified_reporter.agent"
        ):
            result = parse_json_output(json.dumps(data))

        assert len(result.topics) == 1
        # P014: Warning logged but output still returned
        assert any("source_server_ids" in r.message for r in caplog.records)

    def test_parse_json_without_source_server_ids_logs_warning(self, caplog):
        """parse_json_output logs warning when source_server_ids missing."""
        data = {
            "overview": "Test",
            "trend_analysis": "Test",
            "topics": [
                {"topic_id": "tp_test", "topic_name": "Test"},
            ],
        }
        with caplog.at_level(
            logging.WARNING, logger="z_winnow.subagents.unified_reporter.agent"
        ):
            result = parse_json_output(json.dumps(data))

        # P014: Warning logged but output still returned (no crash)
        assert len(result.topics) == 1
        assert any("source_server_ids" in r.message for r in caplog.records)


# ============================================================
# Mock data verification
# ============================================================


class TestMockSourceServerIds:
    """Verify mock output has source_server_ids in all sections."""

    def test_mock_resources_have_source_server_ids(self):
        """Mock resources include source_server_ids."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        for resource in result.resources:
            assert hasattr(resource, "source_server_ids"), (
                f"Mock resource missing source_server_ids: {resource}"
            )
            assert len(resource.source_server_ids) > 0, (
                f"Mock resource has empty source_server_ids: {resource}"
            )

    def test_mock_engineering_issues_have_source_server_ids(self):
        """Mock engineering records (custom_tables slot) include source_server_ids."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        issues = result.custom_tables["engineering"]["issues"]
        assert len(issues) > 0, "Mock has no engineering issues"
        for issue in issues:
            assert isinstance(issue, dict), f"Mock engineering issue not a dict: {issue}"
            ids = issue.get("source_server_ids")
            assert isinstance(ids, list) and len(ids) > 0, (
                f"Mock engineering issue has empty/missing source_server_ids: {issue}"
            )

    def test_mock_topics_have_source_server_ids(self):
        """Mock topics include source_server_ids."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        for topic in result.topics:
            assert hasattr(topic, "source_server_ids"), (
                f"Mock topic missing source_server_ids: {topic}"
            )
            assert len(topic.source_server_ids) > 0, (
                f"Mock topic has empty source_server_ids: {topic}"
            )

    def test_mock_topics_have_all_lifecycle_types(self):
        """Mock topics cover all three lifecycle types (user_defined, sustained, emerging)."""
        result = _mock_generate_unified_report([], "20250120", "TestGroup")
        lifecycles = {t.lifecycle for t in result.topics}
        assert "user_defined" in lifecycles, "Mock missing user_defined topic"
        assert "sustained" in lifecycles, "Mock missing sustained topic"
        assert "emerging" in lifecycles, "Mock missing emerging topic"
