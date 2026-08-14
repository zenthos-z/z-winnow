"""Test custom_tables field extension in UnifiedReporterOutput.

CT-4: Model extension to support dynamic table slots.
"""

import importlib.util
import typing

import pytest
from pydantic import ValidationError

# Direct import of models.py to avoid dependency chain issues in isolated testing
spec = importlib.util.spec_from_file_location(
    "models", "src/z_winnow/subagents/unified_reporter/models.py"
)
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)


models.UnifiedReporterOutput.model_rebuild(
    _types_namespace={
        "Topic": models.Topic,
        "Resource": models.Resource,
        "EngineeringIssue": models.EngineeringIssue,
        "UnifiedReporterOutput": models.UnifiedReporterOutput,
        "Any": typing.Any,
    }
)

UnifiedReporterOutput = models.UnifiedReporterOutput


class TestCustomTablesField:
    """AC1-AC6: Model extension tests."""

    def test_model_has_custom_tables_field(self):
        """AC1: UnifiedReporterOutput contains custom_tables field."""
        # Verify field exists in model fields
        assert "custom_tables" in UnifiedReporterOutput.model_fields
        # Verify field can be accessed on an instance
        output = UnifiedReporterOutput(overview="test overview", trend_analysis="test trend")
        assert hasattr(output, "custom_tables")

    def test_custom_tables_field_type(self):
        """AC2: custom_tables field type is dict[str, Any]."""
        from typing import Any, get_args, get_origin

        field_info = UnifiedReporterOutput.model_fields["custom_tables"]
        # Get the annotation and verify it's dict[str, Any]
        annotation = field_info.annotation
        # The annotation should be dict[str, Any]
        assert get_origin(annotation) is dict
        args = get_args(annotation)
        assert args[0] is str
        # args[1] should be Any
        assert args[1] is Any

    def test_custom_tables_default_empty_dict(self):
        """AC3: custom_tables defaults to empty dict (default_factory=dict)."""
        output = UnifiedReporterOutput(overview="test overview", trend_analysis="test trend")
        assert output.custom_tables == {}
        assert isinstance(output.custom_tables, dict)

    def test_engineering_moved_to_custom_tables_slot(self):
        """engineering no longer has a hardcoded top-level field — it flows through
        the generic ``custom_tables`` slot (extra='forbid' rejects the legacy key)."""
        # Legacy top-level field is gone
        assert "engineering_issues" not in UnifiedReporterOutput.model_fields
        assert "group_summary" not in UnifiedReporterOutput.model_fields
        # Generic slot is the destination
        assert "custom_tables" in UnifiedReporterOutput.model_fields
        output = UnifiedReporterOutput(
            overview="test overview",
            trend_analysis="test trend",
            custom_tables={"engineering": {"issues": [], "group_summary": {}}},
        )
        assert output.custom_tables["engineering"]["issues"] == []
        # Legacy top-level key is now rejected (extra='forbid')
        with pytest.raises(ValidationError):
            UnifiedReporterOutput.model_validate(
                {"overview": "x", "trend_analysis": "y", "engineering_issues": []}
            )

    def test_extra_forbid_rejects_unknown_fields(self):
        """AC5: extra='forbid' still applies, unknown fields trigger ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            UnifiedReporterOutput(
                overview="test overview",
                trend_analysis="test trend",
                unknown_field="should_be_rejected",
            )
        # Verify error mentions extra field
        errors = exc_info.value.errors()
        assert any("unknown_field" in str(e) for e in errors)

    def test_model_dump_includes_custom_tables(self):
        """AC6: model_dump output includes custom_tables."""
        output = UnifiedReporterOutput(
            overview="test overview",
            trend_analysis="test trend",
            custom_tables={"table1": [{"col1": "val1"}]},
        )
        dumped = output.model_dump()
        assert "custom_tables" in dumped
        assert dumped["custom_tables"] == {"table1": [{"col1": "val1"}]}


class TestCustomTablesUsage:
    """Additional tests for custom_tables field usage scenarios."""

    def test_custom_tables_with_complex_data(self):
        """custom_tables can hold complex nested data structures."""
        complex_data = {
            "engineering_table": [
                {"issue": "bug1", "status": "fixed"},
                {"issue": "bug2", "status": "open"},
            ],
            "metrics_table": {"total": 100, "resolved": 80, "pending": 20},
        }
        output = UnifiedReporterOutput(
            overview="test overview", trend_analysis="test trend", custom_tables=complex_data
        )
        assert output.custom_tables == complex_data

    def test_custom_tables_default_factory_creates_new_dict(self):
        """Verify default_factory creates a new dict per instance."""
        output1 = UnifiedReporterOutput(overview="overview1", trend_analysis="trend1")
        output2 = UnifiedReporterOutput(overview="overview2", trend_analysis="trend2")
        # Each instance should have its own dict
        output1.custom_tables["key1"] = "value1"
        assert "key1" not in output2.custom_tables
