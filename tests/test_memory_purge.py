"""Tests for MemOS memory purge + group-deletion cascade.

Covers scenario ① (delete a group → cascade-clean its memories + local data)
and scenario ② (wipe-all). Uses MockMemOSAdapter + in-memory SQLite — no real
MemOS service required.
"""

from __future__ import annotations

import types

import aiosqlite
import pytest
from pydantic import ValidationError

from z_winnow.memory.mock_adapter import MockMemOSAdapter
from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.web.routes.memos import MemosWipeConfirm
from z_winnow.web.services.group_service import (
    delete_group,
    purge_group_local_data,
)
from z_winnow.web.services.memos_service import (
    delete_cube,
    purge_cube_memories,
    purge_group_memories,
    wipe_all_memories,
)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await init_database_in_conn(conn)
        yield conn


async def _seed_group(db, group_id="g1", display="Group One"):
    await db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id, is_active) VALUES (?, ?, ?, 1)",
        (group_id, display, f"{group_id}@chatroom"),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# purge_cube_memories
# ---------------------------------------------------------------------------


async def test_purge_cube_memories_removes_all_and_verifies():
    adapter = MockMemOSAdapter()
    cube = "winnow:g1:topics"
    await adapter.add_memory("g1", cube, [{"content": "m1"}, {"content": "m2"}, {"content": "m3"}])

    res = await purge_cube_memories(adapter, cube, group_id="g1")

    assert res["removed"] == 3
    assert res["verified_empty"] is True
    assert res["ok"] is True
    # store is actually empty now
    remaining = await adapter.get_all_memories(cube_id=cube, group_id="g1")
    assert remaining["text_mem"] == []


async def test_purge_cube_memories_already_empty_is_ok():
    adapter = MockMemOSAdapter()
    res = await purge_cube_memories(adapter, "winnow:g1:topics", group_id="g1")
    assert res["removed"] == 0
    assert res["verified_empty"] is True
    assert res["ok"] is True


async def test_purge_cube_memories_collects_usermemory_type():
    """F1: a feedback/UserMemory-tagged node is still collected + deleted.

    The mock filters by metadata.memory_type; we tag one node UserMemory and
    leave the default nodes untagged. purge must union both (default fetch
    returns untagged; the UserMemory fetch returns the tagged one).
    """
    adapter = MockMemOSAdapter()
    cube = "winnow:g1:feedback"
    await adapter.add_memory("g1", cube, [{"content": "topic-mem"}])
    # seed a UserMemory-tagged node directly
    adapter._store[cube]["g1"].append(
        {"id": "fb-1", "memory": "feedback-mem", "metadata": {"memory_type": "UserMemory"}}
    )

    res = await purge_cube_memories(adapter, cube, group_id="g1")

    assert res["removed"] == 2  # both the default + the UserMemory node
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# purge_group_memories / wipe_all_memories
# ---------------------------------------------------------------------------


async def test_purge_group_memories_hits_topics_and_feedback():
    adapter = MockMemOSAdapter()
    await adapter.add_memory("g1", "winnow:g1:topics", [{"content": "t1"}])
    await adapter.add_memory("g1", "winnow:g1:feedback", [{"content": "f1"}])

    res = await purge_group_memories(adapter, "g1")

    scopes = {c["cube_id"].split(":")[-1] for c in res["cubes"]}
    assert {"topics", "daily", "feedback"} <= scopes  # all known scopes visited
    assert res["total_removed"] == 2
    assert res["all_ok"] is True


async def test_wipe_all_memories_iterates_all_groups():
    adapter = MockMemOSAdapter()
    await adapter.add_memory("g1", "winnow:g1:topics", [{"content": "a"}])
    await adapter.add_memory("g2", "winnow:g2:topics", [{"content": "b"}, {"content": "c"}])

    res = await wipe_all_memories(adapter, ["g1", "g2"])

    assert res["groups"] == 2
    assert res["total_removed"] == 3
    assert res["all_ok"] is True


# ---------------------------------------------------------------------------
# delete_cube (single-cube API path) — uses group_id parsed from cube_id
# ---------------------------------------------------------------------------


async def test_delete_cube_extracts_group_id_and_clears():
    adapter = MockMemOSAdapter()
    cube = "winnow:g1:topics"
    await adapter.add_memory("g1", cube, [{"content": "x"}])

    ok = await delete_cube(adapter, cube)
    assert ok is True
    remaining = await adapter.get_all_memories(cube_id=cube, group_id="g1")
    assert remaining["text_mem"] == []


# ---------------------------------------------------------------------------
# Group-deletion cascade (scenario ①)
# ---------------------------------------------------------------------------


