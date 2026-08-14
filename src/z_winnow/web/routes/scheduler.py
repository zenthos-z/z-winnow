"""Scheduler status route -- GET /api/v1/scheduler/status.

Read-only view of per-group scheduling state + daemon liveness, so the web UI can
show next-fire / last-run / missing-days alongside the cron input it already
persists. Delegates entirely to ``scheduler.status`` (same data layer as the CLI
dashboard) — zero duplication.

# P054: route stays thin; no business logic.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["scheduler"])


@router.get("/scheduler/status")
async def get_scheduler_status_route(request: Request) -> dict:
    """查看各群的定时调度状态（只读）。

    【定时调度】每个群：cron、是否启用、下次触发、上次运行、缺失天数；以及调度守护
    进程的心跳存活状态（last_tick）。供 Web 配置页显示「下次运行 / 调度器是否在跑」。

    什么时候用：群配置页加载时拉一次，在 cron 输入框旁展示生效中的调度信息。
    注意：本接口只读不触发——真正按 cron 触发生成的是独立的 `winnow scheduler` 守护进程。
    """
    from z_winnow.config.settings import get_settings
    from z_winnow.scheduler.status import get_scheduler_status

    settings = get_settings()
    db_path = getattr(request.app.state, "db_path", None) or settings.db_path
    status = await get_scheduler_status(
        db_path,
        lookback_days=settings.scheduler_lookback_days,
        tz=settings.scheduler_tz,
    )
    return status.to_dict()
