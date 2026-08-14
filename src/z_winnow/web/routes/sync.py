"""ECS 同步路由 — POST /api/v1/sync/push, GET /api/v1/sync/progress, GET /api/v1/sync/status

前端「ECS 服务器同步」按钮的后端：一键把本地 L3 推到 ECS 公网服务，弹窗轮询进度。

设计模式:
  - P054: Parse-validate-delegate — 路由层保持 thin，委托 sync_service
  - 错误码：400 = ECS 未配置；409 = 已有同步在跑；202 = 已开始后台推送
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from z_winnow.sync.transport import SyncConfigError
from z_winnow.web.schemas.sync import (
    SyncProgressResponse,
    SyncPushResponse,
    SyncStatusResponse,
)
from z_winnow.web.services import sync_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])


@router.post("/sync/push", response_model=SyncPushResponse, status_code=202)
async def push_to_ecs() -> SyncPushResponse:
    """一键把本地 L3 数据推送到 ECS（后台执行，立即返回任务号）。

    【ECS 服务器同步】网页上点「开始同步」即调用，等价于终端 `winnow sync push`。
    - 推送内容：L3 快照(l3_snapshot.db) + processed JSON + mcp_keys.yaml
    - 返回：202 + task_id；用 GET /sync/progress 轮询阶段进度
    - 出错：400 = ECS 未配置（缺 SSH host/key，见 .env）；409 = 已有同步在跑
    """
    try:
        r = await sync_service.start_sync()
    except sync_service.SyncInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except SyncConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return SyncPushResponse(task_id=r["task_id"], state=r["state"])


@router.get("/sync/progress", response_model=SyncProgressResponse)
async def get_progress() -> SyncProgressResponse:
    """查询当前同步进度 + 上次同步时间。

    【ECS 服务器同步】弹窗打开时、以及同步过程中每 ~1.2s 轮询一次。
    - 返回：state(idle/syncing/done/failed) + 当前阶段 + 进度百分比 + last_sync(上次成功摘要)
    - idle 时 last_sync 可能为 null（从未同步过）
    """
    p = await sync_service.get_progress()
    return SyncProgressResponse(**p)


@router.get("/sync/status", response_model=SyncStatusResponse)
async def get_status() -> SyncStatusResponse:
    """本地 vs ECS 数据行数比对 + inbox 待 pull 计数。

    【ECS 服务器同步】弹窗「详细信息」区用，等价于终端 `winnow sync status`。
    - 返回：local(本地各表行数) + ecs_l3/ecs_inbox(ECS 行数或 NOT_EXISTS) + inbox_pending_pull
    - 出错：400 = ECS 未配置
    """
    try:
        r = await sync_service.get_comparison()
    except SyncConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return SyncStatusResponse(**r)


__all__ = ["router"]
