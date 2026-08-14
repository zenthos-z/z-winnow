"""Tests for W15 new Pydantic schema models (F-W15-SCHEMAS).

Covers acceptance criteria B1-B4:
  B1: All 17 new schemas importable from z_winnow.web.schemas
  B2: RLExportRequest date normalization + end < start ValidationError
  B3: CubeDeleteConfirm confirm=true only; BatchRunRequest empty items → 422
  B4: KeyPeopleOut.is_active defaults True; FeedbackOut.consumed_at/consumed_by present

# P022: Pure DTO — schemas are tested as data structures
# P054: Schema-level validation only — @field_validator tested in B2/B3
# P078: Uses real imports, no mock schemas
# A008: All test fixtures pre-initialized before validation calls
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import ValidationError

# Import all new W15 schemas from the package
from z_winnow.web.schemas import (
    BatchRunItem,
    BatchRunRequest,
    BatchRunResponse,
    CubeDeleteConfirm,
    DataStatsOut,
    FeedbackOut,
    FeishuPushRequest,
    FlushOut,
    KeyPeopleOut,
    KeyPeopleUpdate,
    L1MessageDetailOut,
    MarkdownExportRequest,
    MemCubeListOut,
    MemCubeOut,
    MemoryDetailOut,
    ProvenanceChainOut,
    RebuildRequest,
    RegenerateRequest,
    RLExportRequest,
    VacuumRequest,
)

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """P012: Isolate environment for each test."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


# ============================================================
# B1: All 17 new schemas importable + instantiate from dict
# ============================================================

# A008: Fixtures pre-initialized before any validation calls
W15_NEW_SCHEMAS_AND_FIXTURES = [
    # --- data.py (3 new) ---
    (
        DataStatsOut,
        {
            "total_messages": 15000,
            "total_groups": 5,
            "total_topics": 120,
            "total_reports": 30,
            "date_range_start": "2026-01-01",
            "date_range_end": "2026-06-01",
        },
    ),
    (
        L1MessageDetailOut,
        {
            "serverID": "srv_001",
            "date": "2026-06-01",
            "group_id": "g_test_001",
            "sender": "Alice",
            "content": "Let's discuss the architecture.",
            "msg_type": "text",
            "image_path": None,
            "sanitized": 0,
            "raw_json": '{"type":"text","content":"..."}',
            "created_at": "2026-06-01 08:30:00",
            "contexts": [{"context_id": "ctx_001", "context_text": "Discussion..."}],
            "summaries": [{"summary_id": "sum_001", "topic_name": "Architecture"}],
        },
    ),
    (
        ProvenanceChainOut,
        {
            "server_id": "srv_001",
            "message": {"serverID": "srv_001", "content": "Hello", "sender": "Alice"},
            "topics": [{"summary_id": "sum_001", "topic_name": "Architecture"}],
        },
    ),
    # --- runs.py (3 new) ---
    (
        BatchRunItem,
        {"component": "daily_reporter", "group_id": "g_test_001", "date": "2026-06-01"},
    ),
    (
        BatchRunRequest,
        {
            "items": [
                {"component": "daily_reporter", "group_id": "g1"},
                {"component": "engineering_reporter", "group_id": "g2", "date": "2026-06-01"},
            ]
        },
    ),
    (
        BatchRunResponse,
        {"batch_id": "batch_001", "total": 3, "status": "accepted", "results": []},
    ),
    # --- reports.py (3 new) ---
    (
        RegenerateRequest,
        {"group_id": "g_test_001", "date": "2026-06-01"},
    ),
    (
        MarkdownExportRequest,
        {"group_id": "g_test_001", "date": "2026-06-01"},
    ),
    (
        FeishuPushRequest,
        {"report_id": "rpt_001", "doc_title": "Daily Report 2026-06-01"},
    ),
    # --- memos.py (7 new) ---
    (
        MemCubeOut,
        {
            "cube_id": "cube_001",
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "summary": "Architecture discussion summary",
            "message_count": 42,
            "status": "active",
            "created_at": "2026-06-01 10:00:00",
        },
    ),
    (
        MemCubeListOut,
        {"total": 1, "cubes": []},
    ),
    (
        CubeDeleteConfirm,
        {"confirm": True, "cube_id": "cube_001"},
    ),
    (
        RebuildRequest,
        {"group_id": "g_test_001", "full": True},
    ),
    (
        VacuumRequest,
        {"group_id": "g_test_001", "dry_run": True},
    ),
    (
        MemoryDetailOut,
        {
            "memory_id": "mem_001",
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "content": "Architecture decision: microservices",
            "source": "daily_reporter",
            "metadata_json": '{"confidence":0.92}',
            "created_at": "2026-06-01 09:00:00",
        },
    ),
    (
        FlushOut,
        {"status": "completed", "flushed_count": 15, "message": "Flush successful"},
    ),
    # --- export.py (1 new) ---
    (
        RLExportRequest,
        {"group_id": "g_test_001", "start_date": "2026-01-01", "end_date": "2026-06-01"},
    ),
]


