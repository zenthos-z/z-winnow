"""Smoke tests: validate every API endpoint through real lifespan + middleware.

Runs the actual FastAPI app with real lifespan (DB init, MemOS worker), real
middleware (auth + error handler), and file-based SQLite (required for SSE
which opens its own connection).  Covers 4 blind spots not tested by existing
unit tests:

  1. Lifespan startup/shutdown never exercised
  2. POST async tasks (runs/judge/memos) only tested with trivial coroutines
  3. SSE streaming only tested for headers, not real event content
  4. Middleware chain never tested through real app

Usage:
    python -m poetry run pytest tests/test_web_smoke.py -v
    python -m poetry run pytest tests/test_web_smoke.py -v -m integration
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate environment: mock mode, test env, no API key."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.setenv("WINNOW_ENV", "test")
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "")  # overridden per-fixture
    monkeypatch.setenv("WINNOW_DB_PATH", "")
    monkeypatch.setenv("WINNOW_REPORTS_DIR", "")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset Settings singleton so each test picks up monkeypatched env."""
    from z_winnow.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


# ============================================================
# Core fixtures: real app + real lifespan + file-based SQLite
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a FastAPI app with real lifespan, middleware, and routes.

    Uses file-based SQLite in tmp_path so SSE (which opens its own
    connection) can see the same data.
    """
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.middleware import (
        ApiKeyMiddleware,
        ErrorHandlerMiddleware,
    )
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "smoke.db")
    reports_dir = str(tmp_path / "reports")
    Path(reports_dir).mkdir(exist_ok=True)

    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
    reset_settings()

    fresh = FastAPI(title="smoke-test", lifespan=lifespan)
    # Middleware order: outermost first = added last in FastAPI
    if ErrorHandlerMiddleware is not None:
        fresh.add_middleware(ErrorHandlerMiddleware)
    if ApiKeyMiddleware is not None:
        fresh.add_middleware(ApiKeyMiddleware)
    fresh.include_router(api_router)
    return fresh


@pytest.fixture
async def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield a running app with real lifespan active."""
    from z_winnow.web.app import lifespan

    app = _build_app(tmp_path, monkeypatch)
    async with lifespan(app):
        yield app


