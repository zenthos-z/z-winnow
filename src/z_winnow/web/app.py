"""T-W14-7: FastAPI application entry point — pure API backend.

Provides the FastAPI ASGI app with:
  - API routes under ``/api/v1`` (aggregated from ``web.routes`` package)
  - Static file serving at ``/ui/`` for frontend SPA
  - Root redirect ``GET /`` -> ``/ui/``
  - API key auth + unified error handler middleware

Usage:
    uvicorn z_winnow.web.app:app --port 8100
    # or via CLI: poetry run winnow web

# A002: Zero residual HTMX imports — no web.pages.* references remain.
# P016: Lazy import pattern preserved for memory.sync_worker in lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from z_winnow.pipeline.database import init_database_in_conn

logger = logging.getLogger(__name__)

# ============================================================
# Configuration — T-W12-5: S7 配置单源 via Settings
# A013: No module-level os.getenv() calls. Settings read at function level.
# ============================================================


def _get_db_path() -> str:
    """Resolve SQLite database path via Settings, creating parent dir if needed.

    T-W12-5: S7 配置单源 — reads from Settings instead of module-level os.getenv().
    A013: Called at function level (inside lifespan), not module level.
    """
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    db_path = Path(settings.sqlite_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


# ============================================================
# Application lifespan — DB connection + schema init
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Manage FastAPI app lifecycle: init DB schema + memos sync worker on startup.

    Creates the database file and schema if they don't exist.
    Stores the connection on app.state for route handlers.

    T-W10-E-c P016: Single-point integration — memos sync worker started
    via asyncio.create_task with stop_event for graceful shutdown.
    Lazy import: memory module not imported at module level.
    """
    db_path = _get_db_path()
    # T-W12-5: S7 配置单源 — read from Settings
    from z_winnow.config.settings import get_settings

    settings = get_settings()

    # Initialize LangSmith tracing BEFORE any LangChain/LangGraph imports.
    # LangGraph auto-tracing checks LANGCHAIN_TRACING_V2 at import time;
    # if init_langsmith() is deferred to orchestrator, the env vars won't be
    # set yet and tracing is silently disabled for the lifetime of the process.
    from z_winnow.observability.langsmith_setup import init_langsmith

    init_langsmith()

    logger.info("Initializing database at %s", db_path)

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await init_database_in_conn(conn)

    app.state.db_conn = conn
    app.state.db_path = db_path
    app.state.reports_dir = settings.reports_dir

    # T-W10-E-c P016: Start memos sync worker via asyncio.create_task
    stop_event: asyncio.Event = asyncio.Event()
    app.state.memos_stop_event = stop_event

    # P016: Lazy import — memory module loaded only when lifespan runs
    from z_winnow.memory.sync_worker import start_worker

    worker_task: asyncio.Task[None] = asyncio.create_task(
        start_worker(stop_event=stop_event, db_path=db_path)
    )
    app.state.memos_worker_task = worker_task

    # P016: Create MemOS adapter for route handlers (search, health check)
    from z_winnow.memory.factory import create_memos_adapter

    app.state.memos_adapter = create_memos_adapter()

    # CipherTalk data client for the「新建群」session picker (GET /groups/sessions)
    from z_winnow.pipeline.cipher_talk_client import create_data_client

    app.state.cipher_talk_client = create_data_client()

    logger.info("Web dashboard ready on port %d (memos sync worker started)", settings.web_port)
    yield

    # T-W10-E-c: Graceful shutdown — signal worker to stop
    logger.info("Shutting down memos sync worker...")
    from z_winnow.memory.sync_worker import stop_worker

    await stop_worker(stop_event, timeout_s=10.0)
    logger.info("memos sync worker stopped.")

    ct_client = getattr(app.state, "cipher_talk_client", None)
    if ct_client is not None:
        await ct_client.close()
        logger.info("CipherTalk client closed.")

    await conn.close()
    logger.info("Database connection closed.")


# ============================================================
# FastAPI application — API backend + static SPA serving
# ============================================================

# 面向用户的接口分组说明（按工作流顺序排列），展示在 /docs 每个 tag 标题下方。
# tag 名必须与各路由 APIRouter(tags=[...]) 一致；这里只补充中文用途说明。
API_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "【系统总览】探活：确认后端服务是否活着、数据库是否连得上。"},
    {"name": "overview", "description": "【系统总览】首页大盘：群组数、报告数、消息数等汇总统计。"},
    {"name": "system", "description": "【系统总览】系统运行信息与配置（已脱敏，不含密钥）。"},
    {"name": "groups", "description": "【群组配置】注册和管理要分析的微信群——生成日报前必须先在这里登记群。"},
    {"name": "core-topics", "description": "【核心议题】管理每个群要长期追踪的话题，生成日报时会优先匹配。"},
    {"name": "key-people", "description": "【关键人物】标记群里的重要成员，并统计他们的发言情况。"},
    {"name": "runs", "description": "【数据抓取】发起和查看日报生成任务（即跑流水线：抓消息→解析→AI生成→落库）。"},
    {"name": "data", "description": "【原始数据】浏览抓下来的三层原始数据（L1原文/L2上下文/L3总结），以及按消息溯源。"},
    {"name": "reports", "description": "【报告产出】查看/导出/重新生成/推送飞书 日报。"},
    {"name": "memos", "description": "【长期记忆】MemOS 记忆的运维：健康检查、语义搜索、重建、清理、补刷同步队列。"},
    {"name": "judge", "description": "【质量评估】用 AI 给已生成的报告从 4 个维度打分。"},
    {"name": "feedback", "description": "【用户反馈】记录和处理用户对报告的意见（点赞/点踩/纠错）。"},
    {"name": "rl", "description": "【训练数据】把历史报告导出成强化学习训练用的数据集。"},
    {"name": "scheduler", "description": "【定时调度】查看各群定时日报的调度状态（cron/下次触发/守护心跳），只读。"},
]

