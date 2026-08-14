"""W15-P1-DATA: Data stats, provenance chain, and L1 message detail tests.

Tests verify (P011: 1:1 AC mapping):
  H1: All tests pass (0 failures)
  B1: GET /data/stats without filters -> 200 with counts across all 3 layers
  B2: GET /data/provenance/{server_id} linked to 2 topics -> 200 with message + topics
  B3: GET /data/l1/{gid}/{date}/detail/{sid} -> 200 with L2 context blocks
  B4: GET /data/provenance/nonexistent -> 404; GET /data/l1/.../detail/nonexistent -> 404

P078: Real SQLite :memory: databases — seed data, query, verify. No mock DB.
P050: All SQL uses parameterized ? placeholders (tested indirectly via service impl).
A008: Service functions init result vars before try blocks.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import aiosqlite
import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

# L070: Import data router directly to bypass pre-existing import errors
# in other route modules (memos.py) under parallel build.
_data_path = (
    Path(__file__).resolve().parent.parent / "src" / "z_winnow" / "web" / "routes" / "data.py"
)
_data_spec = importlib.util.spec_from_file_location("data_route_module", str(_data_path))
assert _data_spec is not None, f"Could not find module spec for {_data_path}"
assert _data_spec.loader is not None, f"Loader is None for {_data_path}"
_data_mod = importlib.util.module_from_spec(_data_spec)
_data_spec.loader.exec_module(_data_mod)
data_router = _data_mod.router

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create a test FastAPI app with the data router included.

    Uses data_router directly (not api_router) to avoid pre-existing
    import errors in other route modules under parallel build.
    """
    _app = FastAPI()
    # Mount under /api/v1 prefix matching production
    _api = APIRouter(prefix="/api/v1")
    _api.include_router(data_router)
    _app.include_router(_api)
    return _app


@pytest.fixture
async def db_conn():
    """Provide an in-memory SQLite connection with all required tables.

    P078: Real SQLite :memory: — no mock cursors or fake connections.
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS raw_messages (
            serverID TEXT PRIMARY KEY,
            date TEXT,
            group_id TEXT,
            sender TEXT,
            content TEXT,
            msg_type TEXT DEFAULT 'text',
            image_path TEXT,
            sanitized INTEGER DEFAULT 0,
            raw_json TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS parsed_contexts (
            context_id TEXT PRIMARY KEY,
            date TEXT,
            group_id TEXT,
            server_ids TEXT,
            context_text TEXT,
            token_count INTEGER,
            source_subagent TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS topic_summaries (
            summary_id TEXT PRIMARY KEY,
            date TEXT,
            group_id TEXT,
            topic_name TEXT,
            topic_id TEXT,
            summary_text TEXT,
            context_ids TEXT,
            source_server_ids TEXT,
            confidence REAL,
            model_used TEXT,
            lifecycle TEXT DEFAULT 'emerging',
            matched_core_topic_id TEXT,
            conclusion TEXT,
            description TEXT,
            participants TEXT,
            trend TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS report_versions (
            version_id TEXT PRIMARY KEY,
            report_id TEXT,
            group_id TEXT,
            date TEXT,
            version_number INTEGER,
            content TEXT,
            content_changed INTEGER DEFAULT 0,
            source TEXT,
            build_duration_s REAL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        );
    """)
    yield conn
    await conn.close()


@pytest.fixture
async def client(app: FastAPI, db_conn: aiosqlite.Connection):
    """Async test client with db_conn and db_path on app.state."""
    app.state.db_conn = db_conn
    app.state.db_path = ":memory:"
    app.state.reports_dir = "."

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Seed helpers
# ============================================================


async def seed_data_stats_scenario(db: aiosqlite.Connection) -> None:
    """Seed minimal data across all 3 layers for stats testing.

    Inserts messages, contexts, topics, and report_versions
    so aggregate counts are non-zero.
    """
    # 3 messages in 2 groups
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-001', '20260601', 'grp-a', 'Alice', 'Hello')"
    )
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-002', '20260601', 'grp-a', 'Bob', 'Hi')"
    )
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-003', '20260602', 'grp-b', 'Carol', 'Hey')"
    )

    # 2 contexts (1 unused)
    await db.execute(
        "INSERT INTO parsed_contexts (context_id, date, group_id, server_ids, context_text) "
        "VALUES ('ctx-001', '20260601', 'grp-a', '[\"sid-001\", \"sid-002\"]', 'Context block A')"
    )
    await db.execute(
        "INSERT INTO parsed_contexts (context_id, date, group_id, server_ids, context_text) "
        "VALUES ('ctx-002', '20260602', 'grp-b', '[\"sid-003\"]', 'Context block B')"
    )

    # 2 topic summaries
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-001', '20260601', 'grp-a', 'Topic Alpha', 't-alpha', "
        " 'summary text A', '[\"ctx-001\"]', '[\"sid-001\", \"sid-002\"]', 'emerging')"
    )
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-002', '20260602', 'grp-b', 'Topic Beta', 't-beta', "
        " 'summary text B', '[\"ctx-002\"]', '[\"sid-003\"]', 'active')"
    )

    # 2 report versions
    await db.execute(
        "INSERT INTO report_versions (version_id, report_id, group_id, date, "
        " version_number, content) "
        "VALUES ('ver-001', 'rpt-001', 'grp-a', '20260601', 1, 'report content A')"
    )
    await db.execute(
        "INSERT INTO report_versions (version_id, report_id, group_id, date, "
        " version_number, content) "
        "VALUES ('ver-002', 'rpt-002', 'grp-b', '20260602', 1, 'report content B')"
    )

    await db.commit()


