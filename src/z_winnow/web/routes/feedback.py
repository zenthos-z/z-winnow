"""Feedback route -- GET/POST /api/v1/feedback.

# P054: Parse-validate-delegate. Zero business logic.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from z_winnow.web.schemas.feedback import FeedbackCreate, FeedbackOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])


@router.get("/feedback", response_model=list[FeedbackOut])
async def list_feedback(
    request: Request,
    group_id: str = Query(..., description="Group ID"),
    date: str = Query(..., description="Date string YYYYMMDD"),
) -> Any:
    """列出某群某天尚未处理的用户反馈。

    【用户反馈】看用户对报告提了哪些意见、哪些还没消化。

    什么时候用：在「反馈」页查看待处理的反馈。
    - 参数：group_id、date（都必填）
    - 返回：未消化的反馈事件列表
    """
    from z_winnow.web.services.feedback_service import list_unconsumed_feedback

    db: object = request.app.state.db_conn
    rows = await list_unconsumed_feedback(db, group_id, date)
    return [FeedbackOut(**r) for r in rows]


@router.post("/feedback", response_model=FeedbackOut, status_code=201)
async def create_feedback(request: Request, body: FeedbackCreate) -> Any:
    """提交一条对报告的反馈（点赞/点踩/纠错等）。

    【用户反馈】用户看完报告后表达意见；反馈会入库并同步到长期记忆。

    什么时候用：前端报告页的"反馈"按钮提交。
    - 入参：signal（如 like/dislike）、severity、correction 等
    - 返回：201 + 反馈记录
    """
    from z_winnow.web.services.feedback_service import create_feedback as svc

    feedback_id = str(uuid.uuid4())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Extract enum values for storage
    signal_val = body.signal.value if hasattr(body.signal, "value") else body.signal
    severity_val = body.severity.value if hasattr(body.severity, "value") else body.severity

    db: object = request.app.state.db_conn
    # M4: 自动解析被反馈目标的原始内容（议题结论/概要/资源等）填入 original_text，
    # 否则 prompt <correction_example><original> 为空，LLM 不知被纠正的原内容。
    if body.original_text is None:
        from z_winnow.web.services.report_service import (
            resolve_original_text_for_feedback,
        )

        _orig = await resolve_original_text_for_feedback(
            db, body.target_type, body.target_id, body.target_version_id
        )
        if _orig:
            body = body.model_copy(update={"original_text": _orig})

    await svc(
        db,
        feedback_id=feedback_id,
        group_id=body.group_id,
        date=body.date,
        target_type=body.target_type,
        signal=signal_val,
        report_id=body.report_id,
        target_id=body.target_id,
        target_path=body.target_path,
        target_version_id=body.target_version_id,
        target_topic_id=body.target_topic_id,
        severity=severity_val,
        rating=body.rating,
        tags=body.tags,
        correction_mode=body.correction_mode,
        original_text=body.original_text,
        corrected_text=body.corrected_text,
        correction_note=body.correction_note,
        reporter=body.reporter,
    )

    # Enqueue MemOS feedback sync (fire-and-forget, non-blocking on error)
    try:
        from z_winnow.memory.feedback_sync import enqueue_feedback_sync

        await enqueue_feedback_sync(db, feedback_id)
    except Exception:
        logger.warning("feedback sync enqueue failed for id=%s", feedback_id, exc_info=True)

    return FeedbackOut(
        feedback_id=feedback_id,
        created_at=now,
        group_id=body.group_id,
        date=body.date,
        report_id=body.report_id,
        target_type=body.target_type,
        target_id=body.target_id,
        target_path=body.target_path,
        target_version_id=body.target_version_id,
        target_topic_id=body.target_topic_id,
        signal=signal_val,
        severity=severity_val,
        rating=body.rating,
        tags=body.tags,
        correction_mode=body.correction_mode,
        original_text=body.original_text,
        corrected_text=body.corrected_text,
        correction_note=body.correction_note,
        reporter=body.reporter,
    )


# ---------------------------------------------------------------------------
# W15-P1-FEEDBACK: detail / consume / rollback
# ---------------------------------------------------------------------------


@router.get("/feedback/{feedback_id}", response_model=FeedbackOut)
async def get_feedback(request: Request, feedback_id: str) -> Any:
    """按 ID 查看单条反馈。

    【用户反馈】看某条反馈的完整内容。

    什么时候用：点开列表里某条反馈看详情。
    - 出错：404 = 该反馈不存在
    """
    from z_winnow.web.services.feedback_service import get_feedback_by_id

    db: object = request.app.state.db_conn
    row = await get_feedback_by_id(db, feedback_id)
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")
    return FeedbackOut(**row)


@router.get("/feedback/{feedback_id}/provenance")
async def get_feedback_provenance(request: Request, feedback_id: str) -> Any:
    """查某条反馈的完整溯源四元组。

    【用户反馈】反馈溯源：反馈本体 + 被反馈版本/议题内容 + 反馈产出的新版本 + MemOS 记忆双节点。
    什么时候用：审计一条反馈如何介入并改变了报告与记忆。
    - 出错：404 = 该反馈不存在
    """
    from fastapi import HTTPException

    from z_winnow.web.services.feedback_service import (
        get_feedback_provenance as svc,
    )

    db: object = request.app.state.db_conn
    result = await svc(db, feedback_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")
    return result


@router.post("/feedback/{feedback_id}/consume", response_model=FeedbackOut)
async def consume_feedback(request: Request, feedback_id: str) -> Any:
    """把一条反馈标记为"已处理"。可重复调用（已处理再调仍返回成功）。

    【用户反馈】处理完一条反馈后标记它已消化，避免重复处理。

    什么时候用：人工/系统处理完反馈后调用。
    - 出错：404 = 该反馈不存在
    """
    from fastapi import HTTPException

    from z_winnow.web.services.feedback_service import (
        consume_feedback as svc_consume,
    )
    from z_winnow.web.services.feedback_service import (
        get_feedback_by_id,
    )

    db: object = request.app.state.db_conn

    row = await get_feedback_by_id(db, feedback_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")

    # Idempotent: always call consume; returns False if already consumed
    await svc_consume(db, feedback_id, consumed_by="api")

    row = await get_feedback_by_id(db, feedback_id)
    assert row is not None, f"Feedback {feedback_id} vanished during consume"
    return FeedbackOut(**row)


@router.post("/feedback/{feedback_id}/rollback", response_model=FeedbackOut)
async def rollback_feedback(request: Request, feedback_id: str) -> Any:
    """把"已处理"的反馈撤销回"未处理"。可重复调用。

    【用户反馈】误标已处理后，撤回到待处理状态。

    什么时候用：发现某反馈其实还没真正处理完。
    - 出错：404 = 该反馈不存在
    """
    from fastapi import HTTPException

    from z_winnow.web.services.feedback_service import (
        get_feedback_by_id,
    )
    from z_winnow.web.services.feedback_service import (
        rollback_feedback as svc_rollback,
    )

    db: object = request.app.state.db_conn

    row = await get_feedback_by_id(db, feedback_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")

    # Idempotent: always call rollback; returns False if already unconsumed
    await svc_rollback(db, feedback_id)

    row = await get_feedback_by_id(db, feedback_id)
    assert row is not None, f"Feedback {feedback_id} vanished during rollback"
    return FeedbackOut(**row)
