"""Groups route -- CRUD /api/v1/groups.

# P054: Parse-validate-delegate. Zero business logic.
# B4: Handlers are thin adapters -- parse request, call service, return model.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from z_winnow.web.schemas.common import PaginatedResponse
from z_winnow.web.schemas.groups import (
    CipherTalkSessionsResponse,
    FeishuCatalogOut,
    FeishuInitRequest,
    FeishuTableKindOut,
    GroupCreate,
    GroupOut,
    GroupUpdate,
)

router = APIRouter(tags=["groups"])


@router.get("/groups", response_model=PaginatedResponse[GroupOut])
async def list_groups(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    is_active: bool | None = Query(default=True),
    search: str = Query(default=""),
) -> Any:
    """列出所有已注册的群组（分页）。

    【群组配置】看系统里登记了哪些要分析的群。

    什么时候用：在「群组」页展示群列表，或按名称搜索某个群。
    - 筛选：search（群名关键字）、is_active（是否启用）
    - 返回：分页的群组列表
    """
    from z_winnow.web.services.group_service import list_groups as svc_list

    db: object = request.app.state.db_conn
    return await svc_list(
        db,
        page=page,
        page_size=page_size,
        is_active=is_active,
        search=search,
    )


@router.get("/groups/sessions", response_model=CipherTalkSessionsResponse)
async def list_cipher_talk_sessions_route(request: Request) -> Any:
    """从 CipherTalk 拉取真实存在的群聊列表，供「新建群」时选择。

    【群组配置】注册新群时，从这里挑真实的群，而不是手输群 ID。

    什么时候用：点「新建群」弹出的群选择器加载这个接口。
    - 返回：可选群聊列表 + available 标记（CipherTalk 连不上时 available=false、列表为空，前端回退到手填）
    """
    from z_winnow.web.services.group_service import (
        list_cipher_talk_sessions as svc_sessions,
    )

    db: object = request.app.state.db_conn
    client = getattr(request.app.state, "cipher_talk_client", None)
    if client is None:
        # Lifespan did not attach a client (degraded boot / test) -> unavailable
        return CipherTalkSessionsResponse(sessions=[], available=False)
    return await svc_sessions(db, client)


@router.get("/groups/{group_id}", response_model=GroupOut)
async def get_group(request: Request, group_id: str) -> GroupOut:
    """按群 ID 查看单个群的详情。

    【群组配置】看某个群的基本信息和配置。

    什么时候用：在群列表里点某个群进详情页。
    - 出错：404 = 该群 ID 不存在
    """
    from z_winnow.web.services.group_service import get_group_detail

    db: object = request.app.state.db_conn
    result = await get_group_detail(db, group_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return result


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group(request: Request, body: GroupCreate) -> Any:
    """注册一个新群组（要分析哪个群，先在这里登记）。

    【群组配置】把一个微信群加入系统，之后才能给它生成日报。

    什么时候用：「新建群」时提交，通常先调 /groups/sessions 选真实群。
    - 入参：群名、chatroom_id 等
    - 返回：201 + 新建的群信息
    """
    # P024: Service call handles persistence; route just validates + delegates
    from z_winnow.web.services.group_service import create_group as svc_create

    db: object = request.app.state.db_conn
    return await svc_create(db, body)


@router.put("/groups/{group_id}", response_model=GroupOut)
async def update_group(request: Request, group_id: str, body: GroupUpdate) -> Any:
    """修改一个群组的信息（改名、启停等）。

    【群组配置】编辑已登记群的属性。

    什么时候用：在群详情页修改配置。
    - 出错：404 = 该群不存在
    """
    from z_winnow.web.services.group_service import update_group as svc_update

    db: object = request.app.state.db_conn
    result = await svc_update(db, group_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return result


@router.get("/feishu/catalog", response_model=FeishuCatalogOut)
async def get_feishu_catalog() -> FeishuCatalogOut:
    """飞书表种类目录（全局，所有群共用）。

    【群组配置·飞书】渲染「这个群要哪些表」勾选清单的数据源。每个种类含
    kind/显示名/是否必选(mandatory)/默认是否启用/字段数。加新表种类后这里自动多一项。

    什么时候用：群配置页「飞书推送」区加载时拉一次，渲染表清单；3 个 mandatory
    (议题/资源/日报汇总) 锁定必选，其余按群勾选。
    """
    from z_winnow.web.services.group_service import get_feishu_table_catalog

    return FeishuCatalogOut(kinds=[FeishuTableKindOut(**k) for k in get_feishu_table_catalog()])


@router.post("/groups/{group_id}/feishu/init", response_model=GroupOut)
async def init_group_feishu(request: Request, group_id: str, body: FeishuInitRequest) -> GroupOut:
    """给这个群初始化飞书多维表格框架（建 Base + 日报汇总/议题明细/资源/工程问题表）。

    【群组配置·飞书】把一个群接到飞书多维表格上。第一次用要初始化框架。

    什么时候用：群配置页「飞书推送」区点「初始化多维表格框架」。
    - 入参（可选）：base_target（已有 Base 的链接/token，留空=自动新建）、
      enabled_kinds（UI 勾选的可选表种类，不传则从群 engineering_enabled 推导）。
    - 返回：更新后的群信息（含 base_token + 各 table_id + 框架已初始化）
    - 出错：404 = 群不存在；400 = 框架初始化失败（lark-cli 未配置/权限不足等，detail 给原因）
    """
    from z_winnow.web.services.group_service import init_group_feishu_framework

    db: object = request.app.state.db_conn
    try:
        return await init_group_feishu_framework(
            db,
            group_id,
            base_target=body.base_target,
            enabled_kinds=body.enabled_kinds,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail="Group not found") from exc
        raise HTTPException(status_code=400, detail=f"飞书框架初始化失败：{msg}") from exc


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(request: Request, group_id: str) -> None:
    """删除一个群组。

    【群组配置】不再分析某个群时移除它。会级联清理该群的全部相关数据：
    本地孤儿表（topic_summaries/raw_messages/...）、磁盘 L3 正文、以及
    MemOS 长期记忆（best-effort，MemOS 不可用不阻断）。

    什么时候用：在群详情页点删除。
    - 返回：204 删除成功
    - 出错：404 = 该群不存在
    """
    from z_winnow.web.services.group_service import delete_group as svc_delete

    db: object = request.app.state.db_conn
    adapter = getattr(request.app.state, "memos_adapter", None)
    deleted = await svc_delete(db, group_id, adapter=adapter)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")
