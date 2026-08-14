"""ECS 同步 Pydantic schemas.

Request/response models for /api/v1/sync/*（一键推送 L3 到 ECS + 进度查询 + 本地/ECS 比对）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ============================================================
# Response Models
# ============================================================


class SyncPushResponse(BaseModel):
    """``POST /sync/push`` 响应（202 Accepted，后台开始推送）。"""

    task_id: str = Field(..., description="后台任务 ID（async_tasks 主键，用于追踪）")
    state: str = Field(..., description="初始态：syncing")


class SyncLastSync(BaseModel):
    """最近一次成功同步的摘要（从 async_tasks.result 解析）。"""

    finished_at: str = Field(..., description="完成时间 ISO（async_tasks.finished_at）")
    snapshot_bytes: int = Field(default=0, description="L3 快照字节数")
    processed_synced: bool = Field(default=False, description="是否同步了 processed JSON")
    keys_synced: bool = Field(default=False, description="是否同步了 mcp_keys.yaml")
    remote_snapshot_path: str = Field(default="", description="ECS 端快照路径")
    duration_ms: int | None = Field(default=None, description="耗时（毫秒），可缺失")


class SyncProgressResponse(BaseModel):
    """``GET /sync/progress`` 响应：当前同步态 + 最近一次成功摘要。

    state: idle（空闲，无运行中任务）/ syncing / done（刚完成，前端据此打勾）/
    failed（刚失败，前端据此显错误）。done/failed 是「最近一次任务」的终态，
    下次开始同步前会被 syncing 覆盖。
    """

    state: str = Field(..., description="idle|syncing|done|failed")
    stage: str | None = Field(default=None, description="当前阶段 id（snapshot/connect/.../done）")
    stage_label: str | None = Field(default=None, description="当前阶段中文标签")
    pct: int = Field(default=0, ge=0, le=100, description="进度百分比")
    message: str | None = Field(default=None, description="附加消息（如跳过原因）")
    error: str | None = Field(default=None, description="失败时的错误信息")
    task_id: str | None = Field(default=None, description="当前/最近任务 ID")
    last_sync: SyncLastSync | None = Field(
        default=None, description="最近一次成功同步摘要（无历史为 null）"
    )


class SyncStatusResponse(BaseModel):
    """``GET /sync/status`` 响应：本地 vs ECS 行数比对 + inbox 待 pull 计数。

    直接透传 :func:`z_winnow.sync.status` 的形态：
    ecs_l3 / ecs_inbox 可能是 dict、``"NOT_EXISTS"`` 或错误串。
    """

    local: dict[str, int] = Field(default_factory=dict, description="本地主库各表行数")
    ecs_l3: Any = Field(default=None, description="ECS l3_snapshot 各表行数 / NOT_EXISTS / 错误串")
    ecs_inbox: Any = Field(
        default=None, description="ECS feedback_inbox 各表行数 / NOT_EXISTS / 错误串"
    )
    inbox_pending_pull: int = Field(default=0, description="ECS inbox 待 pull 的反馈数")


__all__ = [
    "SyncLastSync",
    "SyncProgressResponse",
    "SyncPushResponse",
    "SyncStatusResponse",
]
