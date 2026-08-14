"""MCP ``submit_feedback`` 入参 schema —— 反馈 Inbox 的格式守门人。

公网 MCP 服务（``mcp.example.com``）的 ``submit_feedback`` 是外部 Agent / 脚本向
知识库写反馈的**唯一对外入口**。本模块是该入口的 schema 单一真源：不符合格式的
请求在写库前被 :func:`validate_feedback_payload` 拒绝（raise ``ToolError``），

.. note::

   本 schema 是 **MCP 路径专属**，刻意不复用 ``web/schemas/feedback.py`` 的
   ``FeedbackCreate``——后者绑了 web API 自己的 ``SignalType`` 枚举
   （``positive/negative/neutral/correction``），与 MCP 文档约定的 5 值集不同，
   属两条独立入库路径的既有契约差异（见 docs/mcp-platform-checkpoint.md §4.1）。

合法取值（与 ``.claude/skills/winnow-mcp/`` 技能包文档一致；drift 由
``tests/test_mcp_feedback_schema.py`` 的 drift-guard 测试兜底）：

- ``signal`` ∈ {correction, supplement, approval, stale, quality}
- ``target_type`` ∈ {report, trend, highlights, topic, resource, section} ∪
  ``custom_tables`` registry 已注册的表 id（engineering / world_models / …）
- ``date`` ∈ YYYYMMDD 或 YYYY-MM-DD（且为真实日历日期），内部归一为 YYYY-MM-DD 存储
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# ============================================================
# Canonical allowed-value sets
# ============================================================


class FeedbackSignal(StrEnum):
    """反馈意图 —— 与 feedback_events.signal 列及 MCP 文档一致。

    correction/supplement 视作"纠错类"：``content`` 存为 corrected_text；
    approval/stale/quality 视作"标注类"：``content`` 存为 correction_note。
    """

    CORRECTION = "correction"
    SUPPLEMENT = "supplement"
    APPROVAL = "approval"
    STALE = "stale"
    QUALITY = "quality"


#: target_type 基础闭集 —— report_service.py 实际会分支解析的类型 + 文档约定的 section。
#: 自定义表 id 由 :func:`allowed_target_types` 动态并入。
BASE_TARGET_TYPES: frozenset[str] = frozenset(
    {"report", "trend", "highlights", "topic", "resource", "section"}
)


def allowed_target_types() -> set[str]:
    """返回当前合法的 target_type 全集 = 基础集 ∪ custom_tables registry 已注册表 id。

    registry 不可用（import 失败 / 无表）时退化为仅基础集，绝不抛异常阻塞 MCP 启动。
    """
    types: set[str] = set(BASE_TARGET_TYPES)
    try:
        from z_winnow.custom_tables.registry import get_all_tables

        for tdef in get_all_tables():
            if tdef.id:
                types.add(tdef.id)
    except Exception:  # pragma: no cover - registry 缺失时降级，不阻塞校验
        pass
    return types


# ============================================================
# Date validation
# ============================================================

_DATE_RE = re.compile(r"^\d{8}$|^\d{4}-\d{2}-\d{2}$")


def _parse_and_normalize_date(value: str) -> str:
    """校验 date 为 YYYYMMDD 或 YYYY-MM-DD 且为真实日历日期；返回归一化的 YYYY-MM-DD。

    ``2026-13-40`` 这类形态对、但日期非法的值会被 ``strptime`` 拒掉。
    """
    s = value.strip()
    if not _DATE_RE.match(s):
        raise ValueError("date 必须是 YYYYMMDD 或 YYYY-MM-DD 格式（如 20260720 或 2026-07-20）")
    fmt = "%Y%m%d" if len(s) == 8 else "%Y-%m-%d"
    try:
        dt = datetime.strptime(s, fmt)
    except ValueError as e:
        raise ValueError(f"date 不是合法日历日期：{s}") from e
    return dt.strftime("%Y-%m-%d")


# ============================================================
# Pydantic model
# ============================================================


class FeedbackSubmission(BaseModel):
    """``submit_feedback`` 的入参 schema（MCP 路径）。

    必填：``group_id`` / ``date`` / ``target_type`` / ``signal`` / ``content``。
    可选：``target_id`` / ``target_version_id`` / ``target_topic_id`` / ``original_text``。
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    group_id: str = Field(min_length=1, description="群 ID（须在 key 的 allowed_groups 内）")
    date: str = Field(min_length=1, description="日期锚点 YYYYMMDD 或 YYYY-MM-DD")
    target_type: str = Field(min_length=1, description="反馈对象类型")
    signal: FeedbackSignal
    content: str = Field(min_length=1, description="反馈正文")
    target_id: str | None = None
    target_version_id: str | None = None
    target_topic_id: str | None = None
    original_text: str | None = None

    @field_validator("date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        return _parse_and_normalize_date(v)

    @field_validator("target_type")
    @classmethod
    def _validate_target_type(cls, v: str) -> str:
        allowed = allowed_target_types()
        if v not in allowed:
            raise ValueError(f"target_type 必须是下列之一（不区分先后）：{sorted(allowed)}")
        return v


# ============================================================
# Public entry — aggregate all violations into one ToolError
# ============================================================


def _format_errors(exc: ValidationError) -> str:
    """把 pydantic ValidationError 聚合成一条中文消息（列出全部违规，非仅首个）。"""
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "")
        msg = err.get("msg", "invalid")
        lines.append(f"  • {loc or '(root)'}：{msg}")
    return "\n".join(lines)


def validate_feedback_payload(**kwargs: Any) -> FeedbackSubmission:
    """校验 submit_feedback 入参；非法则 raise :class:`ToolError`（聚合全部违规）。

    服务端在 ``_check_group_access`` 之后、任何写库之前调用本函数——不合法的请求
    不会落到 feedback_events。
    """
    try:
        return FeedbackSubmission(**kwargs)
    except ValidationError as exc:
        hint_signal = f"合法 signal：{sorted(s.value for s in FeedbackSignal)}"
        hint_target = f"合法 target_type：{sorted(allowed_target_types())}"
        raise ToolError(
            "反馈格式校验失败，已拒绝写入（未落库）。违规项：\n"
            f"{_format_errors(exc)}\n"
            f"参考 —— {hint_signal}；{hint_target}"
        ) from exc
