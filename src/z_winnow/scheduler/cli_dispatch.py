"""CLI dispatch for ``winnow scheduler`` (subcommand group).

Routes subactions to handlers. ``run`` owns its event loop (mirrors ``_cmd_web``
/ ``_cmd_mcp`` — NOT wrapped in asyncio.run by main()); the rest are async and
wrapped in asyncio.run. Bare ``winnow scheduler`` (no action) drops into the
interactive Rich menu.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console

from z_winnow.config.settings import get_settings
from z_winnow.scheduler import cron_util, views
from z_winnow.scheduler.engine import DailyScheduler, PreflightError
from z_winnow.scheduler.preflight import check_environment, try_auto_start_deps
from z_winnow.scheduler.status import get_scheduler_status

logger = logging.getLogger(__name__)
# The preflight probes fire several httpx requests (ciphertalk/memos/llm) whose
# INFO logs would clutter the Rich panels. Quiet them for scheduler commands.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
_console = Console()


def _db_path(args: argparse.Namespace) -> str:
    return getattr(args, "db", None) or get_settings().db_path


# ── dispatch ─────────────────────────────────────────────────────────────────


def _cmd_scheduler_dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "scheduler_action", None)
    if action is None:
        # bare `winnow scheduler` -> interactive menu
        return asyncio.run(_cmd_scheduler_menu(args))
    handlers = {
        "status": _cmd_scheduler_status,
        "set": _cmd_scheduler_set,
        "enable": _cmd_scheduler_enable,
        "disable": _cmd_scheduler_disable,
        "next": _cmd_scheduler_next,
        "doctor": _cmd_scheduler_doctor,
    }
    if action == "run":
        return _cmd_scheduler_run(args)  # blocking daemon — owns its loop
    fn = handlers.get(action)
    if fn is None:
        _console.print(f"[red]未知 scheduler 子命令: {action}[/red]")
        return 1
    return asyncio.run(fn(args))


# ── bare menu ────────────────────────────────────────────────────────────────


async def _cmd_scheduler_menu(args: argparse.Namespace) -> int:
    from z_winnow.scheduler.interactive import run_menu

    with contextlib.suppress(KeyboardInterrupt):
        await run_menu(_db_path(args))
    return 0


# ── status ───────────────────────────────────────────────────────────────────


async def _cmd_scheduler_status(args: argparse.Namespace) -> int:
    s = get_settings()
    lookback = args.window if args.window is not None else s.scheduler_lookback_days
    db = _db_path(args)

    async def _render():
        status = await get_scheduler_status(
            db, lookback_days=lookback, tz=s.scheduler_tz, group=args.group
        )
        _console.print(views.build_dashboard(status))

    if args.watch:
        _console.print("[dim]实时刷新（Ctrl+C 退出）…[/dim]")
        try:
            while True:
                await _render()
                await asyncio.sleep(3)
        except KeyboardInterrupt:
            _console.print("\n[dim]已退出[/dim]")
    else:
        await _render()
    return 0


# ── set / enable / disable ───────────────────────────────────────────────────


async def _resolve_group_id(group: str, db_path: str) -> str | None:
    from z_winnow.pipeline.group_config import resolve_group_id

    if group.startswith("g_"):
        return group
    try:
        return await resolve_group_id(group, db_path=db_path)
    except (ValueError, FileNotFoundError):
        return None


async def _cmd_scheduler_set(args: argparse.Namespace) -> int:
    import aiosqlite

    from z_winnow.web.services import group_service

    db = _db_path(args)
    gid = await _resolve_group_id(args.group, db)
    if not gid:
        _console.print(f"[red]找不到群: {args.group}[/red]")
        return 1

    patch: dict[str, object] = {}
    if args.cron is not None:
        if not cron_util.validate_cron(args.cron):
            _console.print(f"[red]cron 表达式无效: {args.cron}[/red]")
            return 1
        patch["daily_schedule_cron"] = args.cron
    if args.daily_enabled is not None:
        patch["daily_report_enabled"] = args.daily_enabled

    if not patch:
        _console.print("[yellow]未指定任何变更（用 --cron / --enable / --disable）[/yellow]")
        return 1

    async with aiosqlite.connect(db) as conn:
        updated = await group_service.update_group(conn, gid, patch)
    if updated is None:
        _console.print(f"[red]群不存在: {gid}[/red]")
        return 1
    _console.print(
        f"[green]✓ 已更新 {updated.display_name}[/green]  "
        f"cron={updated.daily_schedule_cron or '—'}  enabled={bool(updated.daily_report_enabled)}"
    )
    if updated.daily_schedule_cron and cron_util.validate_cron(updated.daily_schedule_cron):
        fires = cron_util.next_n(
            updated.daily_schedule_cron, datetime.now(ZoneInfo("Asia/Shanghai")), 3
        )
        if fires:
            _console.print(views.build_next_fires_table(updated.daily_schedule_cron, fires))
    return 0


async def _cmd_scheduler_enable(args: argparse.Namespace) -> int:
    args.daily_enabled = True
    args.cron = None
    return await _cmd_scheduler_set(args)


async def _cmd_scheduler_disable(args: argparse.Namespace) -> int:
    args.daily_enabled = False
    args.cron = None
    return await _cmd_scheduler_set(args)


# ── next ─────────────────────────────────────────────────────────────────────


async def _cmd_scheduler_next(args: argparse.Namespace) -> int:
    s = get_settings()
    db = _db_path(args)
    status = await get_scheduler_status(
        db, lookback_days=s.scheduler_lookback_days, tz=s.scheduler_tz, group=args.group
    )
    now = datetime.now(ZoneInfo(s.scheduler_tz))
    any_ = False
    for g in status.groups:
        if not cron_util.validate_cron(g.cron):
            _console.print(f"[yellow]{g.display_name}: cron 无效 ({g.cron})[/yellow]")
            continue
        assert g.cron is not None
        fires = cron_util.next_n(g.cron, now, args.count)
        if fires:
            any_ = True
            _console.print(views.build_next_fires_table(g.cron, fires))
    if not any_:
        _console.print("[yellow]没有配置有效 cron 的群[/yellow]")
    return 0


# ── doctor ───────────────────────────────────────────────────────────────────


async def _cmd_scheduler_doctor(args: argparse.Namespace) -> int:
    db = _db_path(args)
    report = await check_environment(db)
    _console.print(views.build_preflight_panel(report))
    if args.fix and report.critical_failures:
        _console.print("\n[bold]尝试一键拉起依赖…[/bold]")
        ok, out = await try_auto_start_deps()
        if ok:
            _console.print("[green]✓ 拉起完成，复检…[/green]")
            report = await check_environment(db)
            _console.print(views.build_preflight_panel(report))
        else:
            _console.print("[red]拉起失败：[/red]")
            _console.print(out[-1000:])
    return 0 if report.ok else 2


# ── run (daemon / --once) ────────────────────────────────────────────────────


async def _preflight_gate(args, *, allow_fix: bool) -> bool:
    """Run preflight; on critical failure, render + optionally fix. Returns True if OK to proceed."""
    if args.skip_preflight:
        _console.print("[yellow]已跳过环境预检 (--skip-preflight)[/yellow]")
        return True
    report = await check_environment(_db_path(args))
    if report.ok:
        return True
    _console.print(views.build_preflight_panel(report))
    if allow_fix and args.fix_deps:
        _console.print("\n[bold]一键拉起依赖…[/bold]")
        ok, out = await try_auto_start_deps()
        if ok:
            _console.print("[green]✓ 拉起完成，复检…[/green]")
            report = await check_environment(_db_path(args))
            _console.print(views.build_preflight_panel(report))
            return report.ok
        _console.print("[red]拉起失败，请手动处理：[/red]")
        _console.print(out[-1000:])
    return False


def _build_scheduler(args) -> DailyScheduler:
    s = get_settings()
    return DailyScheduler(
        db_path=_db_path(args),
        tz=s.scheduler_tz,
        report_types=[t.strip() for t in s.scheduler_default_report_types.split(",") if t.strip()]
        or ["daily"],
        backfill_days=args.backfill_days
        if args.backfill_days is not None
        else s.scheduler_backfill_days,
        poll_interval_s=args.poll_interval or s.scheduler_poll_interval_s,
        max_parallel=s.scheduler_max_parallel,
    )


def _cmd_scheduler_run(args: argparse.Namespace) -> int:
    """Blocking daemon (or single ``--once`` tick). Owns its event loop."""
    s = get_settings()

    async def _once() -> int:
        if not await _preflight_gate(args, allow_fix=True):
            return 2
        sched = _build_scheduler(args)
        now = None
        if args.now:
            try:
                now = datetime.fromisoformat(args.now)
            except ValueError:
                _console.print(
                    f"[red]--now 格式无效（应为 ISO8601，如 2026-07-23T02:00:00+08:00）: {args.now}[/red]"
                )
                return 1
        with _console.status("评估当前分钟…", spinner="dots"):
            res = await sched.tick(now=now)
        _console.print(
            f"[green]tick 完成[/green] 目标={res.target_date or '-'} 到点群={len(res.due_groups)} "
            f"结果={res.ran or '{}'}"
        )
        return 0

    async def _daemon() -> int:
        if not await _preflight_gate(args, allow_fix=True):
            return 2
        sched = _build_scheduler(args)
        _console.print(
            f"[bold cyan]调度守护启动[/bold cyan]  时区={s.scheduler_tz}  "
            f"轮询={sched.poll_interval_s}s  补跑={sched.backfill_days}天  目标=前一天\n"
            "[dim]Ctrl+C 停止。建议用 tmux/nohup/launchd/systemd 保活；或系统 cron 每分钟跑 `scheduler run --once`。[/dim]"
        )
        stop = asyncio.Event()

        def _sig(*_: object) -> None:
            stop.set()

        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(sig, _sig)
            except (NotImplementedError, RuntimeError):  # Windows / no running loop edge cases
                pass
        try:
            await sched.run_forever(
                stop, run_startup_backfill=not args.no_backfill, do_preflight=False
            )
        except KeyboardInterrupt:
            pass
        finally:
            _console.print("\n[dim]调度守护已停止[/dim]")
        return 0

    try:
        if args.once:
            return asyncio.run(_once())
        return asyncio.run(_daemon())
    except PreflightError as exc:
        _console.print(f"\n[bold red]启动被预检拦截:[/bold red] {exc}")
        _console.print(views.build_preflight_panel(exc.report))
        _console.print(
            "\n[dim]修复后重试，或加 --skip-preflight 强行启动 / --fix-deps 一键拉起。[/dim]"
        )
        return 2


__all__ = ["_cmd_scheduler_dispatch"]
