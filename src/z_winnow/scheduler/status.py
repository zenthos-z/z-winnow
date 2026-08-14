"""Read-only scheduler status — the data layer behind the CLI dashboard and the
``GET /api/v1/scheduler/status`` web endpoint (single source, zero duplication).

For each group with scheduling relevant state, returns: cron (raw + human),
enabled flag, next fire time, last-run info (from report_versions), and the count
of missing days in a lookback window. Plus a daemon-liveness hint derived from the
``scheduler_state`` heartbeat written by :class:`DailyScheduler`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from z_winnow.scheduler import cron_util

__all__ = ["GroupStatus", "SchedulerStatus", "get_scheduler_status"]


@dataclass
class GroupStatus:
    group_id: str
    display_name: str
    enabled: bool
    is_active: bool
    cron: str | None
    cron_human: str
    cron_valid: bool
    next_fire: str | None  # ISO or None
    last_run_date: str | None  # YYYYMMDD
    last_run_at: str | None  # ISO created_at of latest report_versions row
    missing_days: int


@dataclass
class SchedulerStatus:
    groups: list[GroupStatus] = field(default_factory=list)
    tz: str = "Asia/Shanghai"
    daemon_last_tick: str | None = None  # ISO
    daemon_liveness: str = "unknown"  # "running" | "stale" | "unknown"
    target_mode: str = "previous_day"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tz": self.tz,
            "daemon_last_tick": self.daemon_last_tick,
            "daemon_liveness": self.daemon_liveness,
            "target_mode": self.target_mode,
            "groups": [
                {
                    "group_id": g.group_id,
                    "display_name": g.display_name,
                    "enabled": g.enabled,
                    "is_active": g.is_active,
                    "cron": g.cron,
                    "cron_human": g.cron_human,
                    "cron_valid": g.cron_valid,
                    "next_fire": g.next_fire,
                    "last_run_date": g.last_run_date,
                    "last_run_at": g.last_run_at,
                    "missing_days": g.missing_days,
                }
                for g in self.groups
            ],
        }


async def get_scheduler_status(
    db_path: str,
    *,
    lookback_days: int = 7,
    tz: str = "Asia/Shanghai",
    group: str | None = None,
    now: datetime | None = None,
) -> SchedulerStatus:
    """Build the scheduler status report.

    Args:
        db_path: SQLite database path.
        lookback_days: Window for the "missing days" count.
        tz: Timezone for next-fire computation.
        group: Optional group_id/display_name filter.
        now: Injected "now" (tests); None = current time in tz.
    """
    zone = ZoneInfo(tz)
    now_dt = (now or datetime.now(zone)).replace(second=0, microsecond=0)
    today = now_dt.date()
    window_start = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    window_end = (today - timedelta(days=1)).strftime("%Y%m%d")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # All groups (status should show disabled ones too, greyed out) — optional filter.
        if group:
            where = "WHERE group_id = ? OR display_name = ? OR chatroom_id = ?"
            params: tuple[Any, ...] = (group, group, group)
            cur = await db.execute(
                "SELECT group_id, display_name, chatroom_id, daily_report_enabled, "
                "daily_schedule_cron, is_active FROM groups " + where,
                params,
            )
        else:
            cur = await db.execute(
                "SELECT group_id, display_name, chatroom_id, daily_report_enabled, "
                "daily_schedule_cron, is_active FROM groups ORDER BY display_name"
            )
        rows = await cur.fetchall()

        groups: list[GroupStatus] = []
        for r in rows:
            gid = r["group_id"]
            cron = r["daily_schedule_cron"]
            valid = cron_util.validate_cron(cron)
            nf = cron_util.next_fire(cron, now_dt) if valid else None
            last_date, last_at = await _last_run(db, gid)
            missing = (
                await _missing_count(db, gid, window_start, window_end)
                if r["daily_report_enabled"]
                else 0
            )
            groups.append(
                GroupStatus(
                    group_id=gid,
                    display_name=r["display_name"] or r["chatroom_id"] or gid,
                    enabled=bool(r["daily_report_enabled"]),
                    is_active=bool(r["is_active"]),
                    cron=cron,
                    cron_human=cron_util.cron_to_human(cron),
                    cron_valid=valid,
                    next_fire=nf.isoformat() if nf else None,
                    last_run_date=last_date,
                    last_run_at=last_at,
                    missing_days=missing,
                )
            )

        daemon_last_tick, liveness = await _daemon_liveness(db, now_dt)

    return SchedulerStatus(
        groups=groups,
        tz=tz,
        daemon_last_tick=daemon_last_tick,
        daemon_liveness=liveness,
        target_mode="previous_day",
    )


async def _last_run(db: aiosqlite.Connection, group_id: str) -> tuple[str | None, str | None]:
    cur = await db.execute(
        "SELECT date, created_at FROM report_versions WHERE group_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (group_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None, None
    return row["date"], row["created_at"]


async def _missing_count(
    db: aiosqlite.Connection, group_id: str, window_start: str, window_end: str
) -> int:
    """Count days in [window_start, window_end] with no report_versions row."""
    try:
        s_dt = datetime.strptime(window_start, "%Y%m%d").date()
        e_dt = datetime.strptime(window_end, "%Y%m%d").date()
    except ValueError:
        return 0
    if e_dt < s_dt:
        return 0
    total_days = (e_dt - s_dt).days + 1
    cur = await db.execute(
        "SELECT COUNT(DISTINCT date) FROM report_versions WHERE group_id = ? AND date BETWEEN ? AND ?",
        (group_id, window_start, window_end),
    )
    row = await cur.fetchone()
    present = int(row[0]) if row else 0
    return max(0, total_days - present)


async def _daemon_liveness(db: aiosqlite.Connection, now_dt: datetime) -> tuple[str | None, str]:
    """Derive liveness from the scheduler_state heartbeat (if the table exists).

    Considers the daemon "running" if it ticked within the last ~2.5 minutes,
    "stale" if older, "unknown" if no heartbeat / table yet.
    """
    try:
        cur = await db.execute("SELECT value FROM scheduler_state WHERE key = 'last_tick'")
        row = await cur.fetchone()
    except aiosqlite.Error:
        return None, "unknown"  # table not present yet
    if not row:
        return None, "unknown"
    try:
        payload = json.loads(row[0])
        at_iso = payload.get("at")
        if not at_iso:
            return None, "unknown"
        at = datetime.fromisoformat(at_iso)
        # Normalize both to naive-UTC for a clean second-delta.
        now_naive = now_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        at_naive = at.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) if at.tzinfo else at
        age = (now_naive - at_naive).total_seconds()
        return at_iso, "running" if age < 150 else "stale"
    except Exception:
        return None, "unknown"