@pytest.fixture
async def client(real_app: FastAPI):
    """httpx client wired to the real app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
async def seeded_db(real_app: FastAPI):
    """Seed all tables with comprehensive test data."""
    conn: aiosqlite.Connection = real_app.state.db_conn
    await _seed_groups(conn)
    await _seed_members(conn)
    await _seed_raw_messages(conn, count=15, group_id="g_active_1", date="20260601")
    await _seed_core_topics(conn)
    await _seed_topic_summaries(conn)
    await _seed_l1_l2_l3_chain(conn)
    await _seed_feedback_events(conn)
    await _seed_pipeline_runs(conn)
    await _seed_report_versions(conn)
    yield conn


# ============================================================
# Seed helpers (adapted from test_web_services.py)
# ============================================================


async def _seed_raw_messages(
    db: aiosqlite.Connection, count: int, group_id: str = "", date: str = "20260601"
) -> None:
    """Seed raw_messages with deterministic data (3 rotating senders)."""
    for i in range(count):
        sender = f"sender_{i % 3}"
        await db.execute(
            "INSERT INTO raw_messages "
            "(serverID, date, group_id, sender, content, msg_type, raw_json) "
            "VALUES (?, ?, ?, ?, ?, 'text', '{}')",
            (f"srv_{i:04d}", date, group_id, sender, f"Message {i}"),
        )
    await db.commit()


async def _seed_groups(db: aiosqlite.Connection) -> None:
    """Seed groups: 3 active, 2 inactive."""
    for gid, name, chatroom, active in [
        ("g_active_1", "Test Group Alpha", "room1@chatroom", 1),
        ("g_active_2", "Development Team", "room2@chatroom", 1),
        ("g_active_3", "Test Beta Group", "room3@chatroom", 1),
        ("g_inactive_1", "Archived Group", "room4@chatroom", 0),
        ("g_inactive_2", "Old Team", "room5@chatroom", 0),
    ]:
        await db.execute(
            "INSERT INTO groups (group_id, display_name, chatroom_id, is_active) "
            "VALUES (?, ?, ?, ?)",
            (gid, name, chatroom, active),
        )
    await db.commit()


async def _seed_members(db: aiosqlite.Connection) -> None:
    """Seed group_members for g_active_1."""
    for mid, gid, name, role, active in [
        ("gm-1", "g_active_1", "Alice", "admin", 1),
        ("gm-2", "g_active_1", "Bob", "member", 1),
        ("gm-3", "g_active_1", "Charlie", "viewer", 0),
    ]:
        await db.execute(
            "INSERT INTO group_members (member_id, group_id, name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, gid, name, role, active),
        )
    await db.commit()


async def _seed_core_topics(db: aiosqlite.Connection) -> None:
    """Seed core_topics for g_active_1."""
    for tid, gid, name, priority, active in [
        ("core-1", "g_active_1", "API Design", 1, 1),
        ("core-2", "g_active_1", "Performance", 2, 1),
        ("core-3", "g_active_1", "Legacy Topic", 1, 0),
    ]:
        await db.execute(
            "INSERT INTO core_topics "
            "(core_topic_id, group_id, name, priority, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, gid, name, priority, active),
        )
    await db.commit()


async def _seed_topic_summaries(db: aiosqlite.Connection) -> None:
    """Seed topic_summaries with different lifecycle values."""
    for sid, date, gid, name, lifecycle in [
        ("sum_001", "20260601", "g_test", "Topic A", "emerging"),
        ("sum_002", "20260601", "g_test", "Topic B", "active"),
        ("sum_003", "20260601", "g_test", "Topic C", "declining"),
        ("sum_004", "20260602", "g_test", "Topic D", "active"),
        ("sum_005", "20260601", "g_other", "Topic E", "active"),
    ]:
        await db.execute(
            "INSERT INTO topic_summaries "
            "(summary_id, date, group_id, topic_name, summary_text, "
            "context_ids, source_server_ids, lifecycle) "
            "VALUES (?, ?, ?, ?, ?, '[]', '[]', ?)",
            (sid, date, gid, name, f"Summary for {name}", lifecycle),
        )
    await db.commit()


async def _seed_l1_l2_l3_chain(db: aiosqlite.Connection) -> None:
    """Seed a full L1 -> L2 -> L3 provenance chain."""
    # L1
    for args in [
        ("sid-100", "20260601", "grp-chain", "Alice", "Hello world", "{}"),
        ("sid-101", "20260601", "grp-chain", "Bob", "Good morning", "{}"),
        ("sid-102", "20260601", "grp-chain", "Charlie", "Hi there", "{}"),
        ("sid-103", "20260602", "grp-chain", "Dave", "Bye", "{}"),
    ]:
        await db.execute(
            "INSERT INTO raw_messages (serverID, date, group_id, sender, content, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            args,
        )
    # L2
    await db.execute(
        "INSERT INTO parsed_contexts (context_id, date, group_id, server_ids, context_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "ctx-100",
            "20260601",
            "grp-chain",
            json.dumps(["sid-100", "sid-101"]),
            "Alice and Bob discussed greetings.",
        ),
    )
    # L3
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, summary_text, "
        "context_ids, source_server_ids, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ts-100",
            "20260601",
            "grp-chain",
            "Greetings",
            "Morning greetings exchanged.",
            json.dumps(["ctx-100"]),
            json.dumps(["sid-100", "sid-101"]),
            0.95,
        ),
    )
    await db.commit()


async def _seed_feedback_events(db: aiosqlite.Connection) -> None:
    """Seed feedback_events."""
    await db.execute(
        "INSERT INTO feedback_events "
        "(feedback_id, group_id, date, target_type, signal, reporter) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("fb-chain-001", "grp-chain", "20260601", "topic", "positive", "admin"),
    )
    await db.commit()


async def _seed_pipeline_runs(db: aiosqlite.Connection) -> None:
    """Seed pipeline_runs."""
    await db.execute(
        "INSERT INTO pipeline_runs (run_id, component, status, group_id, date) "
        "VALUES (?, ?, ?, ?, ?)",
        ("run-chain-001", "pipeline", "completed", "grp-chain", "20260601"),
    )
    await db.commit()


async def _seed_report_versions(db: aiosqlite.Connection) -> None:
    """Seed report_versions via the production create_version function."""
    from z_winnow.pipeline.report_version import create_version

    await create_version(
        db,
        report_id="rpt-001",
        group_id="g_active_1",
        date="20260601",
        content="# Test Report\n\nHello world.",
    )


# ============================================================
# Test Classes
# ============================================================


class TestHealthAndSystem:
    """GET /health, GET /system/info, GET /system/config — no DB dependency."""

    async def test_health_check(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert "database" in body

    async def test_system_info(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/system/info")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"

    async def test_system_config_masks_secrets(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/system/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "db_path" in body
        assert "log_level" in body
        assert isinstance(body.get("features"), dict)
        # Sensitive keys must be masked or absent
        for key in ("anthropic_api_key", "deepseek_api_key", "openai_api_key"):
            assert key not in body, f"Secret key {key} should not appear in config"


class TestOverviewAndReads:
    """GET /overview, GET /groups — reads from SQLite."""

    async def test_overview_empty(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_messages"] == 0
        assert body["total_groups"] == 0

    async def test_overview_seeded(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_messages"] == 19  # 15 from _seed_raw_messages + 4 from L1-L2-L3
        assert body["total_groups"] == 3  # active only

    async def test_list_groups_default(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/groups")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3  # active only by default
        assert len(body["items"]) == 3
        item = body["items"][0]
        assert "group_id" in item
        assert "display_name" in item
        assert "chatroom_id" in item

    async def test_list_groups_pagination(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/groups?page=1&page_size=2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["page"] == 1

    async def test_list_groups_search(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/groups?search=test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2  # "Test Group Alpha" + "Test Beta Group"
        for item in body["items"]:
            assert "test" in item["display_name"].lower()

    async def test_get_group_by_id(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/groups/g_active_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Test Group Alpha"
        assert body["chatroom_id"] == "room1@chatroom"


class TestKeyPeople:
    """GET /key-people — requires group_id + date query params."""

    async def test_key_people_seeded(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        conn = real_app.state.db_conn
        # P1-3: list_key_people reads group_members (LEFT JOIN raw_messages), so the
        # group and its members must exist — raw senders alone no longer surface.
        await conn.execute(
            "INSERT OR IGNORE INTO groups (group_id, display_name, chatroom_id) "
            "VALUES ('g_test', 'Test', 'room_test@chatroom')"
        )
        for mid, wxid in [("gm-0", "sender_0"), ("gm-1", "sender_1"), ("gm-2", "sender_2")]:
            await conn.execute(
                "INSERT INTO group_members (member_id, group_id, name, wxid, role, is_active) "
                "VALUES (?, 'g_test', ?, ?, 'member', 1)",
                (mid, wxid, wxid),
            )
        await conn.commit()
        await _seed_raw_messages(conn, count=10, group_id="g_test", date="20260601")

        resp = await client.get(
            "/api/v1/key-people", params={"group_id": "g_test", "date": "20260601"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 3  # 3 distinct senders
        # Sorted by message_count descending
        assert body[0]["message_count"] >= body[1]["message_count"]

    async def test_key_people_empty(self, client: httpx.AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/key-people",
            params={"group_id": "nonexistent", "date": "20260601"},
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestCoreTopics:
    """GET /core-topics — requires group_id query param."""

    async def test_core_topics_active_only(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        conn = real_app.state.db_conn
        await _seed_groups(conn)
        await _seed_core_topics(conn)

        resp = await client.get("/api/v1/core-topics", params={"group_id": "g_active_1"})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2  # active only (core-1, core-2)
        for item in body:
            assert "core_topic_id" in item
            assert "name" in item

    async def test_core_topics_all(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        conn = real_app.state.db_conn
        await _seed_groups(conn)
        await _seed_core_topics(conn)

        resp = await client.get(
            "/api/v1/core-topics",
            params={"group_id": "g_active_1", "is_active": "false"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3  # all including inactive


class TestReports:
    """GET /reports — paginated report versions."""

    async def test_list_reports_empty(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/reports")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_list_reports_seeded(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/reports")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        item = body["items"][0]
        assert "version_id" in item
        assert "report_id" in item
        assert "group_id" in item

    async def test_get_report_by_id(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        # First list to get a valid version_id
        list_resp = await client.get("/api/v1/reports")
        version_id = list_resp.json()["items"][0]["version_id"]

        resp = await client.get(f"/api/v1/reports/{version_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version_id"] == version_id


class TestRuns:
    """GET /runs, GET /runs/{run_id} — pipeline run listing."""

    async def test_list_runs(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1
        assert body[0]["run_id"] == "run-chain-001"

    async def test_get_run_by_id(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/runs/run-chain-001")
        assert resp.status_code == 200

    async def test_get_run_not_found(self, client: httpx.AsyncClient) -> None:
        """GET /runs/{id} with nonexistent ID returns 404."""
        resp = await client.get("/api/v1/runs/nonexistent")
        assert resp.status_code == 404


class TestFeedback:
    """GET /feedback — unconsumed feedback events."""

    async def test_list_feedback_seeded(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get(
            "/api/v1/feedback",
            params={"group_id": "grp-chain", "date": "20260601"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1
        assert body[0]["feedback_id"] == "fb-chain-001"

    async def test_list_feedback_empty(self, client: httpx.AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/feedback",
            params={"group_id": "nonexistent", "date": "20260601"},
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestDataLayers:
    """GET /data/{layer}/{group_id}/{date} — L1/L2/L3 provenance data."""

    async def test_data_l1(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/data/l1/grp-chain/20260601")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 3  # sid-100, sid-101, sid-102

    async def test_data_l2(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        """GET /data/l2 returns real parsed_contexts for the group+date."""
        resp = await client.get("/api/v1/data/l2/grp-chain/20260601")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] >= 1  # seeded chain includes ctx-100
        assert any(it["context_id"] == "ctx-100" for it in body["items"])

    async def test_data_l3(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.get("/api/v1/data/l3/grp-chain/20260601")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1

    async def test_data_invalid_layer(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/data/l4/grp-chain/20260601")
        assert resp.status_code == 400


class TestMemos:
    """GET /memos/status — stub endpoint."""

    async def test_memos_status(self, client: httpx.AsyncClient) -> None:
        # STUB: hardcoded response
        resp = await client.get("/api/v1/memos/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body


# ============================================================
# Write endpoints (CRUD)
# ============================================================


class TestGroupCRUD:
    """POST/PUT/DELETE /groups — requires auth when key configured."""

    async def test_create_group(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        resp = await client.post(
            "/api/v1/groups",
            json={
                "display_name": "New Group",
                "chatroom_id": "room_new@chatroom",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["display_name"] == "New Group"
        assert body["chatroom_id"] == "room_new@chatroom"
        assert body["is_active"] == 1

    async def test_update_group(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.put(
            "/api/v1/groups/g_active_1",
            json={"display_name": "Updated Alpha"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Updated Alpha"
        assert body["group_id"] == "g_active_1"

    async def test_delete_group(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        resp = await client.delete("/api/v1/groups/g_active_1")
        assert resp.status_code == 204

        # Verify deleted — follow-up GET should return 404
        resp2 = await client.get("/api/v1/groups/g_active_1")
        assert resp2.status_code == 404

    async def test_create_group_missing_fields(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/api/v1/groups", json={"display_name": ""})
        assert resp.status_code == 422

    async def test_update_nonexistent_group(self, client: httpx.AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/groups/nonexistent",
            json={"display_name": "Ghost"},
        )
        assert resp.status_code == 404


class TestCoreTopicCRUD:
    """POST/PUT/DELETE /core-topics."""

    async def test_create_core_topic(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        await _seed_groups(real_app.state.db_conn)

        resp = await client.post(
            "/api/v1/core-topics",
            json={"group_id": "g_active_1", "name": "New Topic"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "New Topic"
        assert "core_topic_id" in body

    async def test_update_core_topic(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        await _seed_groups(real_app.state.db_conn)
        await _seed_core_topics(real_app.state.db_conn)

        resp = await client.put(
            "/api/v1/core-topics/core-1",
            json={"name": "Updated API Design"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated API Design"

    async def test_delete_core_topic(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        await _seed_groups(real_app.state.db_conn)
        await _seed_core_topics(real_app.state.db_conn)

        resp = await client.delete("/api/v1/core-topics/core-1")
        assert resp.status_code == 204

    async def test_delete_nonexistent_topic(self, client: httpx.AsyncClient) -> None:
        resp = await client.delete("/api/v1/core-topics/nonexistent")
        assert resp.status_code == 404


# ============================================================
# Stub endpoints — track shape until real implementation
# ============================================================


class TestWriteStubs:
    """Endpoints that return inline construction without DB writes."""

    async def test_post_key_people_stub(self, client: httpx.AsyncClient) -> None:
        # STUB: POST /key-people — inline construction, no DB write
        # BUG: route signature `body: Any` is interpreted as query param by FastAPI,
        # so sending JSON body returns 422. Should be `body: dict = Body(...)`
        # or use `request.json()`. Documenting current behavior.
        resp = await client.post(
            "/api/v1/key-people",
            json={"sender": "alice"},
        )
        assert resp.status_code == 422  # known bug — should be 201

    async def test_post_feedback_stub(self, client: httpx.AsyncClient) -> None:
        # STUB: POST /feedback — inline construction, no DB write
        resp = await client.post(
            "/api/v1/feedback",
            json={
                "group_id": "g1",
                "date": "2026-06-01",
                "target_type": "topic",
                "signal": "positive",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "feedback_id" in body
        assert body["signal"] == "positive"


class TestStubIdentification:
    """Verify known stub endpoints return expected shapes.

    If these tests fail because someone replaced a stub with real logic,
    update the test to validate the real behavior instead.
    """

    async def test_data_l2_real(
        self, client: httpx.AsyncClient, seeded_db: aiosqlite.Connection
    ) -> None:
        """GET /data/l2 now returns real parsed_contexts (no longer a stub)."""
        resp = await client.get("/api/v1/data/l2/grp-chain/20260601")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "page" in body
        assert body["total"] >= 1

    async def test_stub_memos_status(self, client: httpx.AsyncClient) -> None:
        # STUB: GET /memos/status — hardcoded response
        resp = await client.get("/api/v1/memos/status")
        assert resp.status_code == 200

    async def test_stub_memos_search(self, client: httpx.AsyncClient) -> None:
        # STUB: POST /memos/search — hardcoded empty response
        resp = await client.post(
            "/api/v1/memos/search",
            json={"query": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("total") == 0
        assert body.get("results") == []

    async def test_stub_key_people_post_shape(self, client: httpx.AsyncClient) -> None:
        # STUB: POST /key-people returns inline shape
        # BUG: same as test_post_key_people_stub — body: Any treated as query
        resp = await client.post(
            "/api/v1/key-people",
            json={"sender": "bob"},
        )
        assert resp.status_code == 422  # known bug — should be 201


# ============================================================
# Async task lifecycle — POST 202 → poll until done
# ============================================================


class TestAsyncTasks:
    """POST /runs, /judge, /memos/rebuild — 202 + background execution."""

    async def test_post_run_returns_202(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/runs",
            json={
                "component": "pipeline",
                "group_id": "g_test",
                "date": "2026-06-01",
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body
        assert body["task_id"]  # non-empty UUID

    async def test_run_task_lifecycle(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        """POST /runs returns 202, then task transitions to done."""
        from z_winnow.web.services.task_queue import get_task_status

        resp = await client.post(
            "/api/v1/runs",
            json={"component": "pipeline", "group_id": "g_active_1", "date": "2026-06-01"},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Poll until done (background task via asyncio.create_task)
        status = None
        for _ in range(20):
            await asyncio.sleep(0.2)
            status = await get_task_status(task_id, db_path=real_app.state.db_path)
            if status and status["status"] in ("done", "failed"):
                break
        assert status is not None, "Task never appeared in DB"
        assert status["status"] == "done", f"Task failed: {status.get('error_message')}"

    async def test_post_judge_returns_202(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/judge",
            json={"report_id": "rpt-001"},
        )
        assert resp.status_code == 202
        assert "task_id" in resp.json()

    async def test_post_memos_rebuild_returns_202(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/memos/cubes/test-cube/rebuild", params={"group": "g_test"}
        )
        assert resp.status_code == 202
        assert "task_id" in resp.json()


# ============================================================
# SSE streaming — real event format validation
# ============================================================


@pytest.mark.slow
class TestSSEStream:
    """GET /runs/stream — SSE with real streaming from file-based SQLite.

    The route hardcodes poll_interval_s=2.0 and max_iterations=300.
    We patch stream_runs to yield exactly 1 event so the test completes
    quickly. Marked slow because the service still has a brief poll delay.
    """

    async def test_sse_produces_events(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        """SSE stream yields properly formatted events."""
        # Seed a running-status run for non-trivial data
        conn: aiosqlite.Connection = real_app.state.db_conn
        await conn.execute(
            "INSERT INTO pipeline_runs (run_id, component, status, group_id, date) "
            "VALUES (?, 'pipeline', 'running', 'g_sse', '20260601')",
            ("run-sse-001",),
        )
        await conn.commit()

        # Patch stream_runs to yield only 1 event (avoid infinite loop in test)
        from z_winnow.web.services import run_service

        original = run_service.stream_runs

        async def _one_shot_stream(db_path, *, poll_interval_s=0.01, max_iterations=1):
            async for event in original(db_path, poll_interval_s=0.01, max_iterations=1):
                yield event

        with patch.object(run_service, "stream_runs", _one_shot_stream):
            resp = await client.get(
                "/api/v1/runs/stream",
                headers={"Accept": "text/event-stream"},
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            assert resp.headers.get("x-accel-buffering") == "no"

            text = resp.text
            assert "data: " in text
            event_data = text.split("data: ", 1)[1].split("\n\n")[0]
            payload = json.loads(event_data)
            assert "runs" in payload
            assert isinstance(payload["runs"], list)

    async def test_sse_empty_db(self, client: httpx.AsyncClient) -> None:
        """SSE stream works even with no runs in DB."""
        from z_winnow.web.services import run_service

        original = run_service.stream_runs

        async def _one_shot_stream(db_path, *, poll_interval_s=0.01, max_iterations=1):
            async for event in original(db_path, poll_interval_s=0.01, max_iterations=1):
                yield event

        with patch.object(run_service, "stream_runs", _one_shot_stream):
            resp = await client.get("/api/v1/runs/stream")
            assert resp.status_code == 200
            text = resp.text
            assert "data: " in text
            payload = json.loads(text.split("data: ", 1)[1].split("\n\n")[0])
            assert "runs" in payload


# ============================================================
# Middleware auth — through real middleware chain
# ============================================================


class TestMiddlewareAuth:
    """Validate ApiKeyMiddleware through real app."""

    async def test_auth_rejects_post_without_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST without key returns 401 when key is configured."""
        from z_winnow.config.settings import reset_settings

        monkeypatch.setenv("WINNOW_WEB_API_KEY", "secret-123")
        reset_settings()

        app = _build_app(tmp_path, monkeypatch)
        from z_winnow.web.app import lifespan

        async with (
            lifespan(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as c,
        ):
            resp = await c.post(
                "/api/v1/groups",
                json={"display_name": "Blocked", "chatroom_id": "x@chatroom"},
            )
            assert resp.status_code == 401
            assert resp.json()["error"] == "AuthenticationError"

    async def test_auth_accepts_post_with_correct_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST with correct key succeeds."""
        from z_winnow.config.settings import reset_settings

        monkeypatch.setenv("WINNOW_WEB_API_KEY", "secret-123")
        reset_settings()

        app = _build_app(tmp_path, monkeypatch)
        from z_winnow.web.app import lifespan

        async with (
            lifespan(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as c,
        ):
            resp = await c.post(
                "/api/v1/groups",
                json={"display_name": "Allowed", "chatroom_id": "y@chatroom"},
                headers={"X-API-Key": "secret-123"},
            )
            assert resp.status_code == 201

    async def test_auth_allows_get_without_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET bypasses auth even when key is configured."""
        from z_winnow.config.settings import reset_settings

        monkeypatch.setenv("WINNOW_WEB_API_KEY", "secret-123")
        reset_settings()

        app = _build_app(tmp_path, monkeypatch)
        from z_winnow.web.app import lifespan

        async with (
            lifespan(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as c,
        ):
            resp = await c.get("/api/v1/health")
            assert resp.status_code == 200


# ============================================================
# Error paths — through real middleware
# ============================================================


class TestErrorPaths:
    """Validate error handling through real ErrorHandlerMiddleware."""

    async def test_404_get_group(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/groups/nonexistent-id")
        assert resp.status_code == 404

    async def test_404_get_report(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/v1/reports/nonexistent-id")
        assert resp.status_code == 404

    async def test_422_create_group_bad_data(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/api/v1/groups", json={})
        assert resp.status_code == 422

    async def test_404_delete_nonexistent_group(self, client: httpx.AsyncClient) -> None:
        resp = await client.delete("/api/v1/groups/nonexistent")
        assert resp.status_code == 404
