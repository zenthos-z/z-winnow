"""Key people route -- GET/POST/PUT/DELETE /api/v1/key-people.

# P054: Parse-validate-delegate. Zero business logic.
# W15-P1-KEYPEOPLE: PUT + DELETE endpoints added.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from z_winnow.web.schemas.key_people import (
    KeyPeopleCreate,
    KeyPeopleOut,
    KeyPeopleUpdate,
)

router = APIRouter(tags=["key-people"])


@router.get("/key-people", response_model=list[KeyPeopleOut])
async def list_key_people(
    request: Request,
    group_id: str = Query(..., description="Group ID"),
    date: str | None = Query(default=None, description="Date string YYYYMMDD (optional)"),
    limit: int = Query(default=100, ge=1, le=200),
) -> Any:
    """列出某个群的关键人物（重点成员，带发言统计）。

    【关键人物】看一个群里标记了哪些重要成员、各发了多少消息。

    什么时候用：在群详情/人物页查看重点成员。
    - 参数：group_id（必填）、date（指定某天统计；不填则统计该群所有日期）
    - 返回：关键人物列表 + 发言数等统计
    """
    from z_winnow.web.services.key_people_service import list_key_people as svc

    db: object = request.app.state.db_conn
    return await svc(db, group_id, date, limit=limit)


@router.post("/key-people", response_model=KeyPeopleOut, status_code=201)
async def create_key_person(
    request: Request,
    data: KeyPeopleCreate,
    group_id: str = Query(..., description="Group ID"),
) -> KeyPeopleOut:
    """手动标记某人为关键人物。

    【关键人物】关键人物通常由系统按发言量自动识别；这个接口用于人工指定。

    什么时候用：想把某人显式标为重要成员（即使他发言不多）。
    - 入参：sender、display_name、role、notes
    - 返回：201 + 新建的关键人物记录
    """
    from z_winnow.web.services.key_people_service import create_key_person as svc

    db: object = request.app.state.db_conn
    await svc(
        db,
        group_id=group_id,
        sender=data.sender,
        display_name=data.display_name,
        role=data.role or "member",
        notes=data.notes,
    )
    return KeyPeopleOut(
        sender=data.sender,
        display_name=data.display_name or data.sender,
        role=data.role or "member",
        notes=data.notes,
        message_count=0,
        group_id=group_id,
    )


@router.get("/key-people/source-members")
async def list_source_members(
    request: Request,
    group_id: str = Query(..., description="Group ID"),
) -> Any:
    """从数据源（CipherTalk）获取群真实成员列表，供前端「添加成员」下拉选择。

    【关键人物】从 CipherTalk API 拉取群成员，返回昵称/备注/微信号，
    自动按优先级排序（有昵称的排前面），方便用户点选而非手动输入。

    什么时候用：groups-config 页面点「添加成员」时，弹出候选列表供选择。
    - 参数：group_id（必填）
    - 返回：成员列表（wxid、nickname、remark、display_name、group_nickname）
    - 容错：数据源不可用时返回空列表（不阻断 UI 渲染）
    """
    from z_winnow.web.services.key_people_service import (
        list_source_members as svc,
    )

    db: object = request.app.state.db_conn
    return await svc(db, group_id)


# ---------------------------------------------------------------------------
# W15-P1-KEYPEOPLE: PUT + DELETE
# ---------------------------------------------------------------------------


@router.put(
    "/key-people/{sender}",
    response_model=KeyPeopleOut,
    responses={404: {"description": "Key person not found"}},
)
async def update_key_person(
    request: Request,
    sender: str,
    data: KeyPeopleUpdate,
    group_id: str = Query(..., description="Group ID"),
) -> KeyPeopleOut:
    """修改某个关键人物的信息（角色、备注等，按群隔离）。

    【关键人物】编辑已标记成员的属性，只更新传入的字段。

    什么时候用：改某人的角色或备注。
    - 参数：sender（路径）、group_id（必填 query，限定在哪个群里）
    - 出错：404 = 该群+该成员的组合不存在
    """
    from z_winnow.web.services.key_people_service import update_key_person as svc

    # P054: Build update_fields dict from the Pydantic model — only non-None
    # fields are forwarded.  Field translation happens in the service layer.
    update_fields: dict[str, object] = data.model_dump(exclude_unset=True)

    db: object = request.app.state.db_conn
    result = await svc(db, sender=sender, group_id=group_id, update_fields=update_fields)

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Key person {sender} not found in group {group_id}",
        )
    return result


@router.delete(
    "/key-people/{sender}",
    status_code=204,
    responses={404: {"description": "Key person not found"}},
)
async def delete_key_person(
    request: Request,
    sender: str,
    group_id: str = Query(..., description="Group ID"),
) -> None:
    """取消某人的关键人物标记（软删除，不删数据，置为停用）。

    【关键人物】把某人从重点成员里移除。

    什么时候用：不再重点关注某人。
    - 参数：sender（路径）、group_id（必填 query）
    - 返回：204 成功
    - 出错：404 = 该群+该成员不存在
    """
    from z_winnow.web.services.key_people_service import delete_key_person as svc

    db: object = request.app.state.db_conn
    deleted = await svc(db, sender=sender, group_id=group_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Key person {sender} not found in group {group_id}",
        )
