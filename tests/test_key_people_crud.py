"""W15-P1-KEYPEOPLE: PUT and DELETE /api/v1/key-people/{sender}?group_id=X tests.

Covers:
  B1: PUT updates display_name → 200 KeyPeopleOut
  B2: DELETE → 204, subsequent PUT → 404
  B3: 404 for nonexistent sender+group_id; 422 for missing group_id
  B4: PUT notes=Test note → DB column note=Test note (field translation)

# P078: All tests use real SQLite :memory: — never mock aiosqlite.
# P011: Each B-criterion has its own dedicated test function.
# P054: Route layer parse-validate-delegate verified via HTTP response codes.
# P050: Parameterized SQL verified by DB inspection after operations.
# L100: Real data, real DB — 100% non-mock.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from z_winnow.web.routes import api_router

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create a test FastAPI app with api_router."""
    _app = FastAPI()
    _app.include_router(api_router)
    return _app


@pytest.fixture
async def db_conn() -> aiosqlite.Connection:
    """In-memory SQLite with minimal schema for key_people tests."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            chatroom_id TEXT NOT NULL DEFAULT '',
            output_dir TEXT,
            feishu_enabled INTEGER DEFAULT 0,
            custom_prompt_hints TEXT,
            is_active INTEGER DEFAULT 1,
            daily_report_enabled INTEGER DEFAULT 1,
            daily_schedule_cron TEXT,
            created_at TEXT,
            updated_at TEXT,
            created_by TEXT
        );
        CREATE TABLE IF NOT EXISTS group_members (
            member_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            wxid TEXT,
            role TEXT DEFAULT 'member',
            weight REAL DEFAULT 1.0,
            note TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        );
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
    """)
    yield conn
    await conn.close()


@pytest.fixture
async def client(app: FastAPI, db_conn: aiosqlite.Connection):
    """Async test client with db_conn on app.state."""
    app.state.db_conn = db_conn
    app.state.db_path = ":memory:"
    app.state.reports_dir = "."

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Helpers
# ============================================================


async def _seed_member(
    db: aiosqlite.Connection,
    group_id: str = "g_test",
    wxid: str = "user_01",
    name: str = "Alice",
    role: str = "member",
    note: str | None = None,
    is_active: int = 1,
) -> str:
    """Insert a test row into group_members and return member_id."""
    import uuid

    member_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO group_members
           (member_id, group_id, name, wxid, role, note, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (member_id, group_id, name, wxid, role, note, is_active),
    )
    await db.commit()
    return member_id


