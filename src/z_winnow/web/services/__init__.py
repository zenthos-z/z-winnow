"""Service layer package -- async service functions wrapping database operations.

Each service module exposes stateless async functions that accept an
``aiosqlite.Connection`` as their first parameter and return Pydantic
models from ``web.schemas``.  No global state, no Settings reads.

# L070: Conditional imports -- parallel task safety.  If T-W14-1 schemas
# are not yet on disk the fallback ``_models`` shim is used instead.
"""

from __future__ import annotations

import contextlib

# L070: Conditional import with fallback
try:
    from z_winnow.web.schemas.common import PaginatedResponse as PaginatedResult
except ImportError:  # pragma: no cover -- fallback when schemas absent
    from pydantic import BaseModel, ConfigDict

    class PaginatedResult(BaseModel):  # type: ignore[no-redef]
        model_config = ConfigDict(from_attributes=True)

        items: list[object]
        total: int
        page: int
        page_size: int


# ---------------------------------------------------------------------------
# T-W14-4: Re-export all public service functions
# L070: Conditional imports to prevent one service's ImportError from
#        breaking the entire package.
# ---------------------------------------------------------------------------

# data_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.data_service import (
        find_server_id_page,
        get_l1_messages,
        get_l2_contexts_by_server_ids,
        get_l3_topics,
        trace_message_to_topics,
        trace_topic_to_messages,
    )

# feedback_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.feedback_service import (
        consume_feedback,
        get_feedback_by_id,
        list_unconsumed_feedback,
        rollback_feedback,
    )

# run_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.run_service import (
        batch_create_runs,
        cancel_run,
        list_runs,
        stream_runs,
    )

# memos_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.memos_service import (
        add_memo,
        delete_cube,
        delete_memo,
        delete_memory_by_id,
        flush_pending,
        get_all_memos,
        get_cube_detail,
        get_memory_detail,
        health_check,
        list_cubes,
        rebuild_memos_cube,
        search_memos,
        start_sync_worker,
        stop_sync_worker,
        vacuum_cube,
    )

# task_queue (P067: foundational -- import eagerly)
with contextlib.suppress(ImportError):
    from z_winnow.web.services.task_queue import (
        cancel_task,
        get_task_status,
        list_tasks,
        start_task,
    )

# judge_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.judge_service import (
        get_judge_result,
        run_judge,
    )

# export_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.export_service import (
        run_export,
        run_rl_dataset_export,
        run_rl_date_range_export,
    )

# system_service
with contextlib.suppress(ImportError):
    from z_winnow.web.services.system_service import (
        get_system_config,
        get_system_stats,
    )

# report_service (W15-P0-REPORTS)
with contextlib.suppress(ImportError):
    from z_winnow.web.services.report_service import (
        export_report,
        regenerate_report,
    )

# key_people_service (W15-P1-KEYPEOPLE)
with contextlib.suppress(ImportError):
    from z_winnow.web.services.key_people_service import (
        create_key_person,
        delete_key_person,
        update_key_person,
    )

__all__ = [
    "PaginatedResult",
    "add_memo",
    "batch_create_runs",
    "cancel_run",
    "cancel_task",
    "consume_feedback",
    "create_key_person",
    "delete_cube",
    "delete_key_person",
    "delete_memo",
    "delete_memory_by_id",
    "export_report",
    "find_server_id_page",
    "flush_pending",
    "get_all_memos",
    "get_cube_detail",
    "get_feedback_by_id",
    "get_judge_result",
    "get_l1_messages",
    "get_l2_contexts_by_server_ids",
    "get_l3_topics",
    "get_memory_detail",
    "get_system_config",
    "get_system_stats",
    "get_task_status",
    "health_check",
    "list_cubes",
    "list_runs",
    "list_tasks",
    "list_unconsumed_feedback",
    "rebuild_memos_cube",
    "regenerate_report",
    "rollback_feedback",
    "run_export",
    "run_judge",
    "run_rl_dataset_export",
    "run_rl_date_range_export",
    "search_memos",
    "start_sync_worker",
    "start_task",
    "stop_sync_worker",
    "stream_runs",
    "trace_message_to_topics",
    "trace_topic_to_messages",
    "update_key_person",
    "vacuum_cube",
]
