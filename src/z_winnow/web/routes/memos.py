"""Memos route -- 11 endpoints covering health, search, cube CRUD,
rebuild, vacuum, memory detail/delete, and flush.

# P054: Parse-validate-delegate. Zero business logic in route layer.
# P067: POST rebuild/vacuum/flush use task_queue for background execution.
# P082: Read paths propagate errors; write paths degrade gracefully.
# P079: DELETE /memos/cubes/{id} requires body {confirm: true}.
# A008: Response variables initialized before try blocks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from z_winnow.web.schemas.common import AsyncTaskResponse
from z_winnow.web.schemas.memos import (
    CubeDeleteConfirm,
    MemCubeOut,
    MemoryDetailOut,
    MemosHealthOut,
    MemosSearchOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memos"])


class MemosSearchRequest(BaseModel):
    """Request body for memos search."""

    query: str = Field(min_length=1, description="Search query")
    cube: str = Field(default="default", description="MemCube ID")
    top_k: int = Field(default=10, ge=1, le=50)


class MemosWipeConfirm(BaseModel):
    """Strong confirm body for full memory-store wipe (dev/debug only).

    Requires the exact string ``WIPE_ALL_MEMORIES`` (not just ``true``) to
    guard against accidental triggers.
    """

    confirm: str = Field(description="必须精确填 'WIPE_ALL_MEMORIES'")

    @field_validator("confirm")
    @classmethod
    def _must_be_wipe_token(cls, v: str) -> str:
        if v != "WIPE_ALL_MEMORIES":
            raise ValueError("confirm 必须精确为 'WIPE_ALL_MEMORIES'")
        return v


# ============================================================
# Existing endpoints (preserved)
# ============================================================


@router.get("/memos/status", response_model=MemosHealthOut)
async def memos_status(request: Request) -> MemosHealthOut:
    """检查长期记忆服务（MemOS）是否正常。

    【长期记忆】看 MemOS 后端连没连上、健康不健康。

    什么时候用：排查"记忆功能不工作"时，先看这里。
    - 返回：status、connected（连不上时 status=unavailable/error）
    """
    from z_winnow.web.services.memos_service import health_check

    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        return MemosHealthOut(status="unavailable", connected=False)
    try:
        result = await health_check(adapter)
        status = result.get("status", "unknown")
        # adapter.health_check returns status/latency/search_status but NOT connected/url;
        # derive them: connected ⟺ healthy, url ⟺ adapter's configured base_url.
        return MemosHealthOut(
            status=status,
            connected=(status == "ok"),
            url=getattr(adapter, "base_url", None),
        )
    except Exception:
        logger.warning("memos health check failed", exc_info=True)
        return MemosHealthOut(status="error", connected=False)


@router.post("/memos/search", response_model=MemosSearchOut)
async def memos_search(request: Request, body: MemosSearchRequest) -> Any:
    """在长期记忆里做语义搜索（用自然语言找相关记忆）。

    【长期记忆】给一句话，从 MemOS 记忆库里召回语义相关的记忆。

    什么时候用：想知道"系统以前记过关于 X 的什么"。
    - 入参：query（搜索词）、cube（在哪个记忆库搜）、top_k（返回几条）
    - 返回：匹配的记忆列表（连不上时返回空结果）
    """
    from z_winnow.web.services.memos_service import search_memos

    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        return MemosSearchOut(query=body.query, total=0, results=[])
    try:
        results = await search_memos(adapter, cube=body.cube, query=body.query, top_k=body.top_k)
        return MemosSearchOut(query=body.query, total=len(results), results=results)
    except Exception:
        logger.warning("memos search failed", exc_info=True)
        return MemosSearchOut(query=body.query, total=0, results=[])


# ============================================================
# W15-P2-MEMOS: 8 new endpoints
# ============================================================


# ---------------------------------------------------------------------------
# GET /memos/cubes — list cubes for a group
# ---------------------------------------------------------------------------


@router.get("/memos/cubes", response_model=list[MemCubeOut])
async def list_memos_cubes(
    request: Request,
    group: str = Query(..., min_length=1, description="Group ID to list cubes for"),
) -> list[MemCubeOut]:
    """列出某个群的所有记忆库（cube）。

    【长期记忆】一个群可能有多个记忆库（如议题库、反馈库），这里列出它们。

    什么时候用：在「记忆」页查看某群有哪些记忆库、各存了多少。
    - 参数：group（必填，群 ID）
    - 出错：502 = MemOS 后端连不上
    """
    from z_winnow.web.services.memos_service import list_cubes

    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        return []

    try:
        cubes_data = await list_cubes(adapter, group_id=group)
    except Exception:
        logger.exception("list_memos_cubes: failed for group=%s", group)
        raise HTTPException(status_code=502, detail="MemOS backend unreachable") from None

    # Map to MemCubeOut — P054: route only transforms service output
    return [
        MemCubeOut(
            cube_id=c.get("cube_id", ""),
            group_id=c.get("group_id", group),
            date=c.get("date", ""),
            summary=None,
            message_count=c.get("memory_count", 0),
            status=c.get("status", "pending"),
            created_at=c.get("created_at"),
        )
        for c in cubes_data
    ]


# ---------------------------------------------------------------------------
# GET /memos/cubes/{cube_id} — get cube detail
# ---------------------------------------------------------------------------


@router.get("/memos/cubes/{cube_id}", response_model=MemCubeOut)
async def get_memos_cube(
    request: Request,
    cube_id: str,
) -> MemCubeOut:
    """查看某个记忆库（cube）的详情。

    【长期记忆】看一个记忆库的基本信息：存了多少条、状态、创建时间。

    什么时候用：点开列表里某个记忆库看详情。
    - 出错：404 = 记忆库不存在；502/503 = MemOS 不可用
    """
    from z_winnow.web.services.memos_service import get_cube_detail

    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="MemOS adapter not available")

    cube_detail: dict[str, Any] | None = None
    try:
        cube_detail = await get_cube_detail(adapter, cube_id=cube_id)
    except Exception:
        logger.exception("get_memos_cube: failed for cube_id=%s", cube_id)
        raise HTTPException(status_code=502, detail="MemOS backend unreachable") from None

    if cube_detail is None:
        raise HTTPException(status_code=404, detail="Cube not found")

    return MemCubeOut(
        cube_id=cube_detail.get("cube_id", cube_id),
        group_id=cube_detail.get("group_id", ""),
        date=cube_detail.get("date", ""),
        summary=None,
        message_count=cube_detail.get("memory_count", 0),
        status=cube_detail.get("status", "pending"),
        created_at=cube_detail.get("created_at"),
    )


# ---------------------------------------------------------------------------
# DELETE /memos/cubes/{cube_id} — delete cube (confirm gate)
# ---------------------------------------------------------------------------


@router.delete("/memos/cubes/{cube_id}", status_code=204)
async def delete_memos_cube(
    request: Request,
    cube_id: str,
    body: CubeDeleteConfirm,
) -> None:
    """删除整个记忆库（cube）。必须在请求体里带 {confirm: true} 二次确认。

    【长期记忆】清空一个群的某个记忆库的所有记忆，不可恢复，所以要求显式确认。

    什么时候用：记忆库数据出错、要彻底重建时。
    - 入参：body.confirm 必须为 true（防误删；不带或为 false 返回 422）
    - 返回：204 删除成功
    - 出错：502/503 = MemOS 不可用或删除失败
    """
    from z_winnow.web.services.memos_service import delete_cube

    # P079: Confirm gate already validated by Pydantic (CubeDeleteConfirm)
    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="MemOS adapter not available")

    try:
        success = await delete_cube(adapter, cube_id=cube_id)
    except Exception:
        logger.exception("delete_memos_cube: failed for cube_id=%s", cube_id)
        raise HTTPException(status_code=502, detail="MemOS backend unreachable") from None

    if not success:
        raise HTTPException(status_code=502, detail="Cube deletion failed — degraded")

    # 204 No Content — FastAPI returns empty response


# ---------------------------------------------------------------------------
# POST /memos/cubes/{cube_id}/rebuild — rebuild from SQLite (async 202)
# ---------------------------------------------------------------------------


@router.post("/memos/cubes/{cube_id}/rebuild", response_model=AsyncTaskResponse, status_code=202)
async def rebuild_memos_cube(
    request: Request,
    cube_id: str,
    group: str = Query(..., min_length=1, description="Group ID for SQLite data"),
) -> AsyncTaskResponse:
    """从 SQLite 重建某个记忆库（后台执行，立即返回任务号）。

    【长期记忆】用本地数据库里的数据，把记忆库从头重建一遍（用于记忆损坏/补全）。

    什么时候用：记忆库数据丢了或脏了，想按数据库重灌。
    - 参数：cube_id（路径）、group（必填，数据来源群）
    - 返回：202 + 任务号
    """
    from z_winnow.web.services.task_queue import start_task

    db_path: str = getattr(request.app.state, "db_path", "")
    adapter_ref = getattr(request.app.state, "memos_adapter", None)

    async def _rebuild_coro() -> dict[str, Any]:
        # P054: rebuild logic lives in service, not route
        from z_winnow.web.services.memos_service import rebuild_memos_cube

        adapter = adapter_ref
        if adapter is None:
            from z_winnow.memory.factory import create_memos_adapter

            adapter = create_memos_adapter()
        return await rebuild_memos_cube(
            adapter=adapter,
            cube_id=cube_id,
            group_id=group,
            db_path=db_path,
        )

    task_id = await start_task(
        task_type="memos_rebuild",
        resource_id=cube_id,
        coro_factory=_rebuild_coro,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/task/{task_id}",
    )


# ---------------------------------------------------------------------------
# POST /memos/cubes/{cube_id}/vacuum — vacuum lifecycle (async 202)
# ---------------------------------------------------------------------------


@router.post("/memos/cubes/{cube_id}/vacuum", response_model=AsyncTaskResponse, status_code=202)
async def vacuum_memos_cube(
    request: Request,
    cube_id: str,
    group: str = Query(..., min_length=1, description="Group ID for vacuum scope"),
) -> AsyncTaskResponse:
    """对记忆库做生命周期清理（后台执行，立即返回任务号）。

    【长期记忆】扫描记忆，把低置信度的归档、过期的删除，给记忆库"瘦身"。

    什么时候用：记忆太多、想清理掉陈旧/低价值的记忆。
    - 参数：cube_id（路径）、group（必填）
    - 返回：202 + 任务号
    """
    from z_winnow.web.services.task_queue import start_task

    adapter_ref = getattr(request.app.state, "memos_adapter", None)

    async def _vacuum_coro() -> dict[str, Any]:
        from z_winnow.web.services.memos_service import vacuum_cube

        adapter = adapter_ref
        if adapter is None:
            from z_winnow.memory.factory import create_memos_adapter

            adapter = create_memos_adapter()
        return await vacuum_cube(
            adapter=adapter,
            cube_id=cube_id,
            group_id=group,
        )

    task_id = await start_task(
        task_type="memos_vacuum",
        resource_id=cube_id,
        coro_factory=_vacuum_coro,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/task/{task_id}",
    )


# ---------------------------------------------------------------------------
# GET /memos/memory/{memory_id} — get memory detail
# ---------------------------------------------------------------------------


@router.get("/memos/memory/{memory_id}", response_model=MemoryDetailOut)
async def get_memory_detail(
    request: Request,
    memory_id: str,
    cube: str = Query(default="default", description="Cube to search in"),
) -> MemoryDetailOut:
    """查看单条记忆的详情。

    【长期记忆】点开某条记忆，看它的完整内容、来源、元数据。

    什么时候用：在记忆列表里点某条看详情。
    - 参数：memory_id（路径）、cube（在哪个记忆库找，默认 default）
    - 出错：404 = 记忆不存在；502/503 = MemOS 不可用
    """
    from z_winnow.web.services.memos_service import (
        get_memory_detail as svc_get_memory_detail,
    )

    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="MemOS adapter not available")

    memory: dict[str, Any] | None = None
    try:
        memory = await svc_get_memory_detail(adapter, memory_id=memory_id, cube_id=cube)
    except Exception:
        logger.exception("get_memory_detail: failed for memory_id=%s", memory_id)
        raise HTTPException(status_code=502, detail="MemOS backend unreachable") from None

    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    meta = memory.get("metadata", {})
    # Handle both dict and dataclass metadata
    if hasattr(meta, "source"):
        source = getattr(meta, "source", None)
    elif isinstance(meta, dict):
        source = meta.get("source")
    else:
        source = None

    # Normalize created_at to string (mock adapter uses float timestamp)
    raw_created = memory.get("created_at")
    created_at = str(raw_created) if isinstance(raw_created, int | float) else raw_created

    return MemoryDetailOut(
        memory_id=memory_id,
        group_id=memory.get("group_id", ""),
        date=memory.get("date", ""),
        content=memory.get("content", ""),
        source=str(source) if source else None,
        metadata_json=str(memory.get("metadata", {})),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# DELETE /memos/memory/{memory_id} — delete specific memory
# ---------------------------------------------------------------------------


@router.delete("/memos/memory/{memory_id}", status_code=204)
async def delete_memory_by_id(
    request: Request,
    memory_id: str,
    cube: str = Query(default="default", description="Cube to delete from"),
) -> None:
    """删除单条记忆。

    【长期记忆】删掉某条不要的记忆（不像删整个库那样要二次确认）。

    什么时候用：发现某条记忆有误，单独删它。
    - 参数：memory_id（路径）、cube（默认 default）
    - 返回：204 删除成功
    - 出错：404 = 记忆不存在或删除失败；502/503 = MemOS 不可用
    """
    from z_winnow.web.services.memos_service import (
        delete_memory_by_id as svc_delete_memory,
    )

    # A008
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="MemOS adapter not available")

    try:
        success = await svc_delete_memory(adapter, memory_id=memory_id, cube_id=cube)
    except Exception:
        logger.exception("delete_memory_by_id: failed for memory_id=%s", memory_id)
        raise HTTPException(status_code=502, detail="MemOS backend unreachable") from None

    if not success:
        raise HTTPException(status_code=404, detail="Memory not found or deletion failed")

    # 204 No Content


# ---------------------------------------------------------------------------
# POST /memos/flush — flush sync queue (async 202)
# ---------------------------------------------------------------------------


@router.post("/memos/flush", response_model=AsyncTaskResponse, status_code=202)
async def flush_memos_queue(
    request: Request,
) -> AsyncTaskResponse:
    """把积压的同步任务一次性冲刷到 MemOS（后台执行，立即返回任务号）。

    【长期记忆】记忆同步是异步排队处理的；积压太多时用这个接口强制全部处理完。

    什么时候用：同步队列积压严重、想立即清空 pending 任务。
    - 返回：202 + 任务号
    """
    from z_winnow.web.services.task_queue import start_task

    db_path: str = getattr(request.app.state, "db_path", "")

    async def _flush_coro() -> dict[str, Any]:
        from z_winnow.web.services.memos_service import flush_pending

        return await flush_pending(db_path=db_path)

    task_id = await start_task(
        task_type="memos_flush",
        resource_id="sync_queue",
        coro_factory=_flush_coro,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/task/{task_id}",
    )


# ---------------------------------------------------------------------------
# DELETE /memos — wipe ALL groups' memories (dev/debug, strong confirm)
# ---------------------------------------------------------------------------


@router.delete("/memos", status_code=200)
async def wipe_all_memos(request: Request, body: MemosWipeConfirm) -> dict[str, Any]:
    """全量清空所有群的 MemOS 记忆（开发调试用，不可逆）。

    【长期记忆】清空所有已注册群的全部 cube 记忆。仅开发调试用：本地要彻底
    还原 MemOS 到空状态时。生产环境请用删群（DELETE /groups/{id}）级联清理。

    什么时候用：开发调试一键清空所有长期记忆。
    - 入参：body.confirm 必须精确为 "WIPE_ALL_MEMORIES"（错串 → 422）
    - 返回：200 + 摘要 {groups, cubes, total_removed, all_ok}
    - 出错：503 = MemOS 不可用；502 = 清空失败
    """
    adapter = getattr(request.app.state, "memos_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="MemOS adapter not available")

    from z_winnow.web.services.group_service import list_group_ids
    from z_winnow.web.services.memos_service import wipe_all_memories

    group_ids = await list_group_ids(request.app.state.db_conn)

    try:
        return await wipe_all_memories(adapter, group_ids)
    except Exception:
        logger.exception("wipe_all_memos: failed")
        raise HTTPException(status_code=502, detail="MemOS wipe failed") from None