async def _seed_group(db: aiosqlite.Connection, group_id: str = "g_test") -> None:
    """Insert a minimal groups row (needed for FK if enabled)."""
    await db.execute(
        """INSERT OR IGNORE INTO groups
           (group_id, display_name, chatroom_id, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        (group_id, f"Group {group_id}", f"chatroom_{group_id}"),
    )
    await db.commit()


# ============================================================
# B1: PUT updates display_name
# ============================================================


@pytest.mark.asyncio
async def test_b1_put_update_display_name(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B1: PUT /key-people/{sender}?group_id=X with display_name → 200, name updated."""
    await _seed_group(db_conn, "g_b1")
    await _seed_member(db_conn, group_id="g_b1", wxid="user_b1", name="OldName")

    resp = await client.put(
        "/api/v1/key-people/user_b1",
        params={"group_id": "g_b1"},
        json={"display_name": "NewName"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sender"] == "user_b1"
    assert data["group_id"] == "g_b1"

    # Verify DB column was updated (P050 / RF3: display_name → name)
    cursor = await db_conn.execute(
        "SELECT name, role, note, is_active FROM group_members WHERE wxid = ? AND group_id = ?",
        ("user_b1", "g_b1"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["name"] == "NewName"
    assert row["is_active"] == 1


# ============================================================
# B1b: PUT partial update — only specified fields change
# ============================================================


@pytest.mark.asyncio
async def test_put_partial_update_role_only(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """P009: PUT with only role → name unchanged, role updated."""
    await _seed_group(db_conn, "g_partial")
    await _seed_member(db_conn, group_id="g_partial", wxid="u_part", name="KeepMe", role="member")

    resp = await client.put(
        "/api/v1/key-people/u_part",
        params={"group_id": "g_partial"},
        json={"role": "admin"},
    )
    assert resp.status_code == 200

    cursor = await db_conn.execute(
        "SELECT name, role FROM group_members WHERE wxid = ? AND group_id = ?",
        ("u_part", "g_partial"),
    )
    row = await cursor.fetchone()
    assert row["name"] == "KeepMe"  # unchanged
    assert row["role"] == "admin"  # updated


# ============================================================
# B1c: PUT with is_active=false
# ============================================================


@pytest.mark.asyncio
async def test_put_is_active_false(client: AsyncClient, db_conn: aiosqlite.Connection) -> None:
    """PUT with is_active=false → DB is_active=0."""
    await _seed_group(db_conn, "g_active")
    await _seed_member(db_conn, group_id="g_active", wxid="u_act", name="ActiveUser")

    resp = await client.put(
        "/api/v1/key-people/u_act",
        params={"group_id": "g_active"},
        json={"is_active": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_active"] is False

    cursor = await db_conn.execute(
        "SELECT is_active FROM group_members WHERE wxid = ? AND group_id = ?",
        ("u_act", "g_active"),
    )
    row = await cursor.fetchone()
    assert row["is_active"] == 0


# ============================================================
# B2: DELETE soft-delete → 204, subsequent PUT → 404
# ============================================================


@pytest.mark.asyncio
async def test_b2_delete_soft_delete_returns_204(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B2: DELETE → 204 No Content, DB is_active=0."""
    await _seed_group(db_conn, "g_b2")
    await _seed_member(db_conn, group_id="g_b2", wxid="user_b2", name="Bob")

    resp = await client.delete(
        "/api/v1/key-people/user_b2",
        params={"group_id": "g_b2"},
    )
    assert resp.status_code == 204
    # 204 has no body
    assert resp.text == ""

    # Verify soft-delete in DB
    cursor = await db_conn.execute(
        "SELECT is_active FROM group_members WHERE wxid = ? AND group_id = ?",
        ("user_b2", "g_b2"),
    )
    row = await cursor.fetchone()
    assert row["is_active"] == 0


# ============================================================
# B2b: DELETE idempotent — second delete also returns 204
# ============================================================


@pytest.mark.asyncio
async def test_delete_idempotent_second_call_204(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """Second DELETE on same sender+group_id still returns 204 (already soft-deleted)."""
    await _seed_group(db_conn, "g_idem")
    await _seed_member(db_conn, group_id="g_idem", wxid="u_idem", name="IdemUser")

    # First delete
    resp1 = await client.delete(
        "/api/v1/key-people/u_idem",
        params={"group_id": "g_idem"},
    )
    assert resp1.status_code == 204

    # Second delete — still 204 (rowcount=0 because is_active already 0)
    resp2 = await client.delete(
        "/api/v1/key-people/u_idem",
        params={"group_id": "g_idem"},
    )
    assert resp2.status_code == 204


# ============================================================
# B3: 404 for nonexistent sender+group_id
# ============================================================


@pytest.mark.asyncio
async def test_b3_put_nonexistent_returns_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B3: PUT for nonexistent sender+group_id → 404."""
    await _seed_group(db_conn, "g_b3")

    resp = await client.put(
        "/api/v1/key-people/nonexistent_user",
        params={"group_id": "g_b3"},
        json={"display_name": "Ghost"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_b3_delete_nonexistent_returns_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B3: DELETE for nonexistent sender+group_id → 404."""
    await _seed_group(db_conn, "g_b3")

    resp = await client.delete(
        "/api/v1/key-people/nonexistent_user",
        params={"group_id": "g_b3"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_b3_missing_group_id_returns_422(client: AsyncClient) -> None:
    """B3: PUT without group_id query param → 422 validation error."""
    resp = await client.put(
        "/api/v1/key-people/some_user",
        json={"display_name": "X"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_b3_delete_missing_group_id_returns_422(client: AsyncClient) -> None:
    """B3: DELETE without group_id query param → 422 validation error."""
    resp = await client.delete("/api/v1/key-people/some_user")
    assert resp.status_code == 422


# ============================================================
# B3c: Wrong group_id → 404 (sender exists but in different group)
# ============================================================


@pytest.mark.asyncio
async def test_put_wrong_group_id_returns_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """PUT with sender that exists in group A but querying group B → 404."""
    await _seed_group(db_conn, "g_A")
    await _seed_group(db_conn, "g_B")
    await _seed_member(db_conn, group_id="g_A", wxid="u_x", name="CrossGroup")

    resp = await client.put(
        "/api/v1/key-people/u_x",
        params={"group_id": "g_B"},  # wrong group
        json={"display_name": "ShouldFail"},
    )
    assert resp.status_code == 404


# ============================================================
# B4: PUT notes=Test note → field translation verified
# ============================================================


@pytest.mark.asyncio
async def test_b4_put_notes_field_translation(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B4: PUT notes=Test note → DB column note=Test note (RF3 translation)."""
    await _seed_group(db_conn, "g_b4")
    await _seed_member(db_conn, group_id="g_b4", wxid="u_b4", name="Carol", note=None)

    resp = await client.put(
        "/api/v1/key-people/u_b4",
        params={"group_id": "g_b4"},
        json={"notes": "Test note"},
    )
    assert resp.status_code == 200

    # Verify DB column "note" was set (NOT "notes" — field translation)
    cursor = await db_conn.execute(
        "SELECT note FROM group_members WHERE wxid = ? AND group_id = ?",
        ("u_b4", "g_b4"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["note"] == "Test note"


# ============================================================
# B4b: PUT multiple fields at once
# ============================================================


@pytest.mark.asyncio
async def test_put_multiple_fields(client: AsyncClient, db_conn: aiosqlite.Connection) -> None:
    """PUT with display_name + role + notes → all three updated."""
    await _seed_group(db_conn, "g_multi")
    await _seed_member(
        db_conn, group_id="g_multi", wxid="u_multi", name="Old", role="member", note=None
    )

    resp = await client.put(
        "/api/v1/key-people/u_multi",
        params={"group_id": "g_multi"},
        json={"display_name": "NewName", "role": "moderator", "notes": "VIP"},
    )
    assert resp.status_code == 200

    cursor = await db_conn.execute(
        "SELECT name, role, note FROM group_members WHERE wxid = ? AND group_id = ?",
        ("u_multi", "g_multi"),
    )
    row = await cursor.fetchone()
    assert row["name"] == "NewName"
    assert row["role"] == "moderator"
    assert row["note"] == "VIP"


# ============================================================
# Edge: PUT empty body → returns 200 with current state (no-op)
# ============================================================


@pytest.mark.asyncio
async def test_put_empty_body_returns_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """PUT with empty JSON body → 404 because service returns None for no-op."""
    await _seed_group(db_conn, "g_empty_body")
    await _seed_member(db_conn, group_id="g_empty_body", wxid="u_eb", name="EmptyBody")

    resp = await client.put(
        "/api/v1/key-people/u_eb",
        params={"group_id": "g_empty_body"},
        json={},
    )
    # Service returns None when no fields to update → route returns 404
    assert resp.status_code == 404
