"""DailyScheduler — the scheduled-report execution engine.

Standalone (no FastAPI). Reads per-group ``daily_schedule_cron`` from the
``groups`` table and fires ``orchestrate()`` + ``auto_push_after_run()`` when the
cron matches the current minute, targeting the previous calendar day. On startup
it back-fills any missing days (downtime recovery), and ``run_forever`` blocks
unless :func:`check_environment` passes — so the scheduler never runs blind.

Idempotency is the single safety net: a ``(group_id, date)`` is generated iff no
``report_versions`` row exists for it. That rule governs per-minute ticks,
multi-day backfill, and daemon/web/manual overlap alike.

Reuses (web-free): ``orchestrate``, ``auto_push_after_run``,
``resolve_group_name``, the ``_check_data`` / ``_register_empty`` pattern from
``batch_scheduler`` — without the batch_jobs/batch_items bookkeeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from z_winnow.config.settings import Settings, get_settings
from z_winnow.scheduler import cron_util
from z_winnow.scheduler.preflight import PreflightReport, check_environment

logger = logging.getLogger(__name__)

# A report for (group_id, date) is considered "present" if a report_versions row
# exists (the output_composer writes it near the end of a successful run).
_REPORT_EXISTS_SQL = "SELECT 1 FROM report_versions WHERE group_id = ? AND date = ? LIMIT 1"
_DATES_IN_WINDOW_SQL = (
    "SELECT DISTINCT date FROM report_versions WHERE group_id = ? AND date BETWEEN ? AND ?"
)


class PreflightError(RuntimeError):
    """Raised by run_forever when a critical dependency check fails."""

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        names = ", ".join(c.name for c in report.critical_failures) or "(none)"
        super().__init__(f"scheduler preflight failed: {names}")


@dataclass
class GroupScheduleRow:
    group_id: str
    display_name: str
    chatroom_id: str
    enabled: bool
    cron: str | None
    feishu_enabled: bool
    is_active: bool


@dataclass
class TickResult:
    evaluated_minute: datetime
    due_groups: list[str]
    ran: dict[str, str] = field(default_factory=dict)
    target_date: str = ""


@dataclass
class BackfillResult:
    group_id: str
    checked: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    skipped_empty: list[str] = field(default_factory=list)
    skipped_exists: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class DailyScheduler:
    """Cron-driven daily-report scheduler.

    Args:
        db_path: SQLite database path.
        tz: Timezone for cron evaluation + "today" derivation (IANA name).
        report_types: Report types passed to ``orchestrate`` (default ``["daily"]``).
        backfill_days: Default lookback window for startup back-fill.
        poll_interval_s: Daemon poll cadence (the loop also aligns to minute boundaries).
        max_parallel: Max concurrent group runs; None => ``settings.max_parallel_groups``.
        settings: Injected Settings (tests); None => ``get_settings()``.
    """

    def __init__(
        self,
        *,
        db_path: str,
        tz: str = "Asia/Shanghai",
        report_types: list[str] | None = None,
        backfill_days: int = 7,
        poll_interval_s: int = 60,
        max_parallel: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.db_path = db_path
        self.tz_name = tz
        self.tz = ZoneInfo(tz)
        self.report_types = list(report_types) if report_types else ["daily"]
        self.backfill_days = backfill_days
        self.poll_interval_s = poll_interval_s
        self._settings = settings or get_settings()
        mp = max_parallel if max_parallel is not None else self._settings.max_parallel_groups
        self._sem = asyncio.Semaphore(max(1, int(mp)))
        self._last_evaluated_minute: datetime | None = None

    # ── time ─────────────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        """Current time in the scheduler tz, truncated to the minute."""
        return datetime.now(self.tz).replace(second=0, microsecond=0)

    # ── group / report queries ───────────────────────────────────────────────

    async def _list_enabled_groups(self) -> list[GroupScheduleRow]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT group_id, display_name, chatroom_id, daily_report_enabled, "
                "daily_schedule_cron, feishu_enabled, is_active FROM groups "
                "WHERE is_active = 1 AND daily_report_enabled = 1"
            )
            rows = await cur.fetchall()
        return [
            GroupScheduleRow(
                group_id=r["group_id"],
                display_name=r["display_name"] or r["chatroom_id"] or r["group_id"],
                chatroom_id=r["chatroom_id"],
                enabled=bool(r["daily_report_enabled"]),
                cron=r["daily_schedule_cron"],
                feishu_enabled=bool(r["feishu_enabled"]),
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    async def _is_report_present(self, group_id: str, date_yyyymmdd: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(_REPORT_EXISTS_SQL, (group_id, date_yyyymmdd))
            return await cur.fetchone() is not None

    async def _missing_dates(self, group_id: str, window: tuple[date, date]) -> list[date]:
        """Dates in ``window`` (inclusive) with no report_versions row, oldest first."""
        start, end = window
        if end < start:
            return []
        all_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        all_yyyymmdd = {d.strftime("%Y%m%d") for d in all_dates}
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                _DATES_IN_WINDOW_SQL,
                (group_id, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
            )
            present = {row[0] for row in await cur.fetchall()}
        missing_yyyymmdd = all_yyyymmdd - present
        return sorted(d for d in all_dates if d.strftime("%Y%m%d") in missing_yyyymmdd)

    # ── data preflight + empty signal (mirrors batch_scheduler) ───────────────

    async def _check_data(self, group_id: str, date_iso: str) -> tuple[bool, int]:
        """Query the data source API for message availability.

        ``date_iso`` may be YYYY-MM-DD or YYYYMMDD. On API failure, conservatively
        returns ``(True, -1)`` so a flaky API never skips a day that may have data.
        """
        normalized = date_iso.replace("-", "") if "-" in date_iso else date_iso
        try:
            from z_winnow.pipeline.cipher_talk_client import create_data_client
            from z_winnow.pipeline.group_config import resolve_chatroom_id

            chatroom_id = await resolve_chatroom_id(group_id, self.db_path)
            async with create_data_client(
                base_url=self._settings.effective_data_base_url,
                token=self._settings.effective_data_token,
            ) as client:
                return await client.check_messages_count(
                    chatroom_id=chatroom_id, date=normalized, limit=1
                )
        except Exception as exc:
            logger.warning(
                "_check_data: API check failed group=%s date=%s — %s (will run pipeline)",
                group_id,
                normalized,
                exc,
            )
            return True, -1

    async def _register_empty(self, group_id: str, date_iso: str) -> None:
        from z_winnow.web.services.empty_day_signal import register_empty_day_signal

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await register_empty_day_signal(db, group_id, date_iso, db_path=self.db_path)
        except Exception as exc:
            logger.warning("_register_empty: failed group=%s date=%s — %s", group_id, date_iso, exc)

    # ── the reused run chain ─────────────────────────────────────────────────

    async def run_group_day(self, group_id: str, target_date_iso: str) -> str:
        """Generate (or skip) the report for one group + date.

        Args:
            group_id: Internal group UUID (g_xxx).
            target_date_iso: ``"YYYY-MM-DD"``.

        Returns one of: ``skipped_exists`` | ``skipped_empty`` | ``completed`` | ``failed``.
        """
        date_yyyymmdd = target_date_iso.replace("-", "")

        # 1. idempotency gate (single source of truth)
        if await self._is_report_present(group_id, date_yyyymmdd):
            logger.info("run_group_day: group=%s date=%s skipped_exists", group_id, date_yyyymmdd)
            return "skipped_exists"

        # 2. empty-data pre-check (avoid wasting an LLM call on a no-data day)
        has_data, _count = await self._check_data(group_id, target_date_iso)
        if not has_data:
            await self._register_empty(group_id, target_date_iso)
            logger.info("run_group_day: group=%s date=%s skipped_empty", group_id, date_yyyymmdd)
            return "skipped_empty"

        # 3. orchestrate
        try:
            from z_winnow.orchestrator import orchestrate
            from z_winnow.web.services.run_service import resolve_group_name

            group_name = await resolve_group_name(group_id, self.db_path)
            run_id = str(uuid.uuid4())
            await orchestrate(
                group_name=group_name,
                date=date_yyyymmdd,
                report_types=self.report_types,
                api_base_url=self._settings.effective_data_base_url,
                api_token=self._settings.effective_data_token,
                source="daily_run",
                run_id=run_id,
            )
        except Exception:
            logger.exception(
                "run_group_day: orchestrate failed group=%s date=%s", group_id, date_yyyymmdd
            )
            return "failed"

        # 4. auto-push to Feishu (fire-and-forget; never affects the run's terminal status)
        try:
            from z_winnow.web.services.report_service import auto_push_after_run

            await auto_push_after_run(
                group_id=group_id, date=target_date_iso, db_path=self.db_path, run_id=run_id
            )
        except Exception:
            logger.exception(
                "run_group_day: auto_push failed group=%s date=%s", group_id, date_yyyymmdd
            )

        logger.info("run_group_day: group=%s date=%s completed", group_id, date_yyyymmdd)
        return "completed"

    # ── per-minute tick ──────────────────────────────────────────────────────

    async def tick(self, now: datetime | None = None) -> TickResult:
        """Evaluate due groups for the current (or injected) minute and run them."""
        now_minute = (now or self._now()).astimezone(self.tz).replace(second=0, microsecond=0)
        if now_minute == self._last_evaluated_minute:
            return TickResult(evaluated_minute=now_minute, due_groups=[], ran={}, target_date="")
        self._last_evaluated_minute = now_minute

        target_iso = cron_util.yesterday_iso(now_minute)
        groups = await self._list_enabled_groups()
        due = [g for g in groups if cron_util.is_due(g.cron, now_minute)]
        ran: dict[str, str] = {}

        if due:

            async def _do(g: GroupScheduleRow) -> tuple[str, str]:
                async with self._sem:
                    return g.group_id, await self.run_group_day(g.group_id, target_iso)

            # return_exceptions=True yields list[tuple | BaseException]; narrow per item.
            outcomes: list[tuple[str, str] | BaseException] = await asyncio.gather(
                *[_do(g) for g in due], return_exceptions=True
            )
            for r in outcomes:
                if isinstance(r, BaseException):
                    logger.error("tick: run_group_day raised: %r", r)
                    continue
                gid, status = r
                ran[gid] = status

        await self._write_heartbeat(now_minute, {"due": len(due), "ran": ran, "target": target_iso})
        logger.info(
            "tick: %s target=%s due=%d ran=%s", now_minute.isoformat(), target_iso, len(due), ran
        )
        return TickResult(
            evaluated_minute=now_minute,
            due_groups=[g.group_id for g in due],
            ran=ran,
            target_date=target_iso,
        )

    # ── downtime recovery ────────────────────────────────────────────────────

    async def backfill(self, lookback_days: int | None = None) -> list[BackfillResult]:
        """Generate reports for any missing days in the lookback window (per group, oldest first)."""
        n = lookback_days if lookback_days is not None else self.backfill_days
        today = self._now().date()
        window = (today - timedelta(days=n), today - timedelta(days=1))  # inclusive, ends yesterday

        results: list[BackfillResult] = []
        for g in await self._list_enabled_groups():
            missing = await self._missing_dates(g.group_id, window)
            if not missing:
                continue
            br = BackfillResult(group_id=g.group_id)
            for d in sorted(missing):  # oldest -> newest
                iso = d.isoformat()
                br.checked.append(iso)
                status = await self.run_group_day(g.group_id, iso)
                _bucket(br, status).append(iso)
            results.append(br)
            logger.info(
                "backfill: group=%s checked=%d generated=%d empty=%d exists=%d failed=%d",
                g.group_id,
                len(br.checked),
                len(br.generated),
                len(br.skipped_empty),
                len(br.skipped_exists),
                len(br.failed),
            )
        return results

    # ── daemon loop ──────────────────────────────────────────────────────────

    async def run_forever(
        self,
        stop_event: asyncio.Event | None = None,
        *,
        run_startup_backfill: bool = True,
        do_preflight: bool = True,
    ) -> None:
        """Blocking daemon: preflight → backfill → tick every minute until stopped.

        Raises :class:`PreflightError` if a critical dependency is missing
        (so the CLI can render the failures instead of silently looping).
        """
        if do_preflight:
            report = await check_environment(self.db_path)
            if report.critical_failures:
                raise PreflightError(report)

        if run_startup_backfill:
            try:
                await self.backfill()
            except Exception:
                logger.exception("startup backfill failed (continuing to tick loop)")

        last: datetime | None = None
        while not (stop_event and stop_event.is_set()):
            now_minute = self._now()
            if now_minute != last:
                try:
                    await self.tick(now=now_minute)
                except Exception:
                    logger.exception("scheduler tick failed (will retry next minute)")
                last = now_minute
            await self._sleep_until_next_tick(stop_event)

    async def _sleep_until_next_tick(self, stop_event: asyncio.Event | None) -> None:
        """Sleep until the next minute boundary (or poll_interval_s), interruptible by stop_event."""
        now = datetime.now(self.tz)
        secs_to_next_minute = 60 - now.second
        wait = min(max(1, secs_to_next_minute), max(1, self.poll_interval_s))
        if stop_event is None:
            await asyncio.sleep(wait)
            return
        # interruptible: wake early if stopped (timeout = normal sleep completion)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=wait)

    # ── heartbeat ────────────────────────────────────────────────────────────

    async def _write_heartbeat(self, at: datetime, payload: dict[str, Any]) -> None:
        """Persist last-tick time + result so the dashboard can show scheduler liveness.

        Creates the table defensively (idempotent) so this works even before the
        canonical migration lands.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS scheduler_state "
                    "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)"
                )
                await db.execute(
                    "INSERT INTO scheduler_state(key, value, updated_at) VALUES('last_tick', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (
                        json.dumps({"at": at.isoformat(), **payload}, ensure_ascii=False),
                        at.isoformat(),
                    ),
                )
                await db.commit()
        except Exception:
            logger.debug("heartbeat write failed (non-fatal)", exc_info=True)


def _bucket(br: BackfillResult, status: str) -> list[str]:
    return {
        "completed": br.generated,
        "skipped_empty": br.skipped_empty,
        "skipped_exists": br.skipped_exists,
        "failed": br.failed,
    }.get(status, br.failed)  # unknown status -> failed bucket


__all__ = [
    "BackfillResult",
    "DailyScheduler",
    "GroupScheduleRow",
    "PreflightError",
    "TickResult",
]
