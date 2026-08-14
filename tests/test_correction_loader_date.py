"""Bug 2 回归测试：load_corrections(date) 按对应日报注入已消费反馈。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
import pytest

from z_winnow.rl.correction_loader import load_corrections

_NOW_ISO = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

_FB_COLS = [
    "feedback_id",
    "created_at",
    "group_id",
    "date",
    "target_type",
    "target_id",
    "severity",
    "tags",
    "correction_mode",
    "original_text",
    "corrected_text",
    "status",
    "consumed_at",
]


async def _insert_fb(conn, **kw):
    vals = [kw.get(c) for c in _FB_COLS]
    placeholders = ",".join(["?"] * len(_FB_COLS))
    collist = ",".join(_FB_COLS)
    await conn.execute(f"INSERT INTO feedback_events ({collist}) VALUES ({placeholders})", vals)


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    conn = await aiosqlite.connect(p)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(
        """
        CREATE TABLE feedback_events (
            feedback_id TEXT PRIMARY KEY, created_at TEXT, group_id TEXT,
            date TEXT, report_id TEXT, target_type TEXT, target_id TEXT,
            target_path TEXT, target_version_id TEXT, target_topic_id TEXT,
            produced_version_id TEXT, memos_cube_id TEXT, memos_node_id TEXT,
            archived_memos_id TEXT, status TEXT DEFAULT 'active',
            rolled_back_at TEXT, rolled_back_by TEXT, signal TEXT,
            severity TEXT DEFAULT 'info', rating TEXT, tags TEXT,
            correction_mode TEXT, original_text TEXT, corrected_text TEXT,
            correction_note TEXT, reporter TEXT, consumed_at TEXT, consumed_by TEXT
        );
        CREATE TABLE group_experiences (
            experience_id TEXT PRIMARY KEY, group_id TEXT, topic_name TEXT,
            target_type TEXT, lesson TEXT, origin_feedback_id TEXT,
            origin_version_id TEXT, status TEXT DEFAULT 'active',
            created_at TEXT, updated_at TEXT, updated_by TEXT
        );
        """
    )
    await _insert_fb(
        conn,
        feedback_id="fb1",
        created_at=_NOW_ISO,
        group_id="g1",
        date="20260701",
        target_type="topic",
        target_id="t1",
        severity="error",
        tags='["fact_error"]',
        correction_mode="inline_edit",
        original_text="旧内容",
        corrected_text="新内容A",
        status="active",
        consumed_at=_NOW_ISO,
    )
    await _insert_fb(
        conn,
        feedback_id="fb2",
        created_at=_NOW_ISO,
        group_id="g1",
        date="20260701",
        target_type="topic",
        target_id="t2",
        severity="warning",
        correction_mode="free_text",
        original_text="X",
        corrected_text="Y未消费",
        status="active",
    )
    await _insert_fb(
        conn,
        feedback_id="fb3",
        created_at=_NOW_ISO,
        group_id="g1",
        date="20260702",
        target_type="topic",
        target_id="t3",
        severity="info",
        correction_mode="inline_edit",
        original_text="P",
        corrected_text="Q别日",
        status="active",
        consumed_at=_NOW_ISO,
    )
    await _insert_fb(
        conn,
        feedback_id="fb4",
        created_at=_NOW_ISO,
        group_id="g1",
        date="20260701",
        target_type="topic",
        target_id="t4",
        correction_mode="inline_edit",
        original_text="R",
        corrected_text="S已回滚",
        status="rolled_back",
        consumed_at=_NOW_ISO,
    )
    await conn.execute(
        "INSERT INTO group_experiences (experience_id, group_id, topic_name, target_type, "
        "lesson, status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("exp1", "g1", "议题X", "topic", "群级经验：避免错别字", "active", _NOW_ISO),
    )
    await conn.commit()
    await conn.close()
    monkeypatch.setattr(
        "z_winnow.rl.correction_loader.get_settings",
        lambda: SimpleNamespace(sqlite_db_path=str(p)),
    )
    return p


async def test_date_mode_returns_only_consumed_for_that_date(db_path):
    results = await load_corrections(group_id="g1", date="20260701")
    should_be = {r.should_be for r in results}
    assert "新内容A" in should_be
    assert "Y未消费" not in should_be
    assert "Q别日" not in should_be
    assert "S已回滚" not in should_be


async def test_date_mode_includes_experiences_as_supplement(db_path):
    results = await load_corrections(group_id="g1", date="20260701")
    should_be = {r.should_be for r in results}
    assert "群级经验：避免错别字" in should_be


async def test_date_accepts_dashed_format(db_path):
    results = await load_corrections(group_id="g1", date="2026-07-01")
    should_be = {r.should_be for r in results}
    assert "新内容A" in should_be


async def test_no_date_legacy_returns_experiences_only(db_path):
    results = await load_corrections(group_id="g1")
    should_be = {r.should_be for r in results}
    assert "群级经验：避免错别字" in should_be
    assert "新内容A" not in should_be


async def test_invalid_group_returns_empty(db_path):
    assert await load_corrections(group_id="", date="20260701") == []