class TestB1AllSchemasImportable:
    """B1: All 17 new schemas importable and constructable from dicts."""

    @pytest.mark.parametrize(
        "model_cls, fixture",
        W15_NEW_SCHEMAS_AND_FIXTURES,
        ids=[m.__name__ for m, _ in W15_NEW_SCHEMAS_AND_FIXTURES],
    )
    def test_model_validate_from_dict(self, model_cls, fixture):
        """Each new schema constructs from a realistic dict without ValidationError."""
        instance = model_cls.model_validate(fixture)
        assert isinstance(instance, model_cls)

        # Verify all declared fields are present in dump output
        dumped = instance.model_dump()
        for field_name in model_cls.model_fields:
            assert field_name in dumped, (
                f"Field '{field_name}' declared in {model_cls.__name__} "
                f"but missing from model_dump()"
            )

    def test_all_17_importable_from_package(self):
        """B1: All 17 new schemas are importable from z_winnow.web.schemas."""
        new_schema_names = [
            "DataStatsOut",
            "L1MessageDetailOut",
            "ProvenanceChainOut",
            "BatchRunRequest",
            "BatchRunItem",
            "BatchRunResponse",
            "RegenerateRequest",
            "MarkdownExportRequest",
            "FeishuPushRequest",
            "MemCubeOut",
            "MemCubeListOut",
            "CubeDeleteConfirm",
            "RebuildRequest",
            "VacuumRequest",
            "MemoryDetailOut",
            "FlushOut",
            "RLExportRequest",
        ]
        import z_winnow.web.schemas as schema_pkg

        for name in new_schema_names:
            obj = getattr(schema_pkg, name)
            assert obj is not None, f"Schema '{name}' not found in package"
            assert name in schema_pkg.__all__, f"Schema '{name}' not in __all__"


# ============================================================
# B2: RLExportRequest date normalization + range validation
# ============================================================


class TestB2RLExportRequest:
    """B2: RLExportRequest date normalization and range validation."""

    def test_date_normalization_yyyy_mm_dd_to_yyyymmdd(self):
        """YYYY-MM-DD input is normalized to YYYYMMDD."""
        req = RLExportRequest(
            group_id="g1",
            start_date="2026-01-15",
            end_date="2026-06-01",
        )
        assert req.start_date == "20260115"
        assert req.end_date == "20260601"

    def test_date_already_yyyymmdd_preserved(self):
        """Already YYYYMMDD input is preserved as-is."""
        req = RLExportRequest(
            group_id="g1",
            start_date="20260101",
            end_date="20260601",
        )
        assert req.start_date == "20260101"
        assert req.end_date == "20260601"

    def test_same_day_range_valid(self):
        """start_date == end_date is valid."""
        req = RLExportRequest(
            group_id="g1",
            start_date="2026-01-01",
            end_date="2026-01-01",
        )
        assert req.start_date == req.end_date

    def test_end_before_start_raises_validation_error(self):
        """B2: end_date < start_date raises ValidationError."""
        with pytest.raises(ValidationError):
            RLExportRequest(
                group_id="g1",
                start_date="2026-06-01",
                end_date="2026-01-01",
            )

    def test_invalid_date_format_raises_validation_error(self):
        """Non-date strings raise ValidationError."""
        with pytest.raises(ValidationError):
            RLExportRequest(
                group_id="g1",
                start_date="not-a-date",
                end_date="2026-06-01",
            )

    def test_empty_group_id_raises_validation_error(self):
        """Empty group_id raises ValidationError."""
        with pytest.raises(ValidationError):
            RLExportRequest(
                group_id="",
                start_date="2026-01-01",
                end_date="2026-06-01",
            )


# ============================================================
# B3: CubeDeleteConfirm + BatchRunRequest validation
# ============================================================


class TestB3CubeDeleteConfirm:
    """B3: CubeDeleteConfirm validates confirm=true only."""

    def test_confirm_true_accepted(self):
        """confirm=True is valid."""
        req = CubeDeleteConfirm(confirm=True, cube_id="cube_001")
        assert req.confirm is True

    def test_confirm_false_raises_validation_error(self):
        """B3: confirm=False raises ValidationError."""
        with pytest.raises(ValidationError):
            CubeDeleteConfirm(confirm=False, cube_id="cube_001")

    def test_confirm_default_false_raises_validation_error(self):
        """Default confirm=False raises ValidationError."""
        with pytest.raises(ValidationError):
            CubeDeleteConfirm(cube_id="cube_001")

    def test_confirm_none_raises_validation_error(self):
        """confirm=None raises ValidationError (Pydantic catches None→bool before our validator)."""
        with pytest.raises(ValidationError):
            CubeDeleteConfirm(confirm=None, cube_id="cube_001")  # type: ignore[arg-type]


class TestB3BatchRunRequest:
    """B3: BatchRunRequest with empty items rejects with ValidationError."""

    def test_with_items_accepted(self):
        """Non-empty items list is valid."""
        req = BatchRunRequest(
            items=[
                BatchRunItem(component="daily_reporter", group_id="g1"),
            ]
        )
        assert len(req.items) == 1

    def test_empty_items_raises_validation_error(self):
        """B3: Empty items list raises ValidationError (422)."""
        with pytest.raises(ValidationError):
            BatchRunRequest(items=[])

    def test_missing_items_raises_validation_error(self):
        """Missing items field raises ValidationError."""
        with pytest.raises(ValidationError):
            BatchRunRequest.model_validate({})


