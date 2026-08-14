"""M4 P2 单元测试：regenerate 回填闭环 —— 经验派生 + MemOS 纠正 + 溯源回填。

锁定 _finalize_regeneration 的核心组件行为（不跑全图，用 mock adapter）：
  - group_experiences CRUD（create/list/update/status）
  - feedback_corrector：search 定位 → feedback_memory 纠正 → 回填 memos_node_id/archived
  - 幂等（已有 memos_node_id 跳过）
  - rating-only（无 corrected_text）不纠正记忆
  - cube_id_for_target 映射（含自定义表）
"""

from __future__ import annotations

import aiosqlite
import pytest

from z_winnow.memory.feedback_corrector import (
    correct_memory_for_feedback,
    cube_id_for_target,
    derive_lesson,
)
from z_winnow.memory.mock_adapter import MockMemOSAdapter
from z_winnow.memory.types import StructuredMemoryItem
from z_winnow.pipeline.database import (
    get_feedback,
    init_database_in_conn,
    update_feedback_provenance,
)
from z_winnow.pipeline.group_experiences import (
    create_experience,
    list_active_experiences,
    set_experience_status,
    set_status_by_origin_version,
    update_lesson,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await init_database_in_conn(conn)
    yield conn
    await conn.close()


async def _seed_feedback(db, **overrides):
    fb = {
        "feedback_id": "fb-1",
        "group_id": "g1",
        "date": "20260719",
        "report_id": "g1-20260719",
        "target_type": "topic",
        "target_id": "tp1",
        "signal": "correction",
        "corrected_text": "结论应为B",
        "correction_note": "A有内存泄漏",
    }
    fb.update(overrides)
    await db.execute(
        """INSERT INTO feedback_events
           (feedback_id, group_id, date, report_id, target_type, target_id, signal,
            corrected_text, correction_note)
           VALUES (:feedback_id, :group_id, :date, :report_id, :target_type, :target_id,
                   :signal, :corrected_text, :correction_note)""",
        fb,
    )
    await db.commit()
    return fb


# ── group_experiences CRUD ──────────────────────────────────────────────


async def test_experience_create_list_update_status(db):
    eid = await create_experience(
        db, "g1", "议题 tp1：结论应为B",
        topic_name="tp1", target_type="topic",
        origin_feedback_id="fb-1", origin_version_id="g1-20260719-v2",
    )
    exps = await list_active_experiences(db, "g1")
    assert len(exps) == 1
    assert exps[0].experience_id == eid
    assert exps[0].lesson == "议题 tp1：结论应为B"
    assert exps[0].topic_name == "tp1"

    ok = await update_lesson(db, eid, "更新后的经验", updated_by="tester")
    assert ok is True
    exps = await list_active_experiences(db, "g1")
    assert exps[0].lesson == "更新后的经验"
    assert exps[0].updated_by == "tester"
    assert exps[0].updated_at is not None

    # 归档后不再出现在 active 列表
    await set_experience_status(db, eid, "archived")
    assert await list_active_experiences(db, "g1") == []


async def test_experience_status_by_origin_version(db):
    """回滚联动：按 produced 版本批量归档经验。"""
    await create_experience(db, "g1", "e1", origin_version_id="g1-20260719-v2")
    await create_experience(db, "g1", "e2", origin_version_id="g1-20260719-v2")
    await create_experience(db, "g1", "e3", origin_version_id="g1-20260719-v3")
    n = await set_status_by_origin_version(db, "g1-20260719-v2", "archived")
    assert n == 2
    # v3 的仍在
    active = await list_active_experiences(db, "g1")
    assert len(active) == 1
    assert active[0].lesson == "e3"


# ── feedback_corrector ──────────────────────────────────────────────────


async def test_correct_memory_searches_and_backfills_provenance(db):
    fb = await _seed_feedback(db)
    adapter = MockMemOSAdapter()
    # 预置一个 topics cube 节点（含 topic_id tp1，供 search 命中）
    await adapter.add_structured_memory(
        cube_id="winnow:g1:topics",
        group_id="g1",
        items=[StructuredMemoryItem(memory="议题ID: tp1 议题: 世界模型 结论: A")],
    )

    res = await correct_memory_for_feedback(adapter, db, fb)
    assert res is not None
    assert res["cube_id"] == "winnow:g1:topics"
    assert res["node_id"]  # new (activated) node
    assert res["archived_id"]  # archived old node

    row = await get_feedback(db, "fb-1")
    assert row["memos_cube_id"] == "winnow:g1:topics"
    assert row["memos_node_id"] == res["node_id"]
    assert row["archived_memos_id"] == res["archived_id"]


async def test_correct_memory_is_idempotent(db):
    fb = await _seed_feedback(db)
    adapter = MockMemOSAdapter()
    await adapter.add_structured_memory(
        cube_id="winnow:g1:topics", group_id="g1",
        items=[StructuredMemoryItem(memory="议题ID: tp1 议题: X 结论: A")],
    )
    first = await correct_memory_for_feedback(adapter, db, fb)
    row = await get_feedback(db, "fb-1")
    second = await correct_memory_for_feedback(adapter, db, row)  # 已有 memos_node_id
    assert second["node_id"] == first["node_id"]


async def test_rating_only_feedback_not_corrected(db):
    """无 corrected_text 的反馈（rating/tag-only）不纠正记忆。"""
    fb = await _seed_feedback(db, corrected_text=None, signal="positive")
    adapter = MockMemOSAdapter()
    res = await correct_memory_for_feedback(adapter, db, fb)
    assert res is None
    row = await get_feedback(db, "fb-1")
    assert row["memos_node_id"] is None


def test_cube_id_for_target_mappings():
    assert cube_id_for_target("g1", "topic") == "winnow:g1:topics"
    assert cube_id_for_target("g1", "resource") == "winnow:g1:resources"
    assert cube_id_for_target("g1", "trend") == "winnow:g1:daily"
    assert cube_id_for_target("g1", "report") == "winnow:g1:daily"
    # 自定义表 → 同名 scope
    assert cube_id_for_target("g1", "engineering") == "winnow:g1:engineering"
    assert cube_id_for_target("g1", "world_models") == "winnow:g1:world_models"


def test_derive_lesson_template():
    assert derive_lesson({"target_type": "topic", "target_id": "tp1",
                          "corrected_text": "X"}) == "议题 tp1：X"
    assert derive_lesson({"target_type": "report", "corrected_text": "Y"}) == "整体日报：Y"


# ── provenance UPDATE helper ────────────────────────────────────────────


async def test_update_feedback_provenance_partial(db):
    await _seed_feedback(db)
    # 只回填 produced_version_id
    ok = await update_feedback_provenance(db, "fb-1", produced_version_id="g1-20260719-v2")
    assert ok is True
    row = await get_feedback(db, "fb-1")
    assert row["produced_version_id"] == "g1-20260719-v2"
    assert row["memos_node_id"] is None  # 其余未动
    # 再补 memos 字段
    await update_feedback_provenance(db, "fb-1", memos_cube_id="winnow:g1:topics",
                                     memos_node_id="n1", archived_memos_id="a1", status="active")
    row = await get_feedback(db, "fb-1")
    assert row["memos_node_id"] == "n1" and row["archived_memos_id"] == "a1"
