"""Tests for web API Pydantic schema models.

Covers acceptance criteria B1-B5 from T-W14-1:
  B1: Schema instantiation from dict (model_validate)
  B2: PaginatedResponse generic envelope
  B3: Field-to-SQLite column alignment (real DDL)
  B4: Request models validate and reject invalid input
  B5: __init__.py exports are complete and importable

# P011: Each B-criterion has a dedicated test class.
# P078: B3 uses real DDL strings from database.py.
# P012: autouse monkeypatch env isolation
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, ValidationError

# ============================================================
# P078: Import real DDL strings from database.py for B3
# ============================================================
from z_winnow.pipeline.database import (
    REPORT_SCHEMA_SQL,
    SCHEMA_SQL,
    WEB_SCHEMA_SQL,
)

# Import all schema classes
from z_winnow.web import schemas as schema_pkg
from z_winnow.web.schemas import (
    ConfigOut,
    CoreTopicCreate,
    CoreTopicOut,
    ExportRequest,
    ExportStatusOut,
    FeedbackCreate,
    FeedbackOut,
    GroupCreate,
    GroupMemberOut,
    GroupOut,
    HealthCheckOut,
    JudgeDimensionScore,
    JudgeResultOut,
    KeyPeopleOut,
    L1MessageOut,
    L2ContextOut,
    L3SummaryOut,
    MemosHealthOut,
    MemosSearchOut,
    OverviewStatsOut,
    PaginatedResponse,
    ProvenanceOut,
    ReportDiffOut,
    ReportOut,
    ReportVersionOut,
    RunCreate,
    RunStatusOut,
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
# Helpers
# ============================================================


def _parse_columns(ddl: str, table_name: str) -> set[str]:
    """Extract column names from a CREATE TABLE block in DDL string.

    Uses regex to parse the real DDL — no hardcoded column lists.
    """
    # Find the CREATE TABLE block for the given table
    pattern = rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{table_name}\s*\((.*?)\);"
    match = re.search(pattern, ddl, re.DOTALL | re.IGNORECASE)
    if not match:
        return set()

    body = match.group(1)
    columns: set[str] = set()
    for line in body.split("\n"):
        line = line.strip().rstrip(",")
        if not line:
            continue
        # Skip constraints and indexes
        upper = line.upper()
        if upper.startswith(("PRIMARY KEY", "UNIQUE(", "FOREIGN", "CHECK", "CONSTRAINT")):
            continue
        if upper.startswith("CREATE "):
            continue
        # First token is the column name
        col_name = line.split()[0]
        if col_name.isidentifier():
            columns.add(col_name)
    return columns


# ============================================================
# B1: Schema instantiation from dict
# ============================================================

# Realistic dict fixtures matching actual SQLite row shapes
# # A019: Realistic fixtures, not minimal stubs

OUT_MODELS_AND_FIXTURES = [
    (
        GroupOut,
        {
            "group_id": "g_test_001",
            "display_name": "Test Group",
            "chatroom_id": "12345@chatroom",
            "output_dir": "/data/groups/test",
            "feishu_enabled": 1,
            "custom_prompt_hints": "Focus on engineering topics",
            "is_active": 1,
            "daily_report_enabled": 1,
            "daily_schedule_cron": "0 9 * * *",
            "created_at": "2026-06-01 10:00:00",
            "updated_at": "2026-06-02 11:00:00",
            "created_by": "admin",
        },
    ),
    (
        GroupMemberOut,
        {
            "member_id": "m_001",
            "group_id": "g_test_001",
            "name": "Alice",
            "wxid": "wxid_abc123",
            "role": "member",
            "weight": 1.5,
            "note": "Tech lead",
            "is_active": 1,
            "created_at": "2026-06-01 10:00:00",
        },
    ),
    (
        CoreTopicOut,
        {
            "core_topic_id": "ct_001",
            "group_id": "g_test_001",
            "name": "Architecture Review",
            "description": "Weekly architecture discussions",
            "keywords": "arch,review,design",
            "priority": 5,
            "is_active": 1,
            "last_matched_date": "2026-06-01",
            "match_count": 12,
            "created_at": "2026-05-01 08:00:00",
            "updated_at": "2026-06-01 09:00:00",
            "created_by": "admin",
        },
    ),
    (
        FeedbackOut,
        {
            "feedback_id": "fb_001",
            "created_at": "2026-06-01 12:00:00",
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "report_id": "rpt_001",
            "target_type": "section",
            "target_id": "s_topic_1",
            "target_path": "/topics/1",
            "signal": "positive",
            "severity": "info",
            "rating": "good",
            "tags": '["quality"]',
            "correction_mode": None,
            "original_text": None,
            "corrected_text": None,
            "correction_note": None,
            "reporter": "admin",
            "consumed_at": None,
            "consumed_by": None,
        },
    ),
    (
        ReportOut,
        {
            "report_id": "rpt_001",
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "latest_version": 2,
            "source": "daily_run",
            "created_at": "2026-06-01 10:00:00",
        },
    ),
    (
        ReportVersionOut,
        {
            "version_id": "rpt_001-v2",
            "report_id": "rpt_001",
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "version_number": 2,
            "content": "# Daily Report\n## Topics\n...",
            "content_changed": 1,
            "source": "daily_run",
            "build_duration_s": 12.5,
            "created_at": "2026-06-01 10:00:00",
        },
    ),
    (
        RunStatusOut,
        {
            "run_id": "run_001",
            "component": "daily_reporter",
            "status": "completed",
            "started_at": "2026-06-01 09:00:00",
            "completed_at": "2026-06-01 09:15:00",
            "message_count": 150,
            "error_message": None,
            "current_node": "report_written",
            "progress_pct": 100,
            "node_history": '["fetch","parse","summarize","report"]',
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "created_at": "2026-06-01 09:00:00",
        },
    ),
    (
        L1MessageOut,
        {
            "serverID": "srv_001",
            "date": "2026-06-01",
            "group_id": "g_test_001",
            "sender": "Alice",
            "content": "Hello, let's discuss the architecture.",
            "msg_type": "text",
            "image_path": None,
            "sanitized": 0,
            "raw_json": '{"type":"text","content":"..."}',
            "created_at": "2026-06-01 08:30:00",
        },
    ),
    (
        L2ContextOut,
        {
            "context_id": "ctx_001",
            "date": "2026-06-01",
            "group_id": "g_test_001",
            "server_ids": "srv_001,srv_002,srv_003",
            "context_text": "Discussion about architecture...",
            "token_count": 512,
            "source_subagent": "daily_reporter",
            "created_at": "2026-06-01 09:00:00",
        },
    ),
    (
        L3SummaryOut,
        {
            "summary_id": "sum_001",
            "date": "2026-06-01",
            "group_id": "g_test_001",
            "topic_name": "Architecture Review",
            "topic_id": "topic_arch_001",
            "summary_text": "The team discussed microservices vs monolith...",
            "context_ids": "ctx_001,ctx_002",
            "source_server_ids": "srv_001,srv_002",
            "confidence": 0.92,
            "model_used": "claude-sonnet-4-20250514",
            "lifecycle": "active",
            "matched_core_topic_id": "ct_001",
            "conclusion": "Proceed with microservices",
            "description": "Architecture decision discussion",
            "participants": "Alice,Bob,Charlie",
            "trend": "increasing",
            "created_at": "2026-06-01 09:05:00",
        },
    ),
    (
        KeyPeopleOut,
        {
            "sender": "Alice",
            "message_count": 42,
            "first_seen": "2026-01-01",
            "last_seen": "2026-06-01",
            "group_id": "g_test_001",
        },
    ),
    (
        MemosHealthOut,
        {
            "status": "ok",
            "url": "https://memos.example.com",
            "connected": True,
            "last_sync": "2026-06-01 10:00:00",
            "pending_queue": 3,
        },
    ),
    (
        MemosSearchOut,
        {
            "query": "architecture",
            "total": 2,
            "results": [
                {
                    "memo_id": "memo_001",
                    "content": "Architecture notes",
                    "tags": ["arch"],
                    "created_at": "2026-06-01",
                    "updated_at": None,
                }
            ],
        },
    ),
    (
        JudgeResultOut,
        {
            "report_id": "rpt_001",
            "overall_score": 0.85,
            "dimensions": [
                {
                    "dimension": "completeness",
                    "score": 0.9,
                    "evidence": "All sections present",
                    "passed": True,
                }
            ],
            "summary": "Good quality report",
            "judged_at": "2026-06-01 10:00:00",
            "model_used": "claude-sonnet-4-20250514",
        },
    ),
    (
        ExportStatusOut,
        {
            "task_id": "task_001",
            "report_id": "rpt_001",
            "format": "json",
            "status": "completed",
            "download_url": "/api/v1/exports/task_001/download",
            "error": None,
            "created_at": "2026-06-01 10:00:00",
        },
    ),
    (
        HealthCheckOut,
        {
            "status": "ok",
            "version": "2.14.0",
            "database": "ok",
            "uptime_seconds": 3600.0,
        },
    ),
    (
        ConfigOut,
        {
            "db_path": "data/winnow.db",
            "web_port": 8100,
            "default_model": "claude-sonnet-4-20250514",
            "log_level": "INFO",
            "features": {"rl_enabled": True},
        },
    ),
    (
        OverviewStatsOut,
        {
            "total_messages": 15000,
            "total_groups": 5,
            "total_topics": 120,
            "total_reports": 30,
            "total_feedback": 50,
            "last_sync_at": "2026-06-01 10:00:00",
            "active_runs": 2,
        },
    ),
    (
        ProvenanceOut,
        {
            "summary_id": "sum_001",
            "server_ids": ["srv_001", "srv_002"],
            "context_ids": ["ctx_001"],
        },
    ),
    (
        ReportDiffOut,
        {
            "report_id": "rpt_001",
            "group_id": "g_test_001",
            "date": "2026-06-01",
            "old_version": 1,
            "new_version": 2,
            "old_content": "# v1",
            "new_content": "# v2",
            "content_changed": True,
        },
    ),
]


class TestB1SchemaInstantiationFromDict:
    """B1: Every *Out model can be constructed from a plain dict with model_validate."""

    @pytest.mark.parametrize(
        "model_cls, fixture",
        OUT_MODELS_AND_FIXTURES,
        ids=[m.__name__ for m, _ in OUT_MODELS_AND_FIXTURES],
    )
    def test_model_validate_from_dict(self, model_cls: type[BaseModel], fixture: dict):
        """Each *Out model constructs from a realistic dict without ValidationError."""
        instance = model_cls.model_validate(fixture)
        assert isinstance(instance, model_cls)

        # # A025: Verify all declared fields are present in dump output
        dumped = instance.model_dump()
        for field_name in model_cls.model_fields:
            assert field_name in dumped, (
                f"Field '{field_name}' declared in {model_cls.__name__} "
                f"but missing from model_dump()"
            )


# ============================================================
# B2: PaginatedResponse generic envelope
# ============================================================


class TestB2PaginatedResponse:
    """B2: PaginatedResponse[T] works for concrete types."""

    def test_paginated_response_group_out(self):
        """PaginatedResponse[GroupOut] serializes correctly."""
        group = GroupOut(
            group_id="g1",
            display_name="Test",
            chatroom_id="123@chatroom",
        )
        page = PaginatedResponse[GroupOut](
            total=1,
            page=1,
            page_size=50,
            items=[group],
        )
        json_str = page.model_dump_json()
        parsed = PaginatedResponse[GroupOut].model_validate_json(json_str)
        assert parsed.total == 1
        assert len(parsed.items) == 1
        assert parsed.items[0].group_id == "g1"

    def test_paginated_response_l1_message(self):
        """PaginatedResponse[L1MessageOut] works independently."""
        msg = L1MessageOut(
            serverID="s1",
            date="2026-06-01",
            sender="Alice",
            content="Hello",
        )
        page = PaginatedResponse[L1MessageOut](
            total=10,
            page=2,
            page_size=5,
            items=[msg],
        )
        dumped = page.model_dump()
        assert dumped["total"] == 10
        assert dumped["page"] == 2
        assert len(dumped["items"]) == 1
        assert dumped["items"][0]["serverID"] == "s1"

    def test_paginated_response_empty_items(self):
        """PaginatedResponse handles empty items list."""
        page = PaginatedResponse[GroupOut](
            total=0,
            page=1,
            page_size=50,
            items=[],
        )
        assert page.items == []
        assert page.total == 0


# ============================================================
# B3: Field-to-SQLite column alignment
# ============================================================

# Mapping of *Out models to their corresponding DDL table
# P078: Uses real DDL strings from database.py
TABLE_DDL_MAP = {
    "raw_messages": SCHEMA_SQL,
    "parsed_contexts": SCHEMA_SQL,
    "topic_summaries": SCHEMA_SQL,
    "pipeline_runs": SCHEMA_SQL,
    "groups": WEB_SCHEMA_SQL,
    "group_members": WEB_SCHEMA_SQL,
    "core_topics": WEB_SCHEMA_SQL,
    "feedback_events": WEB_SCHEMA_SQL,
    "report_versions": REPORT_SCHEMA_SQL,
}

OUT_MODEL_TABLE_MAP: dict[type[BaseModel], str] = {
    GroupOut: "groups",
    GroupMemberOut: "group_members",
    CoreTopicOut: "core_topics",
    FeedbackOut: "feedback_events",
    ReportVersionOut: "report_versions",
    L1MessageOut: "raw_messages",
    L2ContextOut: "parsed_contexts",
    L3SummaryOut: "topic_summaries",
    RunStatusOut: "pipeline_runs",
}


class TestB3FieldToColumnAlignment:
    """B3: *Out model field names match SQLite column names."""

    @pytest.mark.parametrize(
        "model_cls, table_name",
        list(OUT_MODEL_TABLE_MAP.items()),
        ids=[f"{m.__name__}->{t}" for m, t in OUT_MODEL_TABLE_MAP.items()],
    )
    def test_fields_match_ddl_columns(self, model_cls: type[BaseModel], table_name: str):
        """Every field in *Out model exists as a column in the corresponding table."""
        ddl_source = TABLE_DDL_MAP[table_name]
        db_columns = _parse_columns(ddl_source, table_name)
        assert db_columns, f"No columns parsed for table '{table_name}'"

        model_fields = set(model_cls.model_fields.keys())
        for field_name in model_fields:
            assert field_name in db_columns, (
                f"Field '{field_name}' in {model_cls.__name__} "
                f"not found in DDL columns for table '{table_name}'. "
                f"Available columns: {sorted(db_columns)}"
            )


# ============================================================
# B4: Request models validate and reject invalid input
# ============================================================


class TestB4RequestValidation:
    """B4: Request models reject invalid input with ValidationError."""

    def test_group_create_empty_fields(self):
        """GroupCreate rejects empty display_name and chatroom_id."""
        with pytest.raises(ValidationError):
            GroupCreate(chatroom_id="", display_name="")
        with pytest.raises(ValidationError):
            GroupCreate(chatroom_id="abc", display_name="")

    def test_run_create_invalid_date(self):
        """RunCreate rejects invalid date format."""
        with pytest.raises(ValidationError):
            RunCreate(component="test", date="not-a-date")
        with pytest.raises(ValidationError):
            RunCreate(component="test", date="20260101")
        with pytest.raises(ValidationError):
            RunCreate(component="test", date="2026/06/01")

    def test_feedback_create_invalid_signal(self):
        """FeedbackCreate rejects invalid signal enum value."""
        with pytest.raises(ValidationError):
            FeedbackCreate(
                group_id="g1",
                date="2026-06-01",
                target_type="section",
                signal="invalid_signal",
            )

    def test_core_topic_create_empty_name(self):
        """CoreTopicCreate rejects empty name."""
        with pytest.raises(ValidationError):
            CoreTopicCreate(group_id="g1", name="")
        with pytest.raises(ValidationError):
            CoreTopicCreate(group_id="", name="valid")

    def test_export_request_invalid_format(self):
        """ExportRequest rejects invalid format."""
        with pytest.raises(ValidationError):
            ExportRequest(report_id="r1", format="pdf")

    def test_judge_dimension_score_out_of_range(self):
        """JudgeDimensionScore rejects score outside [0, 1]."""
        with pytest.raises(ValidationError):
            JudgeDimensionScore(dimension="quality", score=1.5)
        with pytest.raises(ValidationError):
            JudgeDimensionScore(dimension="quality", score=-0.1)


# ============================================================
# B5: __init__.py exports are complete and importable
# ============================================================


class TestB5ExportsComplete:
    """B5: __init__.py exports all public schema classes."""

    def test_star_import_works(self):
        """from z_winnow.web.schemas import * succeeds."""
        # This test uses the schema_pkg imported at module level
        all_names = schema_pkg.__all__
        assert len(all_names) > 0, "__all__ is empty"

    def test_all_entries_are_basemodel_subclasses(self):
        """Each name in __all__ is importable and is a BaseModel or Enum subclass."""
        from enum import Enum as PyEnum

        all_names = schema_pkg.__all__
        for name in all_names:
            obj = getattr(schema_pkg, name)
            assert isinstance(obj, type) and issubclass(obj, BaseModel | PyEnum), (
                f"{name} is not a BaseModel or Enum subclass"
            )

    def test_no_duplicates_in_all(self):
        """__all__ has no duplicate entries."""
        all_names = schema_pkg.__all__
        assert len(all_names) == len(set(all_names)), (
            f"Duplicate entries in __all__: {[n for n in all_names if all_names.count(n) > 1]}"
        )

    def test_all_count_matches_public_classes(self):
        """len(__all__) equals the total count of unique public classes across submodules."""
        from enum import Enum as PyEnum

        from z_winnow.web.schemas import (
            common,
            core_topics,
            data,
            export,
            feedback,
            groups,
            judge,
            key_people,
            memos,
            overview,
            reports,
            runs,
            system,
        )

        # Collect unique public classes (BaseModel + Enum) across all submodules
        # Filter out parameterized generic aliases (e.g., PaginatedResponse[GroupOut])
        # which are created dynamically when routes define response_model.
        # In Pydantic v2 these have __pydantic_generic_metadata__ attribute.
        seen_classes: set[type] = set()
        for mod in [
            common,
            core_topics,
            data,
            export,
            feedback,
            groups,
            judge,
            key_people,
            memos,
            overview,
            reports,
            runs,
            system,
        ]:
            for attr_name in dir(mod):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseModel | PyEnum)
                    and attr not in (BaseModel, PyEnum)
                    and "["
                    not in attr.__name__  # skip parameterized generics like PaginatedResponse[GroupOut]
                    and attr.__module__
                    == mod.__name__  # only classes DEFINED here (excludes imported bases like StrEnum)
                ):
                    seen_classes.add(attr)

        # __all__ should contain exactly the same number of unique public classes
        assert len(schema_pkg.__all__) == len(seen_classes), (
            f"__all__ has {len(schema_pkg.__all__)} entries but "
            f"found {len(seen_classes)} unique public classes across submodules. "
            f"Missing: {sorted(c.__name__ for c in seen_classes - {getattr(schema_pkg, n) for n in schema_pkg.__all__})}"
        )
