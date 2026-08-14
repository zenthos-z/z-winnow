"""Core topics route -- GET/POST/PUT/DELETE /api/v1/core-topics.

# P054: Parse-validate-delegate. Zero business logic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from z_winnow.web.schemas.core_topics import (
    CoreTopicCreate,
    CoreTopicOut,
    CoreTopicUpdate,
)

router = APIRouter(tags=["core-topics"])


@router.get("/core-topics", response_model=list[CoreTopicOut])
async def list_core_topics(
    request: Request,
    group_id: str = Query(..., description="Group ID"),
    is_active: bool = Query(default=True),
) -> Any:
    """列出某个群设定的「核心议题」（要长期追踪的话题）。

    【核心议题】看一个群重点追踪哪些话题。

    什么时候用：在群详情/议题页查看已配置的核心议题。
    - 参数：group_id（必填，按群筛选）、is_active（是否启用）
    - 返回：核心议题列表
    """
    from z_winnow.web.services.group_service import list_core_topics as svc

    db: object = request.app.state.db_conn
    return await svc(db, group_id, is_active=is_active)


@router.post("/core-topics", response_model=CoreTopicOut, status_code=201)
async def create_core_topic(request: Request, body: CoreTopicCreate) -> Any:
    """新增一个核心议题（告诉系统这个群要持续关注某话题）。

    【核心议题】配置一个要长期追踪的议题，生成日报时会优先匹配它。

    什么时候用：想给某群添加一个长期追踪的话题。
    - 返回：201 + 新建的议题
    """
    from z_winnow.web.services.group_service import create_core_topic as svc

    db: object = request.app.state.db_conn
    return await svc(db, body)


@router.put("/core-topics/{topic_id}", response_model=CoreTopicOut)
async def update_core_topic(
    request: Request,
    topic_id: str,
    body: CoreTopicUpdate,
) -> Any:
    """修改一个核心议题。

    【核心议题】编辑已有议题的内容或状态。

    什么时候用：调整某个追踪话题。
    - 出错：404 = 该议题不存在
    """
    from z_winnow.web.services.group_service import update_core_topic as svc

    db: object = request.app.state.db_conn
    result = await svc(db, topic_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="Core topic not found")
    return result


@router.delete("/core-topics/{topic_id}", status_code=204)
async def delete_core_topic(request: Request, topic_id: str) -> None:
    """删除一个核心议题。

    【核心议题】不再追踪某个话题时移除。

    什么时候用：在议题列表里删除。
    - 返回：204 删除成功
    - 出错：404 = 该议题不存在
    """
    from z_winnow.web.services.group_service import delete_core_topic as svc

    db: object = request.app.state.db_conn
    deleted = await svc(db, topic_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Core topic not found")
