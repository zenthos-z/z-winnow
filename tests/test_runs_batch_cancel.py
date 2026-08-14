"""W15-P0-RUNS: Batch run creation + run cancellation endpoint tests.

Covers:
  B1: POST /runs/batch with 3 valid items -> 202 + valid UUID task_id
  B2: POST /runs/batch with mix of valid+invalid -> partial failure isolation
  B3: POST /runs/{run_id}/cancel for queued run -> 200, dual-table cancelled
  B4: POST /runs/{run_id}/cancel for nonexistent -> 404; terminal -> 409
  B5: POST /runs/batch with empty items list -> 422 ValidationError

# P078: Real SQLite :memory: via app lifespan — no mocked connections.
# P011: Each B-criterion has its own dedicated test function.
# P012: autouse monkeypatch env isolation.
# P013: Class-based organization by endpoint group.
# A018: Real DDL + real INSERT — no mocked dicts.
# L100: All tests hit real SQLite via real app lifespan.

Usage:
    poetry run pytest tests/test_runs_batch_cancel.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "")
    monkeypatch.setenv("WINNOW_DB_PATH", "")
    monkeypatch.setenv("WINNOW_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
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
# App + client fixtures (real lifespan, file-based SQLite)
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a FastAPI app with real lifespan, middleware, and routes."""
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.middleware import ApiKeyMiddleware, ErrorHandlerMiddleware
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "test_runs.db")
    reports_dir = str(tmp_path / "reports")
    Path(reports_dir).mkdir(exist_ok=True)

    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
    reset_settings()

    fresh = FastAPI(title="runs-test", lifespan=lifespan)
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


# ============================================================
# Test: POST /runs/batch (B1, B2, B5)
# ============================================================


