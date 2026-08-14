"""Reports route -- GET /api/v1/reports, GET /api/v1/reports/{report_id},
POST /api/v1/reports/{rid}/regenerate, GET /api/v1/reports/{rid}/export.

# P054: Parse-validate-delegate. Zero business logic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from z_winnow.web.schemas.common import (
    AsyncTaskResponse,
    PaginatedResponse,
    TaskStatusResponse,
)
from z_winnow.web.schemas.reports import (
    CoverRequest,
    FeishuPushRequest,
    RegenerateRequest,
    ReportContentOut,
    ReportDiffOut,
    ReportVersionOut,
)

router = APIRouter(tags=["reports"])


@router.get("/regenerate/active")
async def list_active_regenerate(request: Request) -> Any:
    """列出所有运行中的「根据反馈重生成」任务。

    【报告产出】页面加载/刷新时恢复"重生成中"按钮状态（防止刷新后丢失进度、防重复触发）。
    什么时候用：打开报告页时自动调一次，给运行中的 regen 任务对应的按钮置为 loading。
    - 返回：[{task_id, report_id, version_id, status}, ...]（仅 queued/running/pending）
    """
    from z_winnow.web.services.report_service import list_active_regenerate_tasks

    db: object = request.app.state.db_conn
    return await list_active_regenerate_tasks(db)


@router.get("/reports", response_model=PaginatedResponse[ReportVersionOut])
async def list_reports(
    request: Request,
    group_id: str | None = Query(default=None),
    date: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Any:
    """列出所有报告（日报），支持按群组、日期筛选和分页。

    【报告产出】浏览历史上生成过的报告。

    什么时候用：在「报告」页查看历史报告列表，或按群组/日期缩小范围。
    - 筛选：group_id（群组）、date（日期 YYYYMMDD）
    - 返回：分页的报告版本列表（每条含报告 ID、群组、日期、版本号）
    """
    from z_winnow.web.services.report_service import list_report_versions

    db: object = request.app.state.db_conn
    return await list_report_versions(
        db,
        group_id=group_id,
        date=date,
        page=page,
        page_size=page_size,
    )


@router.get("/reports/{report_id}", response_model=ReportVersionOut)
async def get_report(request: Request, report_id: str) -> ReportVersionOut:
    """按报告 ID 查看单个报告的概要信息（不含正文内容）。

    【报告产出】拿到一份报告的基本信息。

    什么时候用：从列表点进某份报告时，先取它的元信息（群组、日期、版本号等）。
    - 出错：404 = 该报告 ID 不存在
    """
    from z_winnow.web.services.report_service import get_report_version

    db: object = request.app.state.db_conn
    result = await get_report_version(db, report_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Report version not found")
    return result


@router.delete("/reports/{report_id}", status_code=204)
async def delete_report_endpoint(request: Request, report_id: str) -> None:
    """删除一整份报告（该 report_id 的全部版本 + 磁盘上的 L3 正文 JSON）。

    【报告产出】把某群某天的整份报告彻底移除。

    什么时候用：在「报告」页某天的日报上点删除，确认后该报告从时间线消失。
    - report_id 即 ``{group_id}-{date}``，删除等价于移除该群当天整份报告（含历史版本）
    - 同步删除磁盘 L3 JSON（``data/processed/{group_id}/{date}/``）；topic_summaries / feedback 不动
    - 返回：204 删除成功
    - 出错：404 = 该 report_id 不存在（无任何版本）
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import delete_report

    db: object = request.app.state.db_conn
    deleted = await delete_report(db, report_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Report not found")


@router.get("/reports/{report_id}/content", response_model=ReportContentOut)
async def get_report_content_endpoint(
    request: Request,
    report_id: str,
    report_type: str = Query(default="daily", description="daily|resources|engineering|topics"),
) -> ReportContentOut:
    """读取一份报告的正文内容（结构化数据），前端用它渲染报告。

    【报告产出】打开某份报告查看其日报/资源/工程问题/议题的正文。

    什么时候用：在「报告」页点开一份报告看内容时调用。
    - 参数：report_type = daily（日报）/ resources（资源）/ engineering（工程问题）/ topics（议题）
    - 返回：报告正文的结构化数据；纯读文件，不调用 AI
    - 出错：404 = 报告不存在，或该类型的内容文件缺失
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import (
        get_report_content,
        get_report_version,
    )

    db: object = request.app.state.db_conn
    version = await get_report_version(db, report_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Report version not found")
    content = await get_report_content(
        db,
        version.group_id,
        version.date,
        report_type=report_type,
        created_at=version.created_at,
        version_number=version.version_number,
    )
    if content is None:
        raise HTTPException(status_code=404, detail="L3 JSON not found on disk")
    # A026: content is a report_service.ReportContent whose fields match
    # ReportContentOut 1:1; convert into the schema model so the declared
    # return type and response_model both hold (from_attributes on ReportContentOut).
    return ReportContentOut.model_validate(content)


# ============================================================
# W15-P0-REPORTS: regenerate + export endpoints
# ============================================================


@router.post("/reports/{rid}/regenerate", response_model=AsyncTaskResponse, status_code=202)
async def regenerate_report_endpoint(
    request: Request,
    rid: str,
    body: RegenerateRequest | None = None,
) -> AsyncTaskResponse:
    """重新生成一份报告（后台执行，立即返回任务号）。

    【报告产出】对已有报告重跑一遍，产出新版本。

    什么时候用：原报告不满意、或换了配置/数据后想重新生成。
    - 入参（可选）：body 可覆盖 group_id 和 date；不填则沿用该报告原来的群组/日期
    - 返回：202 + 任务号；用 GET /reports/{rid}/tasks/{task_id} 查进度
    - 出错：404 = 报告 ID 不存在
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import regenerate_report

    db: object = request.app.state.db_conn
    group_id = body.group_id if body else None
    date = body.date if body else None
    task_id = await regenerate_report(db, rid, group_id=group_id, date=date)
    if task_id is None:
        raise HTTPException(status_code=404, detail="Report version not found")

    # B8/AC2: status_url points at the reports-scoped per-task status endpoint
    # (GET /reports/{rid}/tasks/{task_id}) which really reads the async_tasks
    # table. The previous non-existent tasks route was a dead link.
    status_url = f"/api/v1/reports/{rid}/tasks/{task_id}"
    return AsyncTaskResponse(task_id=task_id, status_url=status_url)


@router.get("/reports/{rid}/export")
async def export_report_endpoint(
    request: Request,
    rid: str,
    group_id: str | None = Query(default=None, description="Override group ID for L3 JSON lookup"),
    date: str | None = Query(default=None, description="Override date YYYYMMDD for L3 JSON lookup"),
) -> Response:
    """把一份报告导出成 Markdown 文本。

    【报告产出】拿到能直接复制/粘贴或存档的 Markdown 版报告。

    什么时候用：想把报告内容贴到别处、或离线保存。
    - 返回：text/markdown 纯文本；不调用 AI
    - 出错：404 = 报告或其内容文件不存在
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import export_report

    db: object = request.app.state.db_conn
    md_text = await export_report(db, rid, group_id=group_id, date=date)
    if md_text is None:
        raise HTTPException(status_code=404, detail="Report version or L3 JSON not found")

    return Response(content=md_text, media_type="text/markdown")


# ============================================================
# W15-P1-REPORTS: Version listing, diff, Feishu push
# L098: Serial after W15-P0-REPORTS -- appended below, no edits above.
# ============================================================


@router.get("/reports/{report_id}/versions", response_model=list[ReportVersionOut])
async def list_report_versions_by_id(request: Request, report_id: str) -> list[ReportVersionOut]:
    """列出同一份报告的所有历史版本（按版本号升序）。

    【报告产出】看一份报告被重跑过几次、每次的版本记录。

    什么时候用：报告经过多次重新生成后，想查看/对比各版本。
    - 返回：版本列表（若该报告没有任何版本则返回空列表）
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import get_report_versions

    db: object = request.app.state.db_conn
    return await get_report_versions(db, report_id)


@router.post("/reports/{report_id}/versions/{version_id}/rollback")
async def rollback_report_version(request: Request, report_id: str, version_id: str) -> Any:
    """把某份日报的当前生效版本回滚到指定历史版本。

    【报告产出】版本回滚：目标版本重新成为 active，其后产出的较新版本失效；
    这些较新版本产生的反馈标记 rolled_back、派生经验归档。回滚单元=日报版本。

    什么时候用：发现某次反馈重生成让报告变差，想退回之前的版本。
    - 出错：404 = 版本不存在
    """
    from fastapi import HTTPException

    from z_winnow.web.services.report_service import rollback_to_version

    db: object = request.app.state.db_conn
    result = await rollback_to_version(db, version_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Version {version_id} not found")
    return result


@router.get("/reports/{report_id}/diff", response_model=ReportDiffOut)
async def get_report_diff_endpoint(request: Request, report_id: str) -> ReportDiffOut:
    """对比一份报告最近两个版本的差异。

    【报告产出】看重新生成后报告改了哪些地方。

    什么时候用：重新生成报告之后，想看新版和上一版的区别。
    - 返回：最近两个版本之间的 diff
    - 出错：404 = 该报告不足 2 个版本，无法对比
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import get_report_diff

    db: object = request.app.state.db_conn
    result = await get_report_diff(db, report_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Need at least 2 versions for diff")
    return result


@router.post("/reports/{report_id}/feishu", response_model=AsyncTaskResponse, status_code=202)
async def push_report_to_feishu_endpoint(
    request: Request,
    report_id: str,
    body: FeishuPushRequest | None = None,
) -> AsyncTaskResponse:
    """把一份报告推送到飞书多维表格（后台执行，立即返回任务号）。

    【报告产出】把生成的日报同步到飞书存档或分享。

    什么时候用：报告生成好后，要发到飞书。
    - 入参（可选）：body.doc_title 自定义飞书文档标题；body.overwrite 是否覆盖旧记录（默认 true）
    - 返回：202 + 任务号；用 GET /reports/{rid}/tasks/{task_id} 查进度
    - 出错：404 = 报告不存在或没有任何版本
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import push_report_to_feishu

    db: object = request.app.state.db_conn
    doc_title = body.doc_title if body else None
    overwrite = body.overwrite if body else True
    task_id = await push_report_to_feishu(db, report_id, doc_title=doc_title, overwrite=overwrite)
    if task_id is None:
        raise HTTPException(status_code=404, detail="Report not found or has no versions")

    # B8/AC2: status_url points at the reports-scoped per-task status endpoint.
    status_url = f"/api/v1/reports/{report_id}/tasks/{task_id}"
    return AsyncTaskResponse(task_id=task_id, status_url=status_url)


@router.post(
    "/reports/{report_id}/feishu/records/delete", response_model=AsyncTaskResponse, status_code=202
)
async def delete_feishu_records_endpoint(
    request: Request,
    report_id: str,
) -> AsyncTaskResponse:
    """删除飞书表格中该报告日期的旧记录（独立端点，不上传）。

    【飞书管理】仅删除旧记录、不创建新内容。

    什么时候用：需要手动清理飞书旧记录但不想重新上传时。
    - 返回：202 + 任务号；用 GET /reports/{rid}/tasks/{task_id} 查进度
    - 出错：404 = 报告不存在或没有任何版本；400 = 群未启用飞书
    """
    # A008: explicit initialization
    from z_winnow.web.services.report_service import delete_feishu_records_for_report

    db: object = request.app.state.db_conn
    task_id = await delete_feishu_records_for_report(db, report_id)
    if task_id is None:
        raise HTTPException(status_code=404, detail="Report not found or has no versions")

    status_url = f"/api/v1/reports/{report_id}/tasks/{task_id}"
    return AsyncTaskResponse(task_id=task_id, status_url=status_url)


# ============================================================
# W16-A3/B8: Reports-scoped async task status (thin read endpoint)
# # P054 + P022: Thin read of async_tasks — zero business logic.
# # A002: Real GET endpoint that reads async_tasks, replacing the previous
# #       dead per-task link emitted by regenerate/feishu.
# ============================================================


@router.get("/reports/{rid}/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_report_task_status(
    request: Request,
    rid: str,
    task_id: str,
) -> TaskStatusResponse:
    """查询某份报告的后台任务进度（重新生成 / 飞书推送）。

    【报告产出】配合「重新生成」「飞书推送」这两个异步接口轮询结果。

    什么时候用：发起重新生成或飞书推送后，查任务跑到哪了、是否成功。
    - 返回：任务状态（pending/running/done/failed）+ 结果或错误信息
    - 出错：404 = 任务号不存在
    """
    # A008: explicit initialization
    from z_winnow.web.services.task_queue import get_task_status

    row = await get_task_status(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Best-effort parse of the stored result JSON for caller convenience.
    raw_result = row.get("result_json")
    parsed_result: Any = None
    if raw_result:
        import json

        try:
            parsed_result = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            parsed_result = raw_result

    return TaskStatusResponse(
        task_id=task_id,
        status=row.get("status", "unknown"),
        progress=None,
        result=parsed_result,
        error=row.get("error_message"),
    )


# ============================================================
# #9.2 Web API: 日报配图生成（本地，不挂飞书）
# ============================================================


@router.post("/reports/{report_id}/cover", response_model=AsyncTaskResponse, status_code=202)
async def generate_report_cover_endpoint(
    request: Request,
    report_id: str,
    body: CoverRequest | None = None,
) -> AsyncTaskResponse:
    """生成该日报的配图（后台执行，立即返回任务号）。只落本地，不挂飞书。

    【报告产出】给当日日报生成一张信息图风格配图，供预览/后续挂飞书。
    什么时候用：想在报告里配一张图时点这个（生图约 1 分钟）。
    - 入参（可选）：body.count/ratio/size 覆盖默认；不传走配置
    - 返回：202 + 任务号；用 GET /reports/{rid}/tasks/{task_id} 查进度
    - 出错：404 = 报告不存在或没有任何版本
    """
    from z_winnow.web.services.report_service import generate_report_cover

    db: object = request.app.state.db_conn
    count = body.count if body else None
    ratio = body.ratio if body else None
    size = body.size if body else None
    task_id = await generate_report_cover(db, report_id, count=count, ratio=ratio, size=size)
    if task_id is None:
        raise HTTPException(status_code=404, detail="Report not found or has no versions")

    status_url = f"/api/v1/reports/{report_id}/tasks/{task_id}"
    return AsyncTaskResponse(task_id=task_id, status_url=status_url)


@router.get("/reports/{report_id}/cover")
async def get_report_cover_endpoint(
    request: Request,
    report_id: str,
) -> FileResponse:
    """取该日报已生成的配图 PNG（供 <img src> 预览）。

    【报告产出】配图生成完后，前端/设计稿用这个 URL 显示图片。
    什么时候用：要展示配图时（配图必须已生成）。
    - 返回：200 + image/png；404 = 报告不存在或尚未生成配图
    """
    from z_winnow.web.services.report_service import get_cover_image

    db: object = request.app.state.db_conn
    cover_path = await get_cover_image(db, report_id)
    if cover_path is None:
        raise HTTPException(status_code=404, detail="Cover not generated for this report")
    return FileResponse(str(cover_path), media_type="image/png")
