"""ECS 同步服务层：一键 push + 进度查询 + 本地/ECS 比对。

把 CLI 的 ``winnow sync push|status`` 包成 Web 可调用的后台任务，并暴露
阶段级进度给前端轮询。

设计：
- ``_LIVE`` 单槽内存态（最近一次任务），记录 syncing / done / failed + 当前阶段。
  单进程开发工具，progress_cb 在事件循环里同步 mutate，无需锁（同 batch scheduler
  持有运行态的模式）。
- 后台执行与持久化复用 :mod:`task_queue`（``task_type='sync_push'``）—— 不新建表。
  ``get_last_sync`` 查最近一条 ``done`` 记录拿「上次同步时间 + 摘要」，跨重启有效。
- 终态（done/failed）在内存里只保留 ``_TERMINAL_TTL_S`` 秒，过期回落 idle，
  避免前端重开弹窗时卡在旧的「完成」屏。真正的历史走 ``last_sync``（DB）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from z_winnow.sync import push as sync_push
from z_winnow.sync import status as sync_status
from z_winnow.sync.transport import check_config
from z_winnow.web.schemas.sync import SyncLastSync
from z_winnow.web.services.task_queue import list_tasks, start_task

logger = logging.getLogger(__name__)

# 终态在内存的可见时长：前端轮询到 done/failed 后会停轮询并关弹窗；
# 超过这个时长回落 idle，下次开弹窗显示 last_sync（DB 真源）。
_TERMINAL_TTL_S = 30.0

# 单槽内存态：{task_id, state, stage, label, pct, message, error, started_at, finished_at}
_LIVE: dict[str, Any] | None = None


class SyncInProgressError(RuntimeError):
    """已有同步任务在跑（前端应等其完成，不并发触发）。"""


def _set_live(**kwargs: Any) -> None:
    """更新内存单槽态（不存在则新建）。"""
    global _LIVE
    if _LIVE is None:
        _LIVE = {}
    _LIVE.update(kwargs)


async def start_sync() -> dict[str, str]:
    """触发一次 ECS 同步（后台执行，立即返回）。

    Raises:
        SyncInProgressError: 已有同步在跑（路由层 → 409）。
        SyncConfigError: ECS 未配置 host/key（路由层 → 400）。
    """
    global _LIVE
    if _LIVE and _LIVE.get("state") == "syncing":
        raise SyncInProgressError("ECS 同步正在进行中，请等待完成")

    # 提前校验配置，失败快返回 400（否则要等后台任务才暴露配置错误）
    check_config()

    started_ts = time.time()
    _LIVE = {
        "task_id": None,
        "state": "syncing",
        "stage": "init",
        "label": "初始化",
        "pct": 0,
        "message": None,
        "error": None,
        "started_at": started_ts,
        "finished_at": None,
    }

    async def _run() -> dict[str, Any]:
        try:
            summary = await sync_push(progress_cb=_on_progress)
            # 补耗时（push 返回 dict 不含），写进 async_tasks.result 供 last_sync 取
            summary = {**summary, "duration_ms": int((time.time() - started_ts) * 1000)}
            _set_live(
                state="done",
                stage="done",
                label="完成",
                pct=100,
                finished_at=time.time(),
                error=None,
            )
            return summary
        except Exception as e:
            _set_live(
                state="failed",
                error=f"{type(e).__name__}: {e}",
                finished_at=time.time(),
            )
            raise

    def _on_progress(stage: str, label: str, pct: int) -> None:
        """push 的 progress_cb：同步更新内存态（事件循环线程内，无锁安全）。"""
        _set_live(stage=stage, label=label, pct=pct)

    task_id = await start_task(task_type="sync_push", resource_id="ecs", coro_factory=_run)
    _set_live(task_id=task_id)
    logger.info("sync_service: started sync task_id=%s", task_id)
    return {"task_id": task_id, "state": "syncing"}


async def get_last_sync() -> SyncLastSync | None:
    """最近一次成功同步摘要（查 async_tasks，跨重启有效）。无历史返回 None。"""
    rows = await list_tasks(task_type="sync_push", status="done", limit=1)
    if not rows:
        return None
    row = rows[0]
    result_raw = row.get("result") or "{}"
    try:
        summary = json.loads(result_raw) if result_raw else {}
    except json.JSONDecodeError:
        logger.warning("last_sync result not JSON: %s", result_raw[:120])
        summary = {}
    return SyncLastSync(
        finished_at=row.get("finished_at") or row.get("updated_at") or "",
        snapshot_bytes=int(summary.get("snapshot_bytes", 0) or 0),
        processed_synced=bool(summary.get("processed_synced", False)),
        keys_synced=bool(summary.get("keys_synced", False)),
        remote_snapshot_path=str(summary.get("remote_snapshot_path", "")),
        duration_ms=int(summary["duration_ms"]) if "duration_ms" in summary else None,
    )


async def get_progress() -> dict[str, Any]:
    """当前同步态 + 最近一次成功摘要（前端轮询入口）。

    state: idle（空闲）/ syncing（运行中）/ done|failed（最近一次终态，TTL 内可见）。
    """
    last = await get_last_sync()

    live = _LIVE
    if live and live.get("state") == "syncing":
        return {
            "state": "syncing",
            "stage": live.get("stage"),
            "stage_label": live.get("label"),
            "pct": live.get("pct", 0),
            "message": live.get("message"),
            "error": None,
            "task_id": live.get("task_id"),
            "last_sync": last,
        }

    # 终态 TTL 内透传，让前端看到刚完成/刚失败的结果
    finished_at = live.get("finished_at") if live else None
    if live and finished_at and (time.time() - finished_at) < _TERMINAL_TTL_S:
        return {
            "state": live.get("state"),
            "stage": live.get("stage"),
            "stage_label": live.get("label"),
            "pct": live.get("pct", 0),
            "message": live.get("message"),
            "error": live.get("error"),
            "task_id": live.get("task_id"),
            "last_sync": last,
        }

    return {"state": "idle", "last_sync": last}


async def get_comparison() -> dict[str, Any]:
    """本地 vs ECS 行数比对 + inbox 待 pull（透传 sync.status）。

    Raises:
        SyncConfigError: ECS 未配置。
    """
    return await sync_status()


__all__ = [
    "SyncInProgressError",
    "get_comparison",
    "get_last_sync",
    "get_progress",
    "start_sync",
]
