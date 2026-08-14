"""Overview route -- GET /api/v1/overview.

# P054: Parse-validate-delegate. Zero business logic.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from z_winnow.web.schemas.overview import OverviewStatsOut

router = APIRouter(tags=["overview"])


@router.get("/overview", response_model=OverviewStatsOut)
async def get_overview(request: Request) -> OverviewStatsOut:
    """取仪表盘首页用的总览统计数据。

    【系统总览】汇总各群组、报告、消息等关键数字，供首页大盘展示。

    什么时候用：打开 Web 首页时加载这些数字。
    - 返回：群组数、报告数、消息数等汇总统计
    """
    from z_winnow.web.services.overview_service import get_dashboard_summary

    db: object = request.app.state.db_conn
    return await get_dashboard_summary(db)  # type: ignore[arg-type]
