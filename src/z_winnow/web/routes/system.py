"""System route -- GET /api/v1/system/info, GET /api/v1/system/config.

# P054: Parse-validate-delegate. Zero business logic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from z_winnow.web.schemas.system import (
    ConfigOut,
    ConfigUpdateIn,
    ConfigUpdateOut,
    HealthCheckOut,
    LarkCliStatusOut,
    ProbeOut,
    SystemToolsOut,
)

router = APIRouter(tags=["system"])


@router.get("/system/info", response_model=HealthCheckOut)
async def system_info(request: Request) -> HealthCheckOut:
    """取系统运行信息（版本 + 数据库状态）。

    【系统总览】和 /health 类似，附带系统运行信息。

    什么时候用：在「系统」页查看运行状态。
    - 返回：状态、版本、数据库连接
    """
    from z_winnow.web.services.system_service import get_system_config

    config = await get_system_config()
    db_status = "ok" if config.get("db_path") else "unknown"
    return HealthCheckOut(
        status="ok",
        version="0.1.0",
        database=db_status,
    )


@router.get("/system/config", response_model=ConfigOut)
async def system_config(request: Request) -> ConfigOut:
    """取当前系统配置（已脱敏，不含任何密钥）。

    【系统总览】查看数据库路径、端口、默认模型等运行配置。

    什么时候用：排查问题时确认实际生效的配置。
    - 返回：db_path、web_port、默认模型、日志级别等（密钥一律不返回）
    """
    from z_winnow.web.services.system_service import get_system_config

    config = await get_system_config()
    return ConfigOut(
        db_path=config.get("db_path"),
        web_port=config.get("web_port"),
        default_model=config.get("anthropic_model"),
        log_level=config.get("log_level"),
        features=config,
    )


@router.put("/system/config", response_model=ConfigUpdateOut)
async def update_system_config(request: Request, body: ConfigUpdateIn) -> Any:
    """初始化引导页「保存并重启」——校验并持久化配置，可选触发进程重启。

    【初始化引导】把向导填写的配置写入 data/config_overrides.json（最高优先级，
    压过环境变量），可选把 memos-api 基建变量写入 .env；校验失败不落盘。
    什么时候用：在初始化向导点「保存并重启」。
    - 入参：values（Settings 字段→值）、infra（compose .env 变量）、restart
    - 返回：applied_fields / infra_written / restart / warnings（不回显密钥）
    """
    from z_winnow.web import runtime
    from z_winnow.web.services import config_service

    try:
        result = config_service.apply_config_update(body.values, body.infra)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    restart = body.restart and runtime.request_restart()
    warnings = list(result.get("warnings", []))
    # 写了 infra（Qdrant/Redis/Redis-pw）时，触发脚本重建 MemOS 容器使 .env 完全生效。
    if result.get("infra_written"):
        if restart:
            spawned = config_service.trigger_infra_restart()
            warnings.append(
                "已触发 MemOS 容器重建脚本（detached，约 10-30s，日志 data/restart-deps.log）"
                if spawned
                else "未找到 deployments/restart-deps.sh：请手动 docker compose up -d --force-recreate"
            )
        else:
            warnings.append("infra 已写入 .env，需手动 `docker compose up -d --force-recreate qdrant redis memos-api`")
    out = ConfigUpdateOut(
        applied_fields=result.get("applied_fields", []),
        infra_written=result.get("infra_written", []),
        restart=restart,
        warnings=warnings,
    )
    if restart:
        # BackgroundTask 在响应发出后才翻转 server.should_exit → 客户端先收到 202。
        return JSONResponse(out.model_dump(), background=BackgroundTask(runtime.trigger_shutdown))
    return out


@router.post("/system/config/test", response_model=ProbeOut)
async def test_system_config(body: ConfigUpdateIn) -> ProbeOut:
    """用候选值探测 LLM / CipherTalk / MemOS 连通性（不落盘、不重启）。

    【初始化引导】点「测试连接」时用，确认填入的 key/端点可用再保存。
    """
    from z_winnow.web.services import config_service

    res = await config_service.probe_connectivity(body.values, body.targets)
    return ProbeOut(**res)


@router.get("/system/tools", response_model=SystemToolsOut)
async def system_tools() -> SystemToolsOut:
    """外部工具就绪检测（#8）——当前只有 lark-cli。

    【初始化引导 / 群配置·飞书】推飞书多维表格依赖 lark-cli（用户身份 + base/drive 权限）。
    什么时候用：初始化向导的「飞书工具链」步骤、群配置页飞书区的就绪徽标。
    - 返回：lark_cli {installed, path, version, authed, user_name, base_drive_ok, note}
    - note 在未就绪时给具体命令（构建/auth login）。永不抛错（探测失败也返回 note）。
    """
    from z_winnow.web.services.system_service import check_lark_cli

    status = await check_lark_cli()
    return SystemToolsOut(lark_cli=LarkCliStatusOut(**status))
