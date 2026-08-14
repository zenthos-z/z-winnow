"""Web API schema package — Pydantic models for request/response validation.

Re-exports all public schema classes for clean usage::

    from z_winnow.web.schemas import GroupOut, PaginatedResponse

# L070: Explicit imports from each submodule (no import *) to avoid
circular imports and ensure __all__ is deterministic.
# L114: Explicit __all__ export list — do NOT iterate __subclasses__() on BaseModel
subclasses for counting. Generic parameterization creates transient subclasses.
"""

from z_winnow.web.schemas.common import (
    AsyncTaskResponse,
    ErrorResponse,
    Lifecycle,
    PaginatedResponse,
    RunStatus,
    Severity,
    SignalType,
    TaskStatusResponse,
)
from z_winnow.web.schemas.core_topics import (
    CoreTopicCreate,
    CoreTopicOut,
    CoreTopicUpdate,
)
from z_winnow.web.schemas.data import (
    DataStatsOut,
    L1MessageDetailOut,
    L1MessageOut,
    L2ContextOut,
    L3SummaryOut,
    ProvenanceChainOut,
    ProvenanceOut,
)
from z_winnow.web.schemas.export import (
    ExportRequest,
    ExportStatusOut,
    RLExportRequest,
)
from z_winnow.web.schemas.feedback import (
    FeedbackCreate,
    FeedbackOut,
    FeedbackUpdate,
)
from z_winnow.web.schemas.groups import (
    CipherTalkSessionOut,
    CipherTalkSessionsResponse,
    FeishuCatalogOut,
    FeishuInitRequest,
    FeishuTableKindOut,
    GroupCreate,
    GroupMemberOut,
    GroupOut,
    GroupUpdate,
)
from z_winnow.web.schemas.judge import (
    JudgeDimensionScore,
    JudgeRequest,
    JudgeResultOut,
)
from z_winnow.web.schemas.key_people import (
    KeyPeopleCreate,
    KeyPeopleOut,
    KeyPeopleUpdate,
    SourceMemberOut,
)
from z_winnow.web.schemas.memos import (
    CubeDeleteConfirm,
    FlushOut,
    MemCubeListOut,
    MemCubeOut,
    MemoryDetailOut,
    MemosHealthOut,
    MemosSearchItem,
    MemosSearchOut,
    RebuildRequest,
    VacuumRequest,
)
from z_winnow.web.schemas.overview import (
    OverviewGroupItem,
    OverviewStatsOut,
)
from z_winnow.web.schemas.reports import (
    CoverRequest,
    FeishuPushRequest,
    MarkdownExportRequest,
    RegenerateRequest,
    ReportContentOut,
    ReportDiffOut,
    ReportOut,
    ReportVersionOut,
)
from z_winnow.web.schemas.runs import (
    BatchRunItem,
    BatchRunRequest,
    BatchRunResponse,
    RunCreate,
    RunStatusOut,
)
from z_winnow.web.schemas.system import (
    ConfigOut,
    ConfigUpdateIn,
    ConfigUpdateOut,
    HealthCheckOut,
    LarkCliStatusOut,
    ProbeOut,
    SystemToolsOut,
)

__all__ = [
    "AsyncTaskResponse",
    "BatchRunItem",
    "BatchRunRequest",
    "BatchRunResponse",
    "CipherTalkSessionOut",
    "CipherTalkSessionsResponse",
    "ConfigOut",
    "ConfigUpdateIn",
    "ConfigUpdateOut",
    "CoreTopicCreate",
    "CoreTopicOut",
    "CoreTopicUpdate",
    "CoverRequest",
    "CubeDeleteConfirm",
    "DataStatsOut",
    "ErrorResponse",
    "ExportRequest",
    "ExportStatusOut",
    "FeedbackCreate",
    "FeedbackOut",
    "FeedbackUpdate",
    "FeishuCatalogOut",
    "FeishuInitRequest",
    "FeishuPushRequest",
    "FeishuTableKindOut",
    "FlushOut",
    "GroupCreate",
    "GroupMemberOut",
    "GroupOut",
    "GroupUpdate",
    "HealthCheckOut",
    "JudgeDimensionScore",
    "JudgeRequest",
    "JudgeResultOut",
    "KeyPeopleCreate",
    "KeyPeopleOut",
    "KeyPeopleUpdate",
    "L1MessageDetailOut",
    "L1MessageOut",
    "L2ContextOut",
    "L3SummaryOut",
    "LarkCliStatusOut",
    "Lifecycle",
    "MarkdownExportRequest",
    "MemCubeListOut",
    "MemCubeOut",
    "MemoryDetailOut",
    "MemosHealthOut",
    "MemosSearchItem",
    "MemosSearchOut",
    "OverviewGroupItem",
    "OverviewStatsOut",
    "PaginatedResponse",
    "ProbeOut",
    "ProvenanceChainOut",
    "ProvenanceOut",
    "RLExportRequest",
    "RebuildRequest",
    "RegenerateRequest",
    "ReportContentOut",
    "ReportDiffOut",
    "ReportOut",
    "ReportVersionOut",
    "RunCreate",
    "RunStatus",
    "RunStatusOut",
    "Severity",
    "SignalType",
    "SourceMemberOut",
    "SystemToolsOut",
    "TaskStatusResponse",
    "VacuumRequest",
]