app = FastAPI(
    title="winnow 群日报 API",
    description=(
        "微信群聊日报系统：从 CipherTalk 抓取群聊消息，用 AI 生成结构化日报，"
        "包含议题追踪、资源提取、工程问题分析和长期记忆。\n\n"
        "**整体流程（按顺序）：**\n"
        "1. **配置群**（groups / core-topics / key-people）—— 先登记要分析的群、设好核心议题和关键人物；\n"
        "2. **发起生成**（runs）—— 跑流水线：抓消息 → 解析增强 → AI 生成日报/资源/工程问题/议题 → 落库；\n"
        "3. **看原始数据**（data）—— 浏览三层中间数据、按消息 serverId 溯源；\n"
        "4. **看报告**（reports）—— 查看 / 导出 / 重跑 / 推送飞书；\n"
        "5. **评估与反馈**（judge / feedback）—— 给报告打分、收集用户意见；\n"
        "6. **记忆运维**（memos）—— 长期记忆的健康、搜索、重建、清理。\n\n"
        "**关于异步任务：** 标注 `202` 的接口（生成日报、重建记忆、评估、导出等）都是提交后在后台跑，"
        "立即返回一个 `task_id`；用对应的 `GET .../{task_id}` 状态接口轮询结果。\n\n"
        "**关于这个页面：** 这是 Swagger 自动生成的接口文档，下方按业务板块分组列出全部接口，"
        "点开任意接口可见「这个接口干什么、什么时候用、返回/出错说明」。前端控制台在 `/ui/`。"
    ),
    version="0.2.0",
    lifespan=lifespan,
    openapi_tags=API_TAGS,
)

# --- Middleware (outermost first = added last in FastAPI) ---
# P074: Centralized middleware from web.middleware package (T-W14-2)
# L070: Conditional import — middleware submodules may not exist in parallel builds
try:
    from z_winnow.web.middleware import ErrorHandlerMiddleware

    if ErrorHandlerMiddleware is not None:
        app.add_middleware(ErrorHandlerMiddleware)
except ImportError:
    pass

try:
    from z_winnow.web.middleware import ApiKeyMiddleware

    if ApiKeyMiddleware is not None:
        app.add_middleware(ApiKeyMiddleware)
except ImportError:
    pass

# --- API routes (T-W14-6): prefix=/api/v1 ---
# P074: Centralized APIRouter aggregation from route modules
from z_winnow.web.routes import api_router  # noqa: E402

app.include_router(api_router)

# --- Swagger 列表标题注入 ---
# FastAPI 默认只把 docstring 当作 description（展开后的正文），summary（列表里那行标题）
# 会回退成函数名的英文形式（如 "List Reports"）。这里统一把每个处理函数 docstring 的第一行
# 注入为 summary，使 /docs 列表里每个接口都显示一句中文功能说明。
import inspect  # noqa: E402

from fastapi.routing import APIRoute  # noqa: E402

for _route in app.routes:
    if isinstance(_route, APIRoute):
        _doc = inspect.getdoc(_route.endpoint)
        if _doc:
            _route.summary = _doc.splitlines()[0]

# --- Static SPA frontend ---
# Spec: StaticFiles mount at /ui, resolved relative to web/ package
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/ui", StaticFiles(directory=str(_static_dir), html=True), name="ui")


# --- Root redirect ---
@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Redirect root to SPA frontend."""
    return RedirectResponse(url="/ui/")


# --- Attachment file serving (local cache for resource images) ---
# Images downloaded from WeFlow during content_enrich are cached in
# data/processed/{group_id}/{date}/attachments/.  This route serves them
# via HTTP so resource links in reports point to a real, reachable URL.
@app.get("/api/v1/attachments/{group_id}/{date}/{filename}")
async def serve_attachment(group_id: str, date: str, filename: str):
    """Serve a locally cached attachment file.

    Path parameters are validated against traversal attacks (``..``, ``/``).
    Returns 404 if the file does not exist.
    """
    # Path traversal guard
    if ".." in group_id or "/" in group_id or ".." in date or "/" in date:
        raise HTTPException(status_code=400, detail="Invalid path segment")
    safe_name = os.path.basename(filename)
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    from z_winnow.config.settings import get_settings

    file_path = (
        Path(get_settings().layer3_output_dir)
        / group_id
        / date
        / "attachments"
        / safe_name
    )
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(str(file_path))
