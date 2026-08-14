"""Pipeline parallel safety tests — verifies multi-group isolation for fixes A1-A8.

Each test validates that group_id filtering correctly isolates data between groups,
preventing cross-contamination when multiple groups run concurrently.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from z_winnow.pipeline.database import (
    get_context_count,
    get_contexts_by_date,
    get_message_count,
    get_raw_messages_by_date,
    get_topic_count,
    get_topics_by_date,
    init_database_in_conn,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
async def db():
    """In-memory SQLite with schema initialized."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        yield conn


async def _seed_multi_group_data(db: aiosqlite.Connection) -> None:
    """Seed L1/L2/L3 data for two groups (g1, g2) on the same date."""
    date = "20260520"
    for gid, prefix in [("g1", "a"), ("g2", "b")]:
        # L1: 2 raw messages per group
        for i in range(1, 3):
            sid = f"{prefix}_msg_{i:03d}"
            await db.execute(
                """INSERT INTO raw_messages
                   (serverID, date, sender, content, msg_type, sanitized, raw_json, group_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    date,
                    f"user_{gid}_{i}",
                    f"Msg {gid} {i}",
                    "text",
                    0,
                    json.dumps({"server_id": sid}),
                    gid,
                ),
            )

        # L2: 1 parsed context per group
        await db.execute(
            """INSERT INTO parsed_contexts
               (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"ctx_{gid}",
                date,
                json.dumps([f"{prefix}_msg_001", f"{prefix}_msg_002"]),
                f"Context for {gid}",
                100,
                "test",
                gid,
            ),
        )

        # L3: 1 topic summary per group
        await db.execute(
            """INSERT INTO topic_summaries
               (summary_id, date, topic_name, summary_text, context_ids,
                source_server_ids, confidence, model_used, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"sum_{gid}",
                date,
                f"Topic {gid}",
                json.dumps({"summary": f"Summary {gid}"}),
                "[]",
                json.dumps([f"{prefix}_msg_001"]),
                0.9,
                "mock",
                gid,
            ),
        )

    await db.commit()


# ============================================================
# A2: group_id filter isolation
# ============================================================


class TestA2GroupIdFilter:
    """Verify SELECT queries correctly filter by group_id."""

    @pytest.mark.asyncio
    async def test_raw_messages_group_isolation(self, db):
        """get_raw_messages_by_date with group_id returns only that group's data."""
        await _seed_multi_group_data(db)

        g1_msgs = await get_raw_messages_by_date(db, "20260520", group_id="g1")
        g2_msgs = await get_raw_messages_by_date(db, "20260520", group_id="g2")
        all_msgs = await get_raw_messages_by_date(db, "20260520")

        assert len(g1_msgs) == 2
        assert len(g2_msgs) == 2
        assert len(all_msgs) == 4
        assert all(m["group_id"] == "g1" for m in g1_msgs)
        assert all(m["group_id"] == "g2" for m in g2_msgs)

    @pytest.mark.asyncio
    async def test_contexts_group_isolation(self, db):
        """get_contexts_by_date with group_id returns only that group's data."""
        await _seed_multi_group_data(db)

        g1_ctx = await get_contexts_by_date(db, "20260520", group_id="g1")
        g2_ctx = await get_contexts_by_date(db, "20260520", group_id="g2")
        all_ctx = await get_contexts_by_date(db, "20260520")

        assert len(g1_ctx) == 1
        assert len(g2_ctx) == 1
        assert len(all_ctx) == 2
        assert g1_ctx[0]["context_id"] == "ctx_g1"
        assert g2_ctx[0]["context_id"] == "ctx_g2"

    @pytest.mark.asyncio
    async def test_topics_group_isolation(self, db):
        """get_topics_by_date with group_id returns only that group's data."""
        await _seed_multi_group_data(db)

        g1_topics = await get_topics_by_date(db, "20260520", group_id="g1")
        g2_topics = await get_topics_by_date(db, "20260520", group_id="g2")
        all_topics = await get_topics_by_date(db, "20260520")

        assert len(g1_topics) == 1
        assert len(g2_topics) == 1
        assert len(all_topics) == 2
        assert g1_topics[0]["summary_id"] == "sum_g1"
        assert g2_topics[0]["summary_id"] == "sum_g2"

    @pytest.mark.asyncio
    async def test_message_count_group_filter(self, db):
        """get_message_count with group_id returns correct count per group."""
        await _seed_multi_group_data(db)

        assert await get_message_count(db, "20260520", group_id="g1") == 2
        assert await get_message_count(db, "20260520", group_id="g2") == 2
        assert await get_message_count(db, "20260520") == 4
        assert await get_message_count(db, group_id="g1") == 2
        assert await get_message_count(db) == 4

    @pytest.mark.asyncio
    async def test_context_count_group_filter(self, db):
        """get_context_count with group_id returns correct count."""
        await _seed_multi_group_data(db)

        assert await get_context_count(db, "20260520", group_id="g1") == 1
        assert await get_context_count(db, "20260520", group_id="g2") == 1
        assert await get_context_count(db, "20260520") == 2

    @pytest.mark.asyncio
    async def test_topic_count_group_filter(self, db):
        """get_topic_count with group_id returns correct count."""
        await _seed_multi_group_data(db)

        assert await get_topic_count(db, "20260520", group_id="g1") == 1
        assert await get_topic_count(db, "20260520", group_id="g2") == 1
        assert await get_topic_count(db, "20260520") == 2

    @pytest.mark.asyncio
    async def test_none_returns_all(self, db):
        """group_id=None returns all groups' data (backward compat)."""
        await _seed_multi_group_data(db)

        msgs = await get_raw_messages_by_date(db, "20260520", group_id=None)
        ctxs = await get_contexts_by_date(db, "20260520", group_id=None)
        topics = await get_topics_by_date(db, "20260520", group_id=None)

        assert len(msgs) == 4
        assert len(ctxs) == 2
        assert len(topics) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_group_returns_empty(self, db):
        """group_id for a group with no data returns empty results."""
        await _seed_multi_group_data(db)

        msgs = await get_raw_messages_by_date(db, "20260520", group_id="nonexistent")
        assert len(msgs) == 0
        assert await get_message_count(db, "20260520", group_id="nonexistent") == 0


# ============================================================
# A3: data_fetch cache/load group_id
# ============================================================


class TestA3DataFetchGroupIsolation:
    """Verify raw_messages group_id isolation in database layer."""

    @pytest.mark.asyncio
    async def test_count_scoped_by_group_id(self):
        """Counting raw_messages with group_id only counts that group's messages."""
        from z_winnow.pipeline.database import get_message_count

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            async with aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)
                for gid in ["g1", "g2"]:
                    await db.execute(
                        """INSERT INTO raw_messages
                           (serverID, date, sender, content, msg_type, sanitized, raw_json, group_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (f"msg_{gid}", "20260520", "u", "c", "text", 0, "{}", gid),
                    )
                await db.commit()

                g1_count = await get_message_count(db, "20260520", group_id="g1")
                g2_count = await get_message_count(db, "20260520", group_id="g2")
                assert g1_count == 1
                assert g2_count == 1

    @pytest.mark.asyncio
    async def test_load_scoped_by_group_id(self):
        """Loading raw_messages with group_id only loads that group's messages."""
        from z_winnow.pipeline.database import get_raw_messages_by_date

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            async with aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)
                for gid in ["g1", "g2"]:
                    await db.execute(
                        """INSERT INTO raw_messages
                           (serverID, date, sender, content, msg_type, sanitized, raw_json, group_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (f"msg_{gid}", "20260520", "u", "c", "text", 0, "{}", gid),
                    )
                await db.commit()

                g1_msgs = await get_raw_messages_by_date(db, "20260520", group_id="g1")
                all_msgs = await get_raw_messages_by_date(db, "20260520")

                assert len(g1_msgs) == 1
                assert len(all_msgs) == 2


# ============================================================
# A4: CipherTalk pagination
# ============================================================


class TestA4Pagination:
    """Verify CipherTalk client fetches all pages of messages."""

    @pytest.mark.asyncio
    async def test_single_page_no_loop(self):
        """When messages < page_size, no second fetch occurs."""
        from z_winnow.pipeline.cipher_talk_client import CipherTalkClient

        client = CipherTalkClient(base_url="http://fake", token="t")

        single_page = [{"id": i} for i in range(50)]
        with patch.object(client, "get_messages", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = single_page
            result = await client.fetch_messages("group", "20260520", chatroom_id="test@chatroom")
            assert mock_get.call_count == 1
            assert len(result) == 50

    @pytest.mark.asyncio
    async def test_multi_page(self):
        """When messages span multiple pages, all are fetched."""
        from z_winnow.pipeline.cipher_talk_client import CipherTalkClient

        client = CipherTalkClient(base_url="http://fake", token="t")

        page1 = [{"id": i, "content": f"msg{i}"} for i in range(1000)]
        page2 = [{"id": 1000 + i, "content": f"msg{1000 + i}"} for i in range(500)]

        with patch.object(client, "get_messages", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [page1, page2]
            result = await client.fetch_messages("group", "20260520", chatroom_id="test@chatroom")
            assert mock_get.call_count == 2
            assert len(result) == 1500

    @pytest.mark.asyncio
    async def test_max_total_cap(self):
        """Messages beyond max_total are truncated."""
        from z_winnow.pipeline.cipher_talk_client import CipherTalkClient

        client = CipherTalkClient(base_url="http://fake", token="t")

        pages = [[{"id": i} for i in range(p * 1000, (p + 1) * 1000)] for p in range(6)]

        with patch.object(client, "get_messages", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = pages
            result = await client.fetch_messages("group", "20260520", chatroom_id="test@chatroom")
            assert len(result) <= 5000


# ============================================================
# A5: Report path group_id isolation
# ============================================================


class TestA5ReportPath:
    """Verify report writer paths include group_id when provided."""

    def test_daily_path_without_group_id(self, tmp_path):
        """Without group_id, path is reports/daily/{date}.md."""
        from z_winnow.outputs.report_writer import write_daily_report

        path = write_daily_report("# Test", "20260520", output_dir=str(tmp_path))
        assert Path(path).parts[-2] == "daily"
        assert Path(path).name == "20260520.md"

    def test_daily_path_with_group_id(self, tmp_path):
        """With group_id, path is reports/daily/{group_id}/{date}.md."""
        from z_winnow.outputs.report_writer import write_daily_report

        path = write_daily_report(
            "# Test", "20260520", output_dir=str(tmp_path), group_id="my_group"
        )
        assert "my_group" in Path(path).parts
        assert Path(path).name == "20260520.md"

    def test_no_group_id_no_collision(self, tmp_path):
        """Two groups writing same date don't collide when group_id is used."""
        from z_winnow.outputs.report_writer import write_daily_report

        p1 = write_daily_report("# G1", "20260520", output_dir=str(tmp_path), group_id="g1")
        p2 = write_daily_report("# G2", "20260520", output_dir=str(tmp_path), group_id="g2")

        assert p1 != p2
        assert Path(p1).read_text(encoding="utf-8") == "# G1"
        assert Path(p2).read_text(encoding="utf-8") == "# G2"


# ============================================================
# A7: Image concurrency env resolution (None sentinel)
# ============================================================


class TestA7ConcurrencyEnvResolution:
    """Verify analyze_images_batch concurrency resolution contract.

    Contract (None-sentinel):
      - max_concurrency omitted (None) -> resolved from Settings.image_max_concurrency,
        which reads env IMAGE_MAX_CONCURRENCY / WINNOW_IMAGE_MAX_CONCURRENCY.
      - max_concurrency passed as int -> used verbatim; env is not consulted (P009).

    Verification is behavioral: the observed peak in-flight count of
    analyze_single_image across a batch larger than the configured limit must
    equal the configured limit.
    """

    @staticmethod
    def _image_messages(n: int) -> list[dict]:
        return [
            {
                "server_id": str(i),
                "msg_type": "image",
                "media_local_path": f"/tmp/img_{i}.png",
            }
            for i in range(n)
        ]

    @staticmethod
    def _make_peak_tracker(image_analyzer_mod):
        """Return (fake_analyze, state) tracking peak in-flight concurrency."""
        import asyncio

        state: dict[str, int] = {"in_flight": 0, "peak": 0}
        guard = asyncio.Lock()

        async def fake_analyze(_path: str):
            async with guard:
                state["in_flight"] += 1
                if state["in_flight"] > state["peak"]:
                    state["peak"] = state["in_flight"]
            await asyncio.sleep(0.05)
            async with guard:
                state["in_flight"] -= 1
            return image_analyzer_mod.ImageDescription(
                summary="s", description="d", image_type="other"
            )

        return fake_analyze, state

    async def test_omitted_reads_env(self, monkeypatch):
        """Omitting max_concurrency applies IMAGE_MAX_CONCURRENCY from env."""
        from z_winnow.config.settings import reset_settings
        from z_winnow.content_enrich import image_analyzer

        monkeypatch.setenv("WINNOW_IMAGE_MAX_CONCURRENCY", "3")
        reset_settings()
        fake, state = self._make_peak_tracker(image_analyzer)
        monkeypatch.setattr(image_analyzer, "analyze_single_image", fake)
        try:
            await image_analyzer.analyze_images_batch(self._image_messages(10))
        finally:
            reset_settings()

        assert state["peak"] == 3, f"expected peak concurrency 3 from env, got {state['peak']}"

    async def test_explicit_overrides_env(self, monkeypatch):
        """An explicit int argument takes precedence over env (P009)."""
        from z_winnow.config.settings import reset_settings
        from z_winnow.content_enrich import image_analyzer

        monkeypatch.setenv("WINNOW_IMAGE_MAX_CONCURRENCY", "3")
        reset_settings()
        fake, state = self._make_peak_tracker(image_analyzer)
        monkeypatch.setattr(image_analyzer, "analyze_single_image", fake)
        try:
            await image_analyzer.analyze_images_batch(self._image_messages(10), max_concurrency=2)
        finally:
            reset_settings()

        assert state["peak"] == 2, (
            f"expected peak concurrency 2 (explicit override), got {state['peak']}"
        )


# ============================================================
# A8: MemOS search no lock
# ============================================================


class TestA8SearchNoLock:
    """Verify search_memories does not hold a group lock."""

    def test_search_has_no_lock_acquire(self):
        """search_memories should not call _get_group_lock."""
        import inspect

        from z_winnow.memory.adapter import MemOSAdapter

        source = inspect.getsource(MemOSAdapter.search_memories)
        assert "_get_group_lock" not in source, (
            "search_memories should not acquire a group lock (read-only operation)"
        )

    def test_add_memory_has_lock(self):
        """add_memory should still use _get_group_lock (write operation)."""
        import inspect

        from z_winnow.memory.adapter import MemOSAdapter

        source = inspect.getsource(MemOSAdapter.add_memory)
        assert "_get_group_lock" in source, (
            "add_memory should retain its group lock (write operation)"
        )
