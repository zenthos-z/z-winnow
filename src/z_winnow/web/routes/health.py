"""Health check route -- GET /api/v1/health.

# P054: Parse-validate-delegate. Zero business logic.
# L026: New file only -- does not modify existing web/pages/.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from z_winnow.web.schemas.system import HealthCheckOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthCheckOut)
async def health_check(request: Request) -> HealthCheckOut:
    """检查系统是否正常运行（健康检查）。

    【系统总览】最简单的探活接口，确认服务活着、数据库连得上。

    什么时候用：监控/定时探活，或前端启动时确认后端可用。
    - 返回：status=ok、版本号、数据库连接状态
    """
    from z_winnow.web.services.system_service import get_system_config

    config = await get_system_config()
    db_status = "ok" if config.get("db_path") else "unknown"
    return HealthCheckOut(
        status="ok",
        version="0.1.0",
        database=db_status,
    )
