"""Tests for scheduler.views — assert rendered output contains the right cues."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from z_winnow.scheduler import views
from z_winnow.scheduler.preflight import CheckResult, PreflightReport
from z_winnow.scheduler.status import GroupStatus, SchedulerStatus

SH = ZoneInfo("Asia/Shanghai")


def _g(**kw) -> GroupStatus:
    base = dict(
        group_id="g_a", display_name="群A", enabled=True, is_active=True,
        cron="0 2 * * *", cron_human="凌晨 2:00", cron_valid=True,
        next_fire="2026-07-24T02:00:00+08:00", last_run_date="20260722",
        last_run_at="2026-07-22T02:00:00Z", missing_days=0,
    )
    base.update(kw)
    return GroupStatus(**base)


def test_dashboard_renders_enabled_and_missing_cues():
    status = SchedulerStatus(
        groups=[_g(), _g(display_name="群B", enabled=False, missing_days=3, cron="0 9 * * *", cron_human="早 9:00")],
        tz="Asia/Shanghai", daemon_liveness="running", daemon_last_tick="2026-07-23T02:00:00+08:00",
    )
    out = views.render_to_str(views.build_dashboard(status))
    assert "群A" in out
    assert "群B" in out
    assert "●启用" in out or "启用" in out
    assert "定时日报调度" in out
    assert "running" in out


def test_dashboard_invalid_cron_marked():
    status = SchedulerStatus(groups=[_g(cron="garbage", cron_valid=False, cron_human="garbage", next_fire=None)])
    out = views.render_to_str(views.build_dashboard(status))
    assert "garbage" in out


def test_preflight_panel_ok_and_fail():
    ok = PreflightReport([CheckResult("db", "ok", "data/x.db", critical=True), CheckResult("docker", "ok", "v27")])
    out_ok = views.render_to_str(views.build_preflight_panel(ok))
    assert "全部就绪" in out_ok

    bad = PreflightReport([CheckResult("memos_containers", "fail", "缺少 2/4", "docker compose up", critical=True)])
    out_bad = views.render_to_str(views.build_preflight_panel(bad))
    assert "关键缺失" in out_bad
    assert "fail" in out_bad


def test_next_fires_table():
    fires = [
        datetime(2026, 7, 24, 2, 0, tzinfo=SH),
        datetime(2026, 7, 25, 2, 0, tzinfo=SH),
    ]
    out = views.render_to_str(views.build_next_fires_table("0 2 * * *", fires))
    assert "2026-07-24 02:00" in out
    assert "2026-07-25 02:00" in out


def test_group_detail_invalid_warns():
    out = views.render_to_str(views.build_group_detail(_g(cron="bad", cron_valid=False, cron_human="bad")))
    assert "无效" in out