async def seed_provenance_scenario(db: aiosqlite.Connection) -> None:
    """Seed data for provenance chain: 1 message linked to 2 topics."""
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-prove', '20260601', 'grp-p', 'Dave', 'Provenance test message')"
    )
    await db.execute(
        "INSERT INTO parsed_contexts (context_id, date, group_id, server_ids, context_text) "
        "VALUES ('ctx-prove', '20260601', 'grp-p', "
        " '[\"sid-prove\"]', 'Parsed context for provenance')"
    )
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-prove-1', '20260601', 'grp-p', 'Topic 1', 't-1', "
        " 'summary 1', '[\"ctx-prove\"]', '[\"sid-prove\"]', 'emerging')"
    )
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-prove-2', '20260601', 'grp-p', 'Topic 2', 't-2', "
        " 'summary 2', '[\"ctx-prove\"]', '[\"sid-prove\"]', 'active')"
    )
    await db.commit()


async def seed_l1_detail_scenario(db: aiosqlite.Connection) -> None:
    """Seed data for L1 detail: message with linked context and topic."""
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-detail', '20260601', 'grp-d', 'Eve', 'Detail test message')"
    )
    await db.execute(
        "INSERT INTO parsed_contexts (context_id, date, group_id, server_ids, context_text) "
        "VALUES ('ctx-detail', '20260601', 'grp-d', "
        " '[\"sid-detail\"]', 'Detail context block')"
    )
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-detail', '20260601', 'grp-d', 'Detail Topic', 't-d', "
        " 'detail summary', '[\"ctx-detail\"]', '[\"sid-detail\"]', 'emerging')"
    )
    await db.commit()


# ============================================================
# H1/B1: Data stats endpoint
# ============================================================