# ============================================================
# B4: KeyPeopleOut.is_active + FeedbackOut.consumed_at/consumed_by
# ============================================================


class TestB4KeyPeopleOut:
    """B4: KeyPeopleOut.is_active exists and defaults True."""

    def test_is_active_field_exists_and_defaults_true(self):
        """B4: KeyPeopleOut has is_active field that defaults to True."""
        kp = KeyPeopleOut(
            sender="Alice",
            message_count=42,
        )
        assert hasattr(kp, "is_active")
        assert kp.is_active is True

    def test_is_active_can_be_set_false(self):
        """is_active can be explicitly set to False."""
        kp = KeyPeopleOut(
            sender="Bob",
            is_active=False,
        )
        assert kp.is_active is False

    def test_is_active_in_model_dump(self):
        """is_active appears in model_dump output."""
        kp = KeyPeopleOut(sender="Alice")
        dumped = kp.model_dump()
        assert "is_active" in dumped
        assert dumped["is_active"] is True

    def test_key_people_update_has_is_active(self):
        """B4: KeyPeopleUpdate has is_active field (optional)."""
        update = KeyPeopleUpdate(is_active=False)
        assert update.is_active is False

        update_none = KeyPeopleUpdate()
        assert update_none.is_active is None


class TestB4FeedbackOut:
    """B4: FeedbackOut.consumed_at and consumed_by still present."""

    def test_consumed_at_field_exists(self):
        """FeedbackOut has consumed_at field."""
        fb = FeedbackOut(
            feedback_id="fb_001",
            group_id="g1",
            date="2026-06-01",
            target_type="section",
            signal="positive",
            consumed_at="2026-06-02 10:00:00",
            consumed_by="admin",
        )
        assert fb.consumed_at == "2026-06-02 10:00:00"

    def test_consumed_by_field_exists(self):
        """FeedbackOut has consumed_by field."""
        fb = FeedbackOut(
            feedback_id="fb_002",
            group_id="g1",
            date="2026-06-01",
            target_type="section",
            signal="positive",
            consumed_by="system",
        )
        assert fb.consumed_by == "system"

    def test_consumed_fields_default_none(self):
        """consumed_at and consumed_by default to None."""
        fb = FeedbackOut(
            feedback_id="fb_003",
            group_id="g1",
            date="2026-06-01",
            target_type="section",
            signal="positive",
        )
        assert fb.consumed_at is None
        assert fb.consumed_by is None


# ============================================================
# P022: All new schemas use ConfigDict(from_attributes=True) for Out models
# ============================================================


class TestP022PureDTO:
    """P022: Out schemas use ConfigDict(from_attributes=True)."""

    OUT_MODELS: ClassVar = [
        DataStatsOut,
        L1MessageDetailOut,
        ProvenanceChainOut,
        BatchRunResponse,
        MemCubeOut,
        MemCubeListOut,
        MemoryDetailOut,
        FlushOut,
    ]

    @pytest.mark.parametrize("model_cls", OUT_MODELS, ids=[m.__name__ for m in OUT_MODELS])
    def test_out_models_have_from_attributes(self, model_cls):
        """Every *Out model has ConfigDict(from_attributes=True)."""
        config = model_cls.model_config
        assert config.get("from_attributes") is True, (
            f"{model_cls.__name__} missing from_attributes=True"
        )


# ============================================================
# Schema shape checks — verify ProvenanceChainOut != ProvenanceOut
# ============================================================


class TestSchemaShapeIntegrity:
    """Verify new schemas don't collide with existing ones."""

    def test_provenance_chain_out_different_from_provenance_out(self):
        """ProvenanceChainOut has server_id→message+topics, not summary_id→server_ids+context_ids."""
        pc = ProvenanceChainOut(
            server_id="srv_001",
            message={"content": "Hello"},
            topics=[],
        )
        dumped = pc.model_dump()
        # ProvenanceChainOut has server_id, message, topics
        assert "server_id" in dumped
        assert "message" in dumped
        assert "topics" in dumped
        # It does NOT have summary_id, server_ids, context_ids (ProvenanceOut fields)
        assert "summary_id" not in dumped
        assert "server_ids" not in dumped
        assert "context_ids" not in dumped

    def test_rl_export_request_different_from_export_request(self):
        """RLExportRequest uses group_id+dates, not report_id+format."""
        rl = RLExportRequest(
            group_id="g1",
            start_date="2026-01-01",
            end_date="2026-06-01",
        )
        dumped = rl.model_dump()
        # RLExportRequest has group_id, start_date, end_date
        assert "group_id" in dumped
        assert "start_date" in dumped
        assert "end_date" in dumped
        # It does NOT have report_id (ExportRequest's required field)
        assert "report_id" not in dumped