async def test_purge_group_local_data_clears_orphans_and_disk(tmp_path, monkeypatch):
    # Route disk cleanup at a tmp location
    db_file = tmp_path / "winnow.db"
    monkeypatch.setattr(
        "z_winnow.config.settings.get_settings",
        lambda: types.SimpleNamespace(db_path=str(db_file)),
    )

    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        await _seed_group(conn, "g1")
        await conn.execute(
            "INSERT INTO topic_summaries "
            "(summary_id, date, group_id, topic_name, summary_text, context_ids, source_server_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sum_1", "20260101", "g1", "Topic A", "{}", "[]", "[]"),
        )
        await conn.execute(
            "INSERT INTO memos_sync_queue (op_type, cube_id, payload) VALUES (?, ?, ?)",
            ("add_topic", "winnow:g1:topics", "{}"),
        )
        # an unrelated group's row must survive
        await conn.execute(
            "INSERT INTO memos_sync_queue (op_type, cube_id, payload) VALUES (?, ?, ?)",
            ("add_topic", "winnow:g2:topics", "{}"),
        )
        await conn.commit()

        # create the on-disk L3 dir for g1
        l3_dir = tmp_path / "processed" / "g1"
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text("{}", encoding="utf-8")
        assert l3_dir.exists()

        counts = await purge_group_local_data(conn, "g1")

        assert counts["topic_summaries"] == 1
        assert counts["memos_sync_queue"] == 1
        assert counts["disk_l3_removed"] == 1
        # orphans gone for g1, g2 untouched
        assert (
            await (
                await conn.execute("SELECT COUNT(*) FROM topic_summaries WHERE group_id='g1'")
            ).fetchone()
        )[0] == 0
        assert (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM memos_sync_queue WHERE cube_id LIKE 'winnow:g1:%'"
                )
            ).fetchone()
        )[0] == 0
        assert (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM memos_sync_queue WHERE cube_id LIKE 'winnow:g2:%'"
                )
            ).fetchone()
        )[0] == 1
        assert not l3_dir.exists()


async def test_delete_group_cascades_and_calls_memos_purge():
    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        await _seed_group(conn, "g1")

        adapter = MockMemOSAdapter()
        await adapter.add_memory("g1", "winnow:g1:topics", [{"content": "mem"}])

        deleted = await delete_group(conn, "g1", adapter=adapter)

        assert deleted is True
        assert (
            await (await conn.execute("SELECT COUNT(*) FROM groups WHERE group_id='g1'")).fetchone()
        )[0] == 0
        # MemOS memories purged
        remaining = await adapter.get_all_memories(cube_id="winnow:g1:topics", group_id="g1")
        assert remaining["text_mem"] == []


async def test_delete_group_not_blocked_when_memos_fails():
    """MemOS purge failure must NOT prevent group deletion."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        await _seed_group(conn, "g1")

        class _FailingAdapter(MockMemOSAdapter):
            async def get_or_create_cube(self, scope: str) -> str:
                raise RuntimeError("memos down")

        deleted = await delete_group(conn, "g1", adapter=_FailingAdapter())

        assert deleted is True  # group row still removed
        assert (
            await (await conn.execute("SELECT COUNT(*) FROM groups WHERE group_id='g1'")).fetchone()
        )[0] == 0


# ---------------------------------------------------------------------------
# DELETE /memos strong confirm validation (scenario ②)
# ---------------------------------------------------------------------------


def test_wipe_confirm_accepts_exact_token():
    body = MemosWipeConfirm(confirm="WIPE_ALL_MEMORIES")
    assert body.confirm == "WIPE_ALL_MEMORIES"


def test_wipe_confirm_rejects_wrong_token():
    # "true" (the old loose gate) must be rejected
    with pytest.raises(ValidationError):
        MemosWipeConfirm(confirm="true")
    with pytest.raises(ValidationError):
        MemosWipeConfirm(confirm="")
    with pytest.raises(ValidationError):
        MemosWipeConfirm(confirm="WIPE")


# ---------------------------------------------------------------------------
# feedback cube naming normalization (F3)
# ---------------------------------------------------------------------------


async def test_canonical_cube_names_are_colon_format():
    """Real adapter get_or_create_cube produces winnow:{scope} (production contract).

    purge_group_memories targets these literal ids. (The mock's get_or_create_cube
    returns an opaque UUID, so we verify the real adapter here.)
    """
    from z_winnow.memory.adapter import MemOSAdapter

    adapter = MemOSAdapter.__new__(MemOSAdapter)  # bypass __init__ — method is pure
    assert await adapter.get_or_create_cube("g1:topics") == "winnow:g1:topics"
    assert await adapter.get_or_create_cube("g1:feedback") == "winnow:g1:feedback"


def test_feedback_sync_source_uses_colon_not_hyphen():
    """F3: feedback_sync must build the feedback cube with colons, not hyphens."""
    import inspect

    from z_winnow.memory import feedback_sync

    src = inspect.getsource(feedback_sync)
    assert "winnow:" in src
    assert "winnow-{fb.group_id}-feedback" not in src.replace(" ", "")
    # the legacy hyphen literal must be gone
    assert "winnow-{fb.group_id}-feedback".replace(" ", "") not in src.replace(" ", "")
