"""M4: 反馈驱动的 MemOS 记忆纠正 —— 走原生 feedback_memory（归档旧 + 写新）。

按 ``feedback.target_type`` 定位 cube + search 取 retrieved_memory_ids，调
adapter.feedback_memory 纠正节点，回填 feedback_event 的 memos_node_id /
archived_memos_id / memos_cube_id。与 group_experiences（L3 经验）分工：
本模块纠正 MemOS 语义记忆里的事实节点；经验家园在 SQLite（group_experiences.py）。

幂等：若 feedback_event 已有 memos_node_id 则跳过（不重复纠正）。
适用：有 corrected_text 且 target 可定位的反馈。rating/tag-only 不纠正记忆。
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from z_winnow.memory.types import MemOSAdapterProtocol

logger = logging.getLogger(__name__)

# 固定 target_type → cube scope。自定义表 target_type = table_id → 其同名 scope。
_FIXED_TARGET_CUBES: dict[str, str] = {
    "topic": "topics",
    "resource": "resources",
    "trend": "daily",
    "report": "daily",
}


def cube_id_for_target(group_id: str, target_type: str) -> str:
    """target_type → cube_id。自定义表（engineering/world_models/...）用其同名 scope。"""
    scope = _FIXED_TARGET_CUBES.get(target_type, target_type)
    return f"winnow:{group_id}:{scope}"


def _search_query_for(fb: dict[str, Any]) -> str:
    """从反馈 target 构造 search query（匹配 memory_content 里的标识）。"""
    tt = fb.get("target_type", "")
    tid = (fb.get("target_id") or "").strip()
    if tt == "topic":
        return tid or (fb.get("target_topic_id") or "").strip()
    return tid  # resource_title / trend_{date} / {kind}_id


def _target_label(fb: dict[str, Any]) -> str:
    tt = fb.get("target_type", "")
    tid = fb.get("target_id") or ""
    return {
        "topic": f"议题 {tid}".strip(),
        "resource": f"资源 {tid}".strip(),
        "trend": "趋势分析",
        "report": "整体日报",
    }.get(tt, f"{tt} {tid}".strip())


def _feedback_content(fb: dict[str, Any]) -> str:
    """构造 feedback_memory 的自然语言纠正句。"""
    corrected = fb.get("corrected_text") or ""
    note = fb.get("correction_note") or ""
    parts = [f"{_target_label(fb)} 的修正：{corrected}"]
    if note:
        parts.append(f"原因：{note}")
    return "。".join(parts)


def derive_lesson(fb: dict[str, Any]) -> str:
    """从 corrected_text 模板派生经验句（零 LLM）。供 group_experiences。"""
    corrected = fb.get("corrected_text") or ""
    return f"{_target_label(fb)}：{corrected}"


async def correct_memory_for_feedback(
    adapter: MemOSAdapterProtocol,
    db: aiosqlite.Connection,
    fb: dict[str, Any],
) -> dict[str, Any] | None:
    """对单条反馈执行 MemOS 纠正 + 回填溯源字段。

    Returns:
        ``{"cube_id", "node_id", "archived_id"}`` 或 None（无可纠正内容/失败）。
    """
    from z_winnow.pipeline.database import update_feedback_provenance

    group_id = fb.get("group_id", "")
    fid = fb.get("feedback_id", "")

    # 幂等：已有 memos_node_id 则跳过
    if fb.get("memos_node_id"):
        return {"cube_id": fb.get("memos_cube_id"), "node_id": fb.get("memos_node_id"),
                "archived_id": fb.get("archived_memos_id")}

    if not fb.get("corrected_text"):
        return None  # rating/tag-only 无 corrected_text，不纠正记忆

    target_type = fb.get("target_type", "")
    cube_id = cube_id_for_target(group_id, target_type)
    query = _search_query_for(fb)

    # 1. search 定位 node（按 target_id 标识匹配）
    retrieved: list = []
    if query:
        try:
            retrieved = await adapter.search_memories(
                query=query,
                group_id=group_id,
                readable_cube_ids=[cube_id],
                top_k=3,
            )
        except Exception as exc:
            logger.warning("feedback_corrector: search failed fb=%s — %s", fid, exc)
    retrieved_ids = [r.id for r in retrieved if getattr(r, "id", "")]

    # 2. feedback_memory（归档旧 + 写新）
    try:
        result = await adapter.feedback_memory(
            group_id=group_id,
            cube_ids=[cube_id],
            feedback_content=_feedback_content(fb),
            retrieved_memory_ids=retrieved_ids,
        )
    except Exception as exc:
        logger.warning("feedback_corrector: feedback_memory failed fb=%s — %s", fid, exc)
        return None

    if isinstance(result, dict) and result.get("status") in ("error", "disabled"):
        logger.info("feedback_corrector: feedback_memory non-ok fb=%s — %s", fid, result)
        return None

    new_ids = (result or {}).get("new_ids") or []
    archived_ids = (result or {}).get("archived_ids") or []
    node_id = new_ids[0] if isinstance(new_ids, list) and new_ids else None
    archived_id = archived_ids[0] if isinstance(archived_ids, list) and archived_ids else None

    # 3. 回填溯源
    await update_feedback_provenance(
        db,
        fid,
        memos_cube_id=cube_id,
        memos_node_id=node_id,
        archived_memos_id=archived_id,
    )
    return {"cube_id": cube_id, "node_id": node_id, "archived_id": archived_id}
