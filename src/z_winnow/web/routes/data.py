"""Data route -- GET /api/v1/data/ endpoints.

# P054: Parse-validate-delegate. Zero business logic.
# P032: Multi-layer data explorer -- L1/L2/L3 query pattern.

W15-P1-DATA: Added /data/stats, /data/provenance/{server_id},
/data/l1/{group_id}/{date}/detail/{server_id}.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from z_winnow.web.schemas.data import (
    DataStatsOut,
    L1MessageDetailOut,
    ProvenanceChainOut,
)

router = APIRouter(tags=["data"])


@router.get("/data/{layer}/{group_id}/{date}")
async def get_data(
    request: Request,
    layer: str,
    group_id: str,
    date: str,
) -> Any:
    """按数据层浏览某群某天的原始数据（L1/L2/L3 三层任选）。

    【原始数据】抓取之后、报告之前的数据分三层存放，这里查看任一层内容。

    - L1 = 原始消息（CipherTalk 拉来的原文）
    - L2 = 解析后的上下文块（切分、增强过）
    - L3 = 报告总结（议题摘要等）
    - 参数：layer（l1/l2/l3）、group_id、date
    - 出错：400 = layer 不是 l1/l2/l3
    """
    from z_winnow.web.services.data_service import (
        get_l1_messages,
        get_l2_contexts,
        get_l3_topics,
    )

    db: object = request.app.state.db_conn

    if layer == "l1":
        result = await get_l1_messages(db, date, group_id=group_id)
        return result
    elif layer == "l2":
        return await get_l2_contexts(db, date, group_id=group_id)
    elif layer == "l3":
        topics = await get_l3_topics(db, date, group_id=group_id)
        return {"items": topics, "total": len(topics), "layer": "l3"}
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid layer '{layer}'. Must be l1, l2, or l3.",
        )


# ============================================================
# W15-P1-DATA: Cross-layer stats + provenance + L1 detail
# P054: Parse-validate-delegate — zero SQL in route layer.
# ============================================================


@router.get("/data/stats", response_model=DataStatsOut)
async def get_data_stats(
    request: Request,
    group_id: str | None = Query(None, description="Optional group filter"),
    date: str | None = Query(None, description="Optional date filter YYYYMMDD"),
) -> Any:
    """取三层原始数据的汇总统计（各层有多少条）。

    【原始数据】看 L1/L2/L3 各存了多少数据，了解数据规模。

    什么时候用：在「数据浏览」页展示统计概览。
    - 筛选：group_id、date（可选）
    - 返回：各层的记录数等统计
    """
    from z_winnow.web.services.data_service import get_data_stats as svc

    db: object = request.app.state.db_conn
    return await svc(db, group_id=group_id, date=date)


@router.get("/data/provenance/{server_id}", response_model=ProvenanceChainOut)
async def get_data_provenance(
    request: Request,
    server_id: str,
) -> Any:
    """按微信 serverId 正向溯源：一条原始消息 → 它被哪些议题引用。

    【原始数据】给一条消息的 serverId，查出这条消息本身、所在的上下文块、以及最终进了哪些议题总结。

    什么时候用：想确认"某条消息最终影响了报告的哪些部分"。
    - 出错：404 = 该 serverId 找不到对应消息
    """
    from z_winnow.web.services.data_service import get_provenance_chain

    db: object = request.app.state.db_conn
    result = await get_provenance_chain(db, server_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Message not found for server_id={server_id}",
        )
    return result


@router.get("/data/l1/{group_id}/{date}/detail/{server_id}", response_model=L1MessageDetailOut)
async def get_l1_detail(
    request: Request,
    group_id: str,
    date: str,
    server_id: str,
) -> Any:
    """查看某条原始消息（L1）的完整详情，连带它关联的上下文块和议题。

    【原始数据】点开某条原始消息时，看它的全部内容 + 关联的 L2/L3。

    什么时候用：在数据浏览里点某条消息看详情。
    - 出错：404 = 该群/该天/该 serverId 的消息不存在
    """
    from z_winnow.web.services.data_service import get_l1_message_detail

    db: object = request.app.state.db_conn
    result = await get_l1_message_detail(db, group_id, date, server_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Message not found for server_id={server_id} in group={group_id} date={date}",
        )
    return result