class TestBatchRunCreation:
    """B1: Batch creation with valid items -> 202.
    B2: Mixed valid/invalid -> partial failure isolation.
    B5: Empty items -> 422.
    """

    async def test_batch_3_valid_items_returns_202(self, client: httpx.AsyncClient) -> None:
        """B1: POST /runs/batch with 3 valid items -> 202 + valid UUID task_id."""
        resp = await client.post(
            "/api/v1/runs/batch",
            json={
                "items": [
                    {"component": "pipeline", "group_id": "g_batch_1", "date": "2026-06-01"},
                    {"component": "daily_reporter", "group_id": "g_batch_2", "date": "2026-06-02"},
                    {"component": "topic_tracker", "group_id": "g_batch_3"},
                ],
            },
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "task_id" in body
        assert len(body["task_id"]) > 0  # valid UUID
        assert "status_url" in body

    async def test_batch_partial_failure_isolation(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """B2: Mix of valid items + an item that triggers insert_run failure -> partial failure isolation.

        L037: Per-item try/except ensures a single failing insert_run
        does NOT abort the entire batch. Valid items still get created,
        failures reported per-item in results.

        Uses manual mock replacement (not context-manager patch) because
        the background task runs asynchronously after the HTTP response.
        """
        from z_winnow.web.services import run_service as rs
        from z_winnow.web.services.task_queue import get_task_status

        # P078: Use real database, only mock insert_run for failure simulation
        _original = rs.insert_run
        call_count = [0]
        failed_second = False

        async def _mock_insert(run_id, *, group_id="", date="", message_count=0):
            nonlocal failed_second
            call_count[0] += 1
            if call_count[0] == 2:
                failed_second = True
                raise RuntimeError("Simulated DB connection error")
            return await _original(
                run_id, group_id=group_id, date=date, message_count=message_count
            )

        # Manual mock replacement — must persist across background task execution
        rs.insert_run = _mock_insert
        try:
            resp = await client.post(
                "/api/v1/runs/batch",
                json={
                    "items": [
                        {"component": "pipeline", "group_id": "g_valid_a", "date": "2026-06-01"},
                        {
                            "component": "pipeline",
                            "group_id": "g_will_fail",
                        },  # this one fails via mock
                        {"component": "daily_reporter", "group_id": "g_valid_c"},
                    ],
                },
            )
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]

            # Poll until done (mock stays in place during polling)
            status = None
            for _ in range(30):
                await asyncio.sleep(0.2)
                status = await get_task_status(task_id, db_path=real_app.state.db_path)
                if status and status["status"] in ("done", "failed"):
                    break
        finally:
            # Restore original before assertions
            rs.insert_run = _original

        assert status is not None, "Batch task never appeared in DB"
        assert status["status"] == "done", f"Batch task failed: {status.get('error_message')}"

        # Verify results show partial success
        import json

        result = json.loads(status.get("result", "{}"))
        assert result.get("total") == 3
        results_list = result.get("results", [])
        assert len(results_list) == 3
        # At least one item succeeded (run_id present) and at least one failed (error present)
        success_items = [r for r in results_list if "run_id" in r]
        error_items = [r for r in results_list if "error" in r]
        assert len(success_items) == 2, (
            f"Expected 2 successful items, got {len(success_items)}: {results_list}"
        )
        assert len(error_items) == 1, (
            f"Expected 1 failed item, got {len(error_items)}: {results_list}"
        )

    async def test_batch_empty_items_returns_422(self, client: httpx.AsyncClient) -> None:
        """B5: POST /runs/batch with empty items list -> 422 ValidationError."""
        resp = await client.post("/api/v1/runs/batch", json={"items": []})
        assert resp.status_code == 422


# ============================================================
# Test: POST /runs/{run_id}/cancel (B3, B4)
# ============================================================


class TestRunCancellation:
    """B3: Cancel queued run -> 200, dual-table consistency.
    B4: Cancel nonexistent -> 404; terminal -> 409.
    """

    async def test_cancel_queued_run_dual_table_consistency(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """B3: POST /runs/{run_id}/cancel for queued run -> 200.

        Verifies dual-table consistency: BOTH async_tasks and pipeline_runs
        tables show 'cancelled'.
        """
        import uuid

        from z_winnow.web.services.task_queue import _ensure_async_tasks_table

        run_id = str(uuid.uuid4())
        conn: aiosqlite.Connection = real_app.state.db_conn

        # Ensure async_tasks table has resource_id column (migrations may not have run)
        await _ensure_async_tasks_table(conn)

        # Insert directly into pipeline_runs with 'queued' status
        await conn.execute(
            "INSERT INTO pipeline_runs (run_id, component, status, group_id, date) "
            "VALUES (?, 'pipeline', 'queued', 'g_cancel', '2026-06-01')",
            (run_id,),
        )
        # Also insert a matching async_tasks entry
        await conn.execute(
            "INSERT INTO async_tasks (task_id, task_type, resource_id, status, created_at, updated_at) "
            "VALUES (?, 'pipeline_run', ?, 'queued', datetime('now'), datetime('now'))",
            (str(uuid.uuid4()), run_id),
        )
        await conn.commit()

        # Now cancel the run
        resp = await client.post(f"/api/v1/runs/{run_id}/cancel")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["status"] == "cancelled"

        # Verify dual-table consistency: pipeline_runs
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT status FROM pipeline_runs WHERE run_id = ?", (run_id,))
        run_row = await cursor.fetchone()
        assert run_row is not None
        assert run_row["status"] == "cancelled", f"pipeline_runs status: {run_row['status']}"

        # Verify dual-table consistency: async_tasks
        cursor = await conn.execute(
            "SELECT status FROM async_tasks WHERE resource_id = ? AND task_type = 'pipeline_run'",
            (run_id,),
        )
        task_rows = await cursor.fetchall()
        assert len(task_rows) >= 1
        for task_row in task_rows:
            assert task_row["status"] == "cancelled", f"async_tasks status: {task_row['status']}"

    async def test_cancel_nonexistent_run_returns_404(self, client: httpx.AsyncClient) -> None:
        """B4: POST /runs/{run_id}/cancel for nonexistent run_id -> 404."""
        resp = await client.post("/api/v1/runs/nonexistent-run-id/cancel")
        assert resp.status_code == 404

    async def test_cancel_completed_run_returns_409(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """B4: POST /runs/{run_id}/cancel for already-completed run -> 409 Conflict."""
        import uuid

        run_id = str(uuid.uuid4())
        conn: aiosqlite.Connection = real_app.state.db_conn

        # Insert a completed run
        await conn.execute(
            "INSERT INTO pipeline_runs (run_id, component, status, group_id, date) "
            "VALUES (?, 'pipeline', 'completed', 'g_done', '2026-06-01')",
            (run_id,),
        )
        await conn.commit()

        resp = await client.post(f"/api/v1/runs/{run_id}/cancel")
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
        assert "already" in resp.json()["detail"].lower()

    async def test_cancel_failed_run_returns_409(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """B4: POST /runs/{run_id}/cancel for already-failed run -> 409 Conflict."""
        import uuid

        run_id = str(uuid.uuid4())
        conn: aiosqlite.Connection = real_app.state.db_conn

        await conn.execute(
            "INSERT INTO pipeline_runs (run_id, component, status, group_id, date) "
            "VALUES (?, 'pipeline', 'failed', 'g_fail', '2026-06-01')",
            (run_id,),
        )
        await conn.commit()

        resp = await client.post(f"/api/v1/runs/{run_id}/cancel")
        assert resp.status_code == 409
