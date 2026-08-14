"""T-W12-11: test_contracts — verify contracts.py only has active types.

P060: Removed-Dependency Test Conversion — tests verify:
1. contracts.py only exports active types (unified_reporter + output_composer)
2. Deleted types are truly absent (DailyReporter, ResourceExtractor, etc.)
3. SubagentInput/SubagentOutput aliases are updated
4. subagents/__init__.py re-exports are consistent
"""

from __future__ import annotations

import pytest

import z_winnow.subagents as subagents_pkg
import z_winnow.subagents.contracts as contracts_mod

# ============================================================
# Active types must exist
# ============================================================


class TestActiveTypesExist:
    """Verify all active I/O types are importable."""

    def test_unified_reporter_output_exists(self):
        assert hasattr(contracts_mod, "UnifiedReporterOutput")

    def test_output_composer_input_exists(self):
        assert hasattr(contracts_mod, "OutputComposerInput")

    def test_output_composer_output_exists(self):
        assert hasattr(contracts_mod, "OutputComposerOutput")

    def test_subagent_input_alias_exists(self):
        assert hasattr(contracts_mod, "SubagentInput")

    def test_subagent_output_alias_exists(self):
        assert hasattr(contracts_mod, "SubagentOutput")


# ============================================================
# Deleted types must be absent
# ============================================================

_DELETED_TYPES = [
    "DailyReporterInput",
    "DailyReporterOutput",
    "ResourceExtractorInput",
    "ResourceExtractorOutput",
    "EngineeringAnalyzerInput",
    "EngineeringAnalyzerOutput",
    "TopicTableRow",
    "ResourceItem",
    "EngineeringIssue",
    "TopicEntry",
    "TopicTracker",
]


class TestDeletedTypesAbsent:
    """P060 + A002: Verify all deleted types are truly absent from contracts.py."""

    @pytest.mark.parametrize("type_name", _DELETED_TYPES)
    def test_type_absent_from_contracts(self, type_name):
        assert not hasattr(contracts_mod, type_name), (
            f"{type_name} should have been removed from contracts.py"
        )

    @pytest.mark.parametrize("type_name", _DELETED_TYPES)
    def test_type_absent_from_subagents_init(self, type_name):
        """L070: __init__.py must not re-export deleted types."""
        assert not hasattr(subagents_pkg, type_name), (
            f"{type_name} should have been removed from subagents/__init__.py"
        )


# ============================================================
# subagents/__init__.py consistency
# ============================================================


class TestInitExports:
    """Verify __init__.py exports match contracts.py active types."""

    def test_init_exports_active_contract_types(self):
        """Active contract types are re-exported from subagents package."""
        assert hasattr(subagents_pkg, "UnifiedReporterOutput")
        assert hasattr(subagents_pkg, "OutputComposerInput")
        assert hasattr(subagents_pkg, "OutputComposerOutput")
        assert hasattr(subagents_pkg, "SubagentInput")
        assert hasattr(subagents_pkg, "SubagentOutput")

    def test_init_all_list_no_deleted_types(self):
        """__all__ does not contain any deleted type names."""
        for type_name in _DELETED_TYPES:
            assert type_name not in subagents_pkg.__all__, (
                f"{type_name} should have been removed from subagents/__all__"
            )

    def test_init_all_list_has_active_types(self):
        """__all__ contains all active contract types."""
        active_types = [
            "UnifiedReporterOutput",
            "OutputComposerInput",
            "OutputComposerOutput",
            "SubagentInput",
            "SubagentOutput",
        ]
        for type_name in active_types:
            assert type_name in subagents_pkg.__all__, f"{type_name} should be in subagents/__all__"


# ============================================================
# Pydantic model instantiation
# ============================================================


