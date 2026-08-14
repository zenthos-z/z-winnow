"""M4: group_experiences 表 CRUD — 可编辑、群绑定、跨天的经验家园（L3，不进 MemOS）。

经验从反馈事件派生（regenerate 成功后，零 LLM 模板拼装），是 correction_loader
召回注入 unified_reporter ``<prior_corrections>`` 的主源。与 feedback_events 分工：
feedback_events = 原始事件日志（溯源）；group_experiences = 派生可召回经验。

P022: 纯存储层 — 仅 SQL。DDL 在 database.py（CREATE TABLE group_experiences）。
P050: 参数化 SQL。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupExperience:
    """group_experiences 行。"""

    experience_id: str
    group_id: str
    topic_name: str | None
    target_type: str | None
    lesson: str
    origin_feedback_id: str | None
    origin_version_id: str | None
    status: str = "active"
    created_at: str = ""
    updated_at: str | None = None
    updated_by: str | None = None


def _row_to_experience(row: aiosqlite.Row) -> GroupExperience:
    return GroupExperience(
        experience_id=row["experience_id"],
        group_id=row["group_id"],
        topic_name=row["topic_name"],
        target_type=row["target_type"],
        lesson=row["lesson"],
        origin_feedback_id=row["origin_feedback_id"],
        origin_version_id=row["origin_version_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


async def create_experience(
    db: aiosqlite.Connection,
    group_id: str,
    lesson: str,
    *,
    topic_name: str | None = None,
    target_type: str | None = None,
    origin_feedback_id: str | None = None,
    origin_version_id: str | None = None,
    experience_id: str | None = None,
) -> str:
    """插入一条经验。返回 experience_id。

    experience_id 不传则生成 ``exp-{timestamp-ish}``。由于本模块不能调用 time
    随机源，调用方应传入确定性 id（如基于 feedback_id）。
    """
    if not experience_id:
        # 用 feedback_id 派生（确定性），否则要求调用方提供
        if origin_feedback_id:
            experience_id = f"exp-{origin_feedback_id}"
        else:
            experience_id = f"exp-{group_id}-{hash(lesson) & 0xFFFFFFFF}"

    await db.execute(
        """INSERT OR REPLACE INTO group_experiences
           (experience_id, group_id, topic_name, target_type, lesson,
            origin_feedback_id, origin_version_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
        (
            experience_id,
            group_id,
            topic_name,
            target_type,
            lesson,
            origin_feedback_id,
            origin_version_id,
        ),
    )
    await db.commit()
    return experience_id


async def update_lesson(
    db: aiosqlite.Connection,
    experience_id: str,
    lesson: str,
    *,
    updated_by: str = "admin",
) -> bool:
    """修改经验句（经验家园可编辑）。同时戳 updated_at/updated_by。"""
    cur = await db.execute(
        """UPDATE group_experiences
           SET lesson = ?, updated_at = datetime('now'), updated_by = ?
           WHERE experience_id = ?""",
        (lesson, updated_by, experience_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def set_experience_status(
    db: aiosqlite.Connection,
    experience_id: str,
    status: str,
) -> bool:
    """改状态（active/archived/superseded）——回滚联动用。"""
    cur = await db.execute(
        "UPDATE group_experiences SET status = ? WHERE experience_id = ?",
        (status, experience_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_active_experiences(
    db: aiosqlite.Connection,
    group_id: str,
) -> list[GroupExperience]:
    """列某群的 active 经验（correction_loader 召回主源）。"""
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """SELECT * FROM group_experiences
           WHERE group_id = ? AND status = 'active'
           ORDER BY created_at DESC""",
        (group_id,),
    )
    rows = await cur.fetchall()
    return [_row_to_experience(r) for r in rows]


async def get_experience(
    db: aiosqlite.Connection,
    experience_id: str,
) -> GroupExperience | None:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT * FROM group_experiences WHERE experience_id = ?",
        (experience_id,),
    )
    row = await cur.fetchone()
    return _row_to_experience(row) if row else None


async def set_status_by_origin_version(
    db: aiosqlite.Connection,
    origin_version_id: str,
    status: str,
) -> int:
    """批量改某版本派生的经验状态（回滚联动：produced 版本回滚 → 经验 archived）。"""
    cur = await db.execute(
        "UPDATE group_experiences SET status = ? WHERE origin_version_id = ?",
        (status, origin_version_id),
    )
    await db.commit()
    return cur.rowcount
