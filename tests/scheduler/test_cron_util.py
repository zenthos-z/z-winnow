"""Tests for scheduler.cron_util — pure cron parsing/matching, no I/O."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from z_winnow.scheduler import cron_util

SH = ZoneInfo("Asia/Shanghai")


def dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=SH)


# ── validate_cron ────────────────────────────────────────────────────────────
def test_validate_cron_accepts_standard_5field():
    assert cron_util.validate_cron("0 2 * * *") is True
    assert cron_util.validate_cron("*/5 * * * *") is True
    assert cron_util.validate_cron("0 9 * * mon,fri") is True


def test_validate_cron_rejects_garbage_and_empty():
    assert cron_util.validate_cron(None) is False
    assert cron_util.validate_cron("") is False
    assert cron_util.validate_cron("   ") is False
    assert cron_util.validate_cron("not a cron") is False
    assert cron_util.validate_cron("99 99 * * *") is False


# ── is_due ───────────────────────────────────────────────────────────────────
def test_is_due_matches_exact_minute():
    assert cron_util.is_due("0 2 * * *", dt(2026, 7, 23, 2, 0)) is True


def test_is_due_rejects_wrong_minute():
    assert cron_util.is_due("0 9 * * *", dt(2026, 7, 23, 2, 0)) is False
    assert cron_util.is_due("0 2 * * *", dt(2026, 7, 23, 2, 1)) is False


def test_is_dow_field():
    # 2026-07-23 is a Thursday (dow=4)
    assert cron_util.is_due("0 2 * * 4", dt(2026, 7, 23, 2, 0)) is True
    assert cron_util.is_due("0 2 * * 1", dt(2026, 7, 23, 2, 0)) is False


def test_is_due_invalid_returns_false_no_raise():
    assert cron_util.is_due("garbage", dt(2026, 7, 23, 2, 0)) is False
    assert cron_util.is_due(None, dt(2026, 7, 23, 2, 0)) is False


# ── next_fire / next_n ───────────────────────────────────────────────────────
def test_next_fire_advances_one_day():
    nf = cron_util.next_fire("0 2 * * *", dt(2026, 7, 23, 2, 0))
    assert nf == dt(2026, 7, 24, 2, 0)


def test_next_n_returns_oldest_first():
    fires = cron_util.next_n("0 2 * * *", dt(2026, 7, 23, 2, 0), 3)
    assert fires == [dt(2026, 7, 24, 2, 0), dt(2026, 7, 25, 2, 0), dt(2026, 7, 26, 2, 0)]


def test_next_n_invalid_empty():
    assert cron_util.next_n("bad", dt(2026, 7, 23, 2, 0), 3) == []
    assert cron_util.next_n("0 2 * * *", dt(2026, 7, 23, 2, 0), 0) == []


# ── time_to_cron ─────────────────────────────────────────────────────────────
def test_time_to_cron_basic():
    assert cron_util.time_to_cron("07:30") == "30 7 * * *"
    assert cron_util.time_to_cron("9:5") == "5 9 * * *"
    assert cron_util.time_to_cron(" 23:59 ") == "59 23 * * *"


def test_time_to_cron_rejects_invalid():
    assert cron_util.time_to_cron("24:00") is None
    assert cron_util.time_to_cron("12:60") is None
    assert cron_util.time_to_cron("abc") is None
    assert cron_util.time_to_cron("") is None
    assert cron_util.time_to_cron(None) is None


# ── cron_to_human ────────────────────────────────────────────────────────────
def test_cron_to_human_preset_and_daily():
    assert cron_util.cron_to_human("0 2 * * *") == "凌晨 2:00"
    assert cron_util.cron_to_human("30 7 * * *") == "每日 07:30"


def test_cron_to_human_empty_and_raw():
    assert cron_util.cron_to_human(None) == "—"
    assert cron_util.cron_to_human("") == "—"
    assert cron_util.cron_to_human("*/5 * * * *") == "*/5 * * * *"


def test_yesterday_iso():
    assert cron_util.yesterday_iso(dt(2026, 7, 23, 0, 30)) == "2026-07-22"
