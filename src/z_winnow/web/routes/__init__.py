"""API routes package -- thin adapters delegating to service layer.

Each sub-module exposes a FastAPI ``APIRouter`` with ``prefix="/api/v1"``.
This package re-exports a single ``api_router`` that aggregates all sub-routers.

# L070: Conditional imports -- parallel tasks may not have all modules ready.
# Pattern: try/except ImportError for each sub-module; if missing, the rest
# still loads without error.
"""

from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")

# L070: Conditional import for each sub-module
try:
    from z_winnow.web.routes.health import router as _health_router

    api_router.include_router(_health_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.auth import router as _auth_router

    api_router.include_router(_auth_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.overview import router as _overview_router

    api_router.include_router(_overview_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.groups import router as _groups_router

    api_router.include_router(_groups_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.key_people import router as _key_people_router

    api_router.include_router(_key_people_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.core_topics import router as _core_topics_router

    api_router.include_router(_core_topics_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.reports import router as _reports_router

    api_router.include_router(_reports_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.runs import router as _runs_router

    api_router.include_router(_runs_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.feedback import router as _feedback_router

    api_router.include_router(_feedback_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.data import router as _data_router

    api_router.include_router(_data_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.memos import router as _memos_router

    api_router.include_router(_memos_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.judge import router as _judge_router

    api_router.include_router(_judge_router)
except ImportError:
    pass

try:
    from z_winnow.web.routes.system import router as _system_router

    api_router.include_router(_system_router)
except ImportError:
    pass

# L070: Conditional import — parallel task W15-P2-MEMOS may write rl.py simultaneously
try:
    from z_winnow.web.routes.rl import router as _rl_router

    api_router.include_router(_rl_router)
except ImportError:
    pass

# Batch generation routes (batch-v2 API)
try:
    from z_winnow.web.routes.batch import router as _batch_router

    api_router.include_router(_batch_router)
except ImportError:
    pass

# Data preview route
try:
    from z_winnow.web.routes.data_preview import router as _data_preview_router

    api_router.include_router(_data_preview_router)
except ImportError:
    pass

# ECS sync routes (一键推送 L3 到 ECS + 进度查询)
try:
    from z_winnow.web.routes.sync import router as _sync_router

    api_router.include_router(_sync_router)
except ImportError:
    pass

# Scheduler status route (定时调度只读状态：cron/下次触发/守护心跳)
try:
    from z_winnow.web.routes.scheduler import router as _scheduler_router

    api_router.include_router(_scheduler_router)
except ImportError:
    pass

__all__ = ["api_router"]
