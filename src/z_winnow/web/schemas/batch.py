"""Batch generation Pydantic schemas.

Request/response models for batch-v2 API.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ============================================================
# Request Models
# ============================================================


class GroupDateRange(BaseModel):
    """日期范围配置：指定群在指定日期范围内生成日报。"""

    group_id: str = Field(..., description="群组 ID (g_xxx)")
    date_from: str = Field(..., description="起始日期 YYYY-MM-DD")
    date_to: str = Field(..., description="结束日期 YYYY-MM-DD")


class BatchRunV2Request(BaseModel):
    """批量日报生成请求。"""

    groups: list[GroupDateRange] = Field(..., min_length=1, description="群日期范围列表")
    max_parallel: int = Field(default=3, ge=1, le=20, description="最大并行群数（可覆盖 settings）")


class DataPreviewRequest(BaseModel):
    """数据预检请求参数。"""

    group_ids: list[str] = Field(..., min_length=1, description="群组 ID 列表")
    date_from: str = Field(..., description="起始日期 YYYY-MM-DD")
    date_to: str = Field(..., description="结束日期 YYYY-MM-DD")


# ============================================================
# Response Models
# ============================================================


class BatchRunV2Response(BaseModel):
    """批量日报生成响应（202 Accepted）。"""

    batch_id: str = Field(..., description="批次 ID")
    status_url: str = Field(..., description="状态查询 URL")
    total_groups: int = Field(..., description="总群数")
    total_days: int = Field(..., description="总天数")
    total_items: int = Field(..., description="总任务数（群×天）")


class BatchItemSummary(BaseModel):
    """单条任务明细（群×日期）。"""

    item_id: str = Field(..., description="任务项 ID")
    date: str = Field(..., description="日期 YYYY-MM-DD")
    status: str = Field(
        ..., description="状态: pending|running|completed|failed|skipped_empty|cancelled"
    )
    progress_pct: int = Field(default=0, ge=0, le=100, description="进度百分比")
    run_id: str | None = Field(default=None, description="关联的 pipeline run_id")
    error_message: str | None = Field(default=None, description="错误信息")


class BatchGroupSummary(BaseModel):
    """群级进度汇总。"""

    group_id: str = Field(..., description="群组 ID")
    display_name: str = Field(..., description="群显示名称")
    total: int = Field(..., description="该群总日期数")
    completed: int = Field(default=0, description="已完成数")
    failed: int = Field(default=0, description="失败数")
    skipped_empty: int = Field(default=0, description="空数据跳过数")
    progress_pct: int = Field(default=0, ge=0, le=100, description="该群进度百分比")
    items: list[BatchItemSummary] = Field(default_factory=list, description="日期级明细")


class BatchJobDetail(BaseModel):
    """批次任务详情。"""

    batch_id: str = Field(..., description="批次 ID")
    status: str = Field(..., description="状态: queued|running|completed|cancelled|partial_failed")
    total_groups: int = Field(..., description="总群数")
    total_days: int = Field(..., description="总天数")
    total_items: int = Field(..., description="总任务数")
    completed: int = Field(default=0, description="已完成数")
    failed: int = Field(default=0, description="失败数")
    skipped_empty: int = Field(default=0, description="空数据跳过数")
    max_parallel: int = Field(default=3, description="最大并行群数")
    progress_pct: int = Field(default=0, ge=0, le=100, description="综合进度百分比")
    started_at: str | None = Field(default=None, description="开始时间")
    completed_at: str | None = Field(default=None, description="完成时间")
    error_message: str | None = Field(default=None, description="错误信息")
    groups: list[BatchGroupSummary] = Field(default_factory=list, description="群级汇总")


class ActiveBatchSummary(BaseModel):
    """活跃批次摘要（列表接口轻量返回，不含 groups；详情走 GET /runs/batch/{id}）。"""

    batch_id: str = Field(..., description="批次 ID")
    status: str = Field(..., description="状态: queued|running")
    total_items: int = Field(..., description="总任务数")
    completed: int = Field(default=0, description="已完成数")
    failed: int = Field(default=0, description="失败数")
    skipped_empty: int = Field(default=0, description="空数据跳过数")
    progress_pct: int = Field(default=0, ge=0, le=100, description="综合进度百分比")
    started_at: str | None = Field(default=None, description="开始时间")


class DataPreviewItem(BaseModel):
    """数据预检单条结果。"""

    group_id: str = Field(..., description="群组 ID")
    date: str = Field(..., description="日期 YYYY-MM-DD")
    has_data: bool = Field(..., description="是否有数据（前端灰显判断依据）")
    message_count: int = Field(default=0, description="消息数量")


class DataPreviewResponse(BaseModel):
    """数据预检响应。"""

    items: list[DataPreviewItem] = Field(default_factory=list, description="预检结果列表")


class BatchCancelResponse(BaseModel):
    """批量取消响应。"""

    success: bool = Field(..., description="是否成功")
    status: str = Field(..., description="状态: cancelled")
    detail: str = Field(..., description="详情消息")


# ============================================================
# SSE Event Models
# ============================================================


class BatchSSEEvent(BaseModel):
    """SSE 事件基础模型。"""

    type: str = Field(..., description="事件类型: batch_update|item_update|batch_complete")
    batch_id: str = Field(..., description="批次 ID")


class BatchUpdateEvent(BatchSSEEvent):
    """批次级进度更新事件。"""

    type: str = Field(default="batch_update", description="事件类型")
    status: str = Field(..., description="批次状态")
    completed: int = Field(..., description="已完成数")
    failed: int = Field(..., description="失败数")
    skipped_empty: int = Field(..., description="空数据跳过数")
    total: int = Field(..., description="总数")
    progress_pct: int = Field(..., description="进度百分比")


class ItemUpdateEvent(BatchSSEEvent):
    """任务项级进度更新事件。"""

    type: str = Field(default="item_update", description="事件类型")
    item_id: str = Field(..., description="任务项 ID")
    group_id: str = Field(..., description="群组 ID")
    date: str = Field(..., description="日期")
    status: str = Field(..., description="状态")
    progress_pct: int = Field(default=0, description="进度百分比")
    error_message: str | None = Field(default=None, description="错误信息")


__all__ = [
    "ActiveBatchSummary",
    "BatchCancelResponse",
    "BatchGroupSummary",
    "BatchItemSummary",
    "BatchJobDetail",
    "BatchRunV2Request",
    "BatchRunV2Response",
    "BatchSSEEvent",
    "BatchUpdateEvent",
    "DataPreviewItem",
    "DataPreviewRequest",
    "DataPreviewResponse",
    "GroupDateRange",
    "ItemUpdateEvent",
]
