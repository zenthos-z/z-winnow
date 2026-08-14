"""qr-evaluator INDEPENDENT verification for W16-B3.

Written by the evaluator (NOT the builder). Distinct from the builder's
test_b3_insert_update_stream_same_source (which only exercised run_service.py).

This file independently verifies the A013 convergence contract for the OTHER
affected files the builder's B3 test did NOT touch end-to-end:
  - storage.py  : None-sentinel __init__ reads Settings LIVE (not import-frozen)
  - progress.py : insert_pipeline_run converged read (os.getenv removed)

All real SQLite (L100/A018: zero mocks). pytest --timeout=30 (L028).
"""

from __future__ import annotations

import aiosqlite
import pytest


def _override_db(monkeypatch: pytest.MonkeyPatch, db_file: str) -> None:
    """reset singleton → set env → (caller rebuilds via get_settings)."""
    from z_winnow.config import reset_settings

    reset_settings()
    monkeypatch.setenv("WINNOW_DB_PATH", db_file)


# ---------------------------------------------------------------------------
# storage.py: None-sentinel __init__ resolves db_path from LIVE Settings
# (proves the default is NOT frozen at import time — the A013 head constraint)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_eval_storage_none_sentinel_reads_live_settings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Warm the pipeline package first: storage.py imports pipeline.database,
    # and pipeline/__init__.py imports storage — a PRE-EXISTING (pre-W16-B3)
    # structural cycle that only bites when storage is imported in isolation.
    import z_winnow.pipeline.database  # noqa: F401
    from z_winnow.config import get_settings
    from z_winnow.storage import Storage

    file_a = str(tmp_path / "a.db")
    file_b = str(tmp_path / "b.db")

    # Two DIFFERENT overrides after import → two DIFFERENT resolved paths
    # (a frozen default-param would return the same value both times).
    _override_db(monkeypatch, file_a)
    assert get_settings().db_path == file_a
    s_a = Storage()  # zero-arg → None-sentinel → must resolve to file_a NOW
    assert s_a.db_path == file_a, (
        f"Storage() None-sentinel must honor live override; got {s_a.db_path!r}"
    )

    _override_db(monkeypatch, file_b)
    assert get_settings().db_path == file_b
    s_b = Storage()
    assert s_b.db_path == file_b, (
        f"Storage() must re-resolve after reset; got {s_b.db_path!r} (frozen default?)"
    )

    # Explicit arg still wins over the sentinel
    s_explicit = Storage(str(tmp_path / "explicit.db"))
    assert s_explicit.db_path == str(tmp_path / "explicit.db")

    from z_winnow.config import reset_settings

    reset_settings()


# ---------------------------------------------------------------------------
# progress.py: insert_pipeline_run converged read (os.getenv removed).
# Builder's B3 test never touched progress.py. Independent write→verify.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_eval_progress_insert_uses_converged_db_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from z_winnow.config import get_settings
    from z_winnow.graph.progress import insert_pipeline_run
    from z_winnow.pipeline.database import init_database

    db_file = str(tmp_path / "eval_progress.db")
    _override_db(monkeypatch, db_file)
    settings = get_settings()
    assert settings.db_path == db_file  # override took effect

    # pipeline write path (progress.py line 319: get_settings().db_path)
    await init_database(settings.db_path)
    ok = await insert_pipeline_run("eval_progress_x", group_id="g", date="20260101")
    assert ok is True

    # Verify the row landed in the EXACT file progress.py resolved to.
    async with aiosqlite.connect(settings.db_path) as verify:
        verify.row_factory = aiosqlite.Row
        cur = await verify.execute(
            "SELECT run_id, group_id, date FROM pipeline_runs WHERE run_id = ?",
            ("eval_progress_x",),
        )
        row = await cur.fetchone()
    assert row is not None, "insert_pipeline_run must write to settings.db_path"
    assert row["group_id"] == "g"
    assert row["date"] == "20260101"

    from z_winnow.config import reset_settings

    reset_settings()