class TestPydanticModels:
    """Verify active Pydantic models work correctly."""

    def test_unified_reporter_output_instantiation(self):
        model = contracts_mod.UnifiedReporterOutput(
            overview="Test overview",
            trend_analysis="Test trend",
        )
        assert model.overview == "Test overview"
        assert model.trend_analysis == "Test trend"
        assert model.topics == []
        assert model.resources == []
        assert model.custom_tables == {}

    def test_unified_reporter_output_topics_with_lifecycle(self):
        """Topics use unified lifecycle values: user_defined, sustained, emerging."""
        model = contracts_mod.UnifiedReporterOutput(
            overview="Overview",
            trend_analysis="Trend",
            topics=[
                {
                    "topic_id": "T1",
                    "topic_name": "Core topic",
                    "lifecycle": "user_defined",
                    "status": "active",
                    "weight": 5,
                    "conclusion": "conclusion text",
                    "description": "desc",
                    "trend": "steady",
                    "participants": ["alice"],
                    "source_server_ids": ["s1"],
                    "first_seen": "20260520",
                    "last_seen": "20260520",
                },
                {
                    "topic_id": "T2",
                    "topic_name": "Sustained topic",
                    "lifecycle": "sustained",
                    "status": "active",
                    "weight": 3,
                    "conclusion": "conclusion text",
                    "description": "desc",
                    "trend": "growing",
                    "participants": ["bob"],
                    "source_server_ids": ["s2"],
                    "first_seen": "20260510",
                    "last_seen": "20260520",
                },
                {
                    "topic_id": "T3",
                    "topic_name": "Emerging topic",
                    "lifecycle": "emerging",
                    "status": "active",
                    "weight": 1,
                    "conclusion": "conclusion text",
                    "description": "desc",
                    "trend": "new",
                    "participants": ["carol"],
                    "source_server_ids": ["s3"],
                    "first_seen": "20260520",
                    "last_seen": "20260520",
                },
            ],
        )
        assert len(model.topics) == 3
        assert model.topics[0].lifecycle == "user_defined"
        assert model.topics[1].lifecycle == "sustained"
        assert model.topics[2].lifecycle == "emerging"

    def test_unified_reporter_output_no_old_lifecycle_values(self):
        """Old lifecycle values (core, continuous, new) must not appear."""
        model = contracts_mod.UnifiedReporterOutput(
            overview="Overview",
            trend_analysis="Trend",
            topics=[
                {
                    "topic_id": "T1",
                    "topic_name": "Test",
                    "lifecycle": "sustained",
                    "status": "active",
                    "weight": 1,
                    "conclusion": "",
                    "description": "",
                    "trend": "",
                    "participants": [],
                    "source_server_ids": [],
                    "first_seen": "",
                    "last_seen": "",
                },
            ],
        )
        valid_lifecycles = {"user_defined", "sustained", "emerging"}
        for topic in model.topics:
            assert topic.lifecycle in valid_lifecycles, (
                f"lifecycle '{topic.lifecycle}' is not a valid value; "
                f"expected one of {valid_lifecycles}"
            )

    def test_unified_reporter_output_old_fields_forbidden(self):
        """Old fields topic_sections and topic_tracking must not exist on the model."""
        model = contracts_mod.UnifiedReporterOutput(
            overview="Overview",
            trend_analysis="Trend",
        )
        assert not hasattr(model, "topic_sections"), (
            "topic_sections field should have been removed from UnifiedReporterOutput"
        )
        assert not hasattr(model, "topic_tracking"), (
            "topic_tracking field should have been removed from UnifiedReporterOutput"
        )

    def test_output_composer_input_instantiation(self):
        model = contracts_mod.OutputComposerInput(
            report_type="daily",
            date="20260520",
            group_name="test_group",
        )
        assert model.report_type == "daily"
        assert model.daily_reports == []

    def test_output_composer_output_instantiation(self):
        model = contracts_mod.OutputComposerOutput(
            final_report="# Report",
            sections=["section1"],
            report_type="daily",
        )
        assert model.final_report == "# Report"
