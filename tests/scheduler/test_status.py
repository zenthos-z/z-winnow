"""Tests for scheduler.status — the shared dashboard data layer."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from z_winnow.pipeline.database import init_database
from z_winnow.scheduler.status import get_scheduler_status

SH = ZoneInfo("Asia/Shanghai")


async def _seed(tmp_path, *, cron="0 2 * * *", enabled=1):
    db = tmp_path / "s.db"
    await init_database(str(db))
    async with aiosqlite.connect(db) as c:
        await c.execute(
            "INSERT INTO groups(group_id, display_name, chatroom_id, output_dir, "
            "is_active, daily_report_enabled, daily_schedule_cron, created_at, updated_at) "
            "VALUES(?,?,?,?,1,?,?,?,?)",
            ("g_a", "群A", "room@a", "out", enabled, cron, "2026-01-01", "2026-01-01"),
        )
        await c.commit()
    return str(db)


async def test_status_basic_fields(tmp_path):
    db = await _seed(tmp_path)
    now = datetime(2026, 7, 23, 2, 0, tzinfo=SH)
    st = await get_scheduler_status(db, lookback_days=7, now=now)

    assert st.tz == "Asia/Shanghai"
    assert st.target_mode == "previous_day"
    assert len(st.groups) == 1
    g = st.groups[0]
    assert g.group_id == "g_a"
    assert g.enabled is True
    assert g.cron == "0 2 * * *"
    assert g.cron_human == "凌晨 2:00"
    assert g.cron_valid is True
    assert g.next_fire is not None  # next 02:00 after 02:00 today is tomorrow
    assert g.missing_days == 7  # no reports yet
    assert g.last_run_date is None
    assert st.daemon_liveness == "unknown"  # no heartbeat table row


async def test_status_missing_days_decreases_with_reports(tmp_path):
    db = await _seed(tmp_path)
    # create a report_versions row for yesterday (today-1)
    now = datetime(2026, 7, 23, 2, 0, tzinfo=SH)
    yest = (now.date() - timedelta(days=1)).strftime("%Y%m%d")
    async with aiosqlite.connect(db) as c:
        await c.execute(
            "INSERT INTO report_versions(version_id, report_id, group_id, date, version_number, "
            "source, is_active, created_at) VALUES(?,?,?,?,1,'daily_run',1,?)",
            ("g_a-y", "g_a-y", "g_a", yest, "2026-07-22T02:00:00Z"),
        )
        await c.commit()
    st = await get_scheduler_status(db, lookback_days=7, now=now)
    g = st.groups[0]
    assert g.missing_days == 6  # one of the 7 now present
    assert g.last_run_date == yest


async def test_status_invalid_cron_flagged(tmp_path):
    db = await _seed(tmp_path, cron="garbage")
    now = datetime(2026, 7, 23, 2, 0, tzinfo=SH)
    st = await get_scheduler_status(db, now=now)
    g = st.groups[0]
    assert g.cron_valid is False
    assert g.next_fire is None
    assert g.cron_human == "garbage"  # falls back to raw


async def test_status_disabled_group_missing_zero(tmp_path):
    db = await _seed(tmp_path, enabled=0)
    now = datetime(2026, 7, 23, 2, 0, tzinfo=SH)
    st = await get_scheduler_status(db, now=now)
    g = st.groups[0]
    assert g.enabled is False
    assert g.missing_days == 0  # disabled groups don't count missing


async def test_status_to_dict_roundtrip(tmp_path):
    db = await _seed(tmp_path)
    now = datetime(2026, 7, 23, 2, 0, tzinfo=SH)
    st = await get_scheduler_status(db, now=now)
    d = st.to_dict()
    assert d["tz"] == "Asia/Shanghai"
    assert isinstance(d["groups"], list) and len(d["groups"]) == 1
    assert "next_fire" in d["groups"][0]