class TestDataStatsEndpoint:
    """B1: GET /data/stats returns correct aggregate counts."""

    @pytest.mark.asyncio
    async def test_stats_without_filters(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B1: GET /data/stats without filters -> 200 DataStatsOut with correct counts."""
        await seed_data_stats_scenario(db_conn)

        resp = await client.get("/api/v1/data/stats")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert body["total_messages"] == 3, f"Expected 3 messages, got {body['total_messages']}"
        assert body["total_groups"] == 2, f"Expected 2 groups, got {body['total_groups']}"
        assert body["total_topics"] == 2, f"Expected 2 topics, got {body['total_topics']}"
        assert body["total_reports"] == 2, f"Expected 2 reports, got {body['total_reports']}"
        assert body["date_range_start"] == "20260601"
        assert body["date_range_end"] == "20260602"

    @pytest.mark.asyncio
    async def test_stats_with_group_filter(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B1: GET /data/stats?group_id=grp-a -> filtered counts for one group."""
        await seed_data_stats_scenario(db_conn)

        resp = await client.get("/api/v1/data/stats", params={"group_id": "grp-a"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["total_messages"] == 2, (
            f"Expected 2 msgs for grp-a, got {body['total_messages']}"
        )
        assert body["total_groups"] == 1
        assert body["total_topics"] == 1
        assert body["total_reports"] == 1

    @pytest.mark.asyncio
    async def test_stats_with_date_filter(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B1: GET /data/stats?date=20260601 -> filtered counts for one date."""
        await seed_data_stats_scenario(db_conn)

        resp = await client.get("/api/v1/data/stats", params={"date": "20260601"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["total_messages"] == 2
        assert body["total_topics"] == 1
        assert body["total_reports"] == 1

    @pytest.mark.asyncio
    async def test_stats_empty_db(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B1: Empty DB returns zero counts, not error."""
        resp = await client.get("/api/v1/data/stats")
        assert resp.status_code == 200

        body = resp.json()
        assert body["total_messages"] == 0
        assert body["total_groups"] == 0
        assert body["total_topics"] == 0
        assert body["total_reports"] == 0
        assert body["date_range_start"] is None
        assert body["date_range_end"] is None


# ============================================================
# H1/B2: Provenance chain endpoint
# ============================================================


class TestProvenanceEndpoint:
    """B2/B4: GET /data/provenance/{server_id} — forward trace + 404."""

    @pytest.mark.asyncio
    async def test_provenance_with_topics(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B2: GET /data/provenance/{sid} linked to 2 topics -> 200 with message + topics."""
        await seed_provenance_scenario(db_conn)

        resp = await client.get("/api/v1/data/provenance/sid-prove")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert body["server_id"] == "sid-prove"
        assert body["message"] is not None
        assert body["message"]["sender"] == "Dave"
        assert body["message"]["content"] == "Provenance test message"

        topics = body["topics"]
        assert len(topics) == 2, f"Expected 2 topics, got {len(topics)}"
        topic_names = {t["topic_name"] for t in topics}
        assert "Topic 1" in topic_names
        assert "Topic 2" in topic_names

    @pytest.mark.asyncio
    async def test_provenance_nonexistent_returns_404(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B4: GET /data/provenance/nonexistent_sid -> 404."""
        await seed_provenance_scenario(db_conn)

        resp = await client.get("/api/v1/data/provenance/nonexistent_sid")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_provenance_no_topics(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B2: Message with no associated topics still returns 200 with empty topics."""
        await db_conn.execute(
            "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
            "VALUES ('sid-alone', '20260601', 'grp-p', 'Frank', 'No topics')"
        )
        await db_conn.commit()

        resp = await client.get("/api/v1/data/provenance/sid-alone")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        body = resp.json()
        assert body["server_id"] == "sid-alone"
        assert body["message"] is not None
        assert body["topics"] == []


# ============================================================
# H1/B3: L1 message detail endpoint
# ============================================================


class TestL1DetailEndpoint:
    """B3/B4: GET /data/l1/{gid}/{date}/detail/{sid} — detail view + 404."""

    @pytest.mark.asyncio
    async def test_l1_detail_with_contexts(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B3: GET /data/l1/{gid}/{date}/detail/{sid} -> 200 with L2 context blocks."""
        await seed_l1_detail_scenario(db_conn)

        resp = await client.get("/api/v1/data/l1/grp-d/20260601/detail/sid-detail")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert body["serverID"] == "sid-detail"
        assert body["sender"] == "Eve"
        assert body["content"] == "Detail test message"
        assert body["group_id"] == "grp-d"
        assert body["date"] == "20260601"

        contexts = body["contexts"]
        assert len(contexts) >= 1, f"Expected at least 1 context, got {len(contexts)}"
        assert any(c["context_id"] == "ctx-detail" for c in contexts)

        summaries = body["summaries"]
        assert len(summaries) >= 1, f"Expected at least 1 summary, got {len(summaries)}"
        assert any(s["summary_id"] == "sum-detail" for s in summaries)

    @pytest.mark.asyncio
    async def test_l1_detail_nonexistent_returns_404(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B4: GET /data/l1/.../detail/nonexistent -> 404."""
        await seed_l1_detail_scenario(db_conn)

        resp = await client.get("/api/v1/data/l1/grp-d/20260601/detail/nonexistent")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_l1_detail_wrong_group_date(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B4: Correct serverID but wrong group/date -> 404."""
        await seed_l1_detail_scenario(db_conn)

        resp = await client.get("/api/v1/data/l1/wrong-group/20260601/detail/sid-detail")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_l1_detail_no_contexts(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """B3: Message without contexts/summaries returns 200 with empty arrays."""
        await db_conn.execute(
            "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
            "VALUES ('sid-naked', '20260601', 'grp-d', 'Grace', 'No contexts')"
        )
        await db_conn.commit()

        resp = await client.get("/api/v1/data/l1/grp-d/20260601/detail/sid-naked")
        assert resp.status_code == 200
        body = resp.json()
        assert body["serverID"] == "sid-naked"
        assert body["contexts"] == []
        assert body["summaries"] == []


# ============================================================
# H1: Existing endpoint regression — GET /data/{layer}/{gid}/{date}
# ============================================================


class TestExistingDataEndpoint:
    """Regression: existing GET /data/{layer}/{gid}/{date} still works."""

    @pytest.mark.asyncio
    async def test_existing_l1_endpoint(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """Existing L1 endpoint returns paginated results."""
        await seed_data_stats_scenario(db_conn)

        resp = await client.get("/api/v1/data/l1/grp-a/20260601")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] >= 1

    @pytest.mark.asyncio
    async def test_existing_l3_endpoint(
        self, app: FastAPI, db_conn: aiosqlite.Connection, client: AsyncClient
    ) -> None:
        """Existing L3 endpoint returns topic list."""
        await seed_data_stats_scenario(db_conn)

        resp = await client.get("/api/v1/data/l3/grp-a/20260601")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["layer"] == "l3"
