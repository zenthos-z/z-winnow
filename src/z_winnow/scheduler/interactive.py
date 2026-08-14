"""Interactive Rich wizard for ``winnow scheduler`` (bare command).

Control-flow layer over :mod:`views` (rendering), :mod:`status` (data),
:mod:`engine` (execution), and :mod:`preflight` (env checks). Plain ``input()``
prompts + Rich rendering — intentionally not a full TUI (no textual), to stay
lightweight and scriptable-friendly.

Entry: :func:`run_menu` — shows the dashboard, then loops on a small command set
(select group by number / refresh / watch / next-fire / env-check / quit).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from rich.console import Console
from rich.prompt import Confirm, Prompt

from z_winnow.config.settings import Settings, get_settings
from z_winnow.scheduler import cron_util, views
from z_winnow.scheduler.engine import DailyScheduler
from z_winnow.scheduler.preflight import (
    PreflightReport,
    check_environment,
    try_auto_start_deps,
)
from z_winnow.scheduler.status import GroupStatus, SchedulerStatus, get_scheduler_status

logger = logging.getLogger(__name__)

__all__ = ["run_menu"]


def _prompt(console: Console, text: str, default: str = "") -> str:
    try:
        return Prompt.ask(text, default=default, console=console).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


async def run_menu(
    db_path: str, *, settings: Settings | None = None, console: Console | None = None
) -> None:
    """Top-level interactive loop. Exits on 'q' / EOF / Ctrl-C."""
    settings = settings or get_settings()
    console = console or Console()
    tz = settings.scheduler_tz
    lookback = settings.scheduler_lookback_days

    console.print("[bold cyan]winnow 定时日报调度 — 交互菜单[/bold cyan]")
    console.print("[dim]提示: 输入序号选群管理；r=刷新 e=环境体检 n=未来触发 w=实时 q=退出[/dim]\n")

    cached_preflight: PreflightReport | None = None
    while True:
        try:
            status = await get_scheduler_status(db_path, lookback_days=lookback, tz=tz)
        except Exception as exc:
            console.print(f"[red]读取调度状态失败：{exc}[/red]")
            return
        console.print(views.build_dashboard(status, cached_preflight))
        choice = _prompt(console, "\n选择 (序号/r/w/n/e/q)").lower()
        if choice in ("q", "quit", "exit", ""):
            console.print("[dim]再见[/dim]")
            return
        if choice == "r":
            continue
        if choice == "e":
            cached_preflight = await _env_check_flow(console, db_path, settings)
            continue
        if choice == "n":
            await _show_next_fires_all(console, status, tz)
            continue
        if choice == "w":
            await _watch_loop(console, db_path, lookback, tz, cached_preflight)
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(status.groups):
                await _group_menu(console, db_path, status.groups[idx], settings)
            else:
                console.print("[yellow]序号超出范围[/yellow]")
            continue
        console.print("[yellow]未知命令[/yellow]")


# ── group management submenu ─────────────────────────────────────────────────


async def _group_menu(console: Console, db_path: str, g: GroupStatus, settings: Settings) -> None:
    while True:
        console.print(views.build_group_detail(g, cron_human=cron_util.cron_to_human(g.cron)))
        console.print(
            "[bold]1)[/bold]改触发时间  [bold]2)[/bold]启/停  [bold]3)[/bold]立即跑一次  [bold]4)[/bold]看未来触发  [bold]5)[/bold]返回"
        )
        c = _prompt(console, "操作").strip()
        if c == "1":
            await _change_time(console, db_path, g)
            return  # back to dashboard to refresh
        if c == "2":
            await _toggle_enabled(console, db_path, g)
            return
        if c == "3":
            await _run_now(console, db_path, g, settings)
            continue
        if c == "4":
            await _show_next_fires(console, g, settings.scheduler_tz)
            continue
        if c in ("5", "q", ""):
            return
        console.print("[yellow]未知操作[/yellow]")


async def _change_time(console: Console, db_path: str, g: GroupStatus) -> None:
    console.print("[bold]触发时间预设:[/bold]")
    console.print(
        "  1) 凌晨 2:00   2) 早 9:00   3) 晚 20:00   4) 自定义 HH:MM   5) 原始 cron 表达式"
    )
    c = _prompt(console, "选择").strip()
    presets = list(cron_util.PRESETS.items())
    new_cron: str | None = None
    if c in ("1", "2", "3"):
        new_cron = presets[int(c) - 1][1]
    elif c == "4":
        hhmm = _prompt(console, "时间 (HH:MM)")
        new_cron = cron_util.time_to_cron(hhmm)
        if new_cron is None:
            console.print("[red]时间格式无效（应为 HH:MM，如 07:30）[/red]")
            return
    elif c == "5":
        expr = _prompt(console, "cron 表达式 (m h dom mon dow)", default=g.cron or "")
        if not cron_util.validate_cron(expr):
            console.print("[red]cron 表达式无效，已取消[/red]")
            return
        new_cron = expr
    else:
        return

    # Preview next 3 fires before committing.
    assert new_cron is not None
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    fires = cron_util.next_n(new_cron, now, 3)
    if fires:
        console.print(views.build_next_fires_table(new_cron, fires))
    if not Confirm.ask(f"保存 {new_cron} 到 [{g.display_name}]?", default=True, console=console):
        console.print("[dim]已取消[/dim]")
        return
    await _update_group(db_path, g.group_id, {"daily_schedule_cron": new_cron})
    console.print(f"[green]✓ 已保存：{cron_util.cron_to_human(new_cron)}[/green]")


async def _toggle_enabled(console: Console, db_path: str, g: GroupStatus) -> None:
    if g.enabled:
        if Confirm.ask(f"关闭 [{g.display_name}] 的定时日报?", default=False, console=console):
            await _update_group(db_path, g.group_id, {"daily_report_enabled": False})
            console.print("[green]✓ 已停用[/green]")
        return
    # enabling: require a valid cron
    if not cron_util.validate_cron(g.cron):
        console.print(
            "[yellow]⚠ 该群尚未配置有效 cron，请先用「改触发时间」设置后再启用。[/yellow]"
        )
        return
    if Confirm.ask(f"启用 [{g.display_name}] 的定时日报?", default=True, console=console):
        await _update_group(db_path, g.group_id, {"daily_report_enabled": True})
        console.print("[green]✓ 已启用[/green]")


async def _run_now(console: Console, db_path: str, g: GroupStatus, settings: Settings) -> None:
    tz = ZoneInfo(settings.scheduler_tz)
    target = cron_util.yesterday_iso(datetime.now(tz))
    console.print(f"[dim]立即生成 {g.display_name} · {target}（前一天）…[/dim]")
    sched = DailyScheduler(
        db_path=db_path,
        tz=settings.scheduler_tz,
        report_types=[
            t.strip() for t in settings.scheduler_default_report_types.split(",") if t.strip()
        ]
        or ["daily"],
        backfill_days=settings.scheduler_backfill_days,
    )
    with console.status("生成中…", spinner="dots"):
        status = await sched.run_group_day(g.group_id, target)
    style = {
        "completed": "green",
        "skipped_exists": "yellow",
        "skipped_empty": "yellow",
        "failed": "red",
    }.get(status, "white")
    console.print(f"[{style}]结果: {status}[/{style}]")


async def _show_next_fires(console: Console, g: GroupStatus, tz: str) -> None:
    if not cron_util.validate_cron(g.cron):
        console.print("[red]cron 无效，无法计算未来触发[/red]")
        return
    assert g.cron is not None
    now = datetime.now(ZoneInfo(tz))
    fires = cron_util.next_n(g.cron, now, 5)
    if fires:
        console.print(views.build_next_fires_table(g.cron, fires))
    else:
        console.print("[yellow]无法计算[/yellow]")


async def _show_next_fires_all(console: Console, status: SchedulerStatus, tz: str) -> None:
    now = datetime.now(ZoneInfo(tz))
    any_ = False
    for g in status.groups:
        if not cron_util.validate_cron(g.cron):
            continue
        assert g.cron is not None
        fires = cron_util.next_n(g.cron, now, 3)
        if fires:
            any_ = True
            console.print(views.build_next_fires_table(g.cron, fires))
    if not any_:
        console.print("[yellow]没有配置有效 cron 的群[/yellow]")


async def _watch_loop(
    console: Console, db_path: str, lookback: int, tz: str, preflight: PreflightReport | None
) -> None:
    """A short live-refresh loop (fixed N ticks) — not a persistent Live widget, to stay simple."""
    console.print("[dim]实时刷新中（每 ~3s，共 20 次；Ctrl+C 退出）…[/dim]")
    try:
        for _ in range(20):
            status = await get_scheduler_status(db_path, lookback_days=lookback, tz=tz)
            console.print(views.build_dashboard(status, preflight))
            await asyncio.sleep(3)
    except KeyboardInterrupt:
        console.print("\n[dim]已退出实时模式[/dim]")


# ── env check ────────────────────────────────────────────────────────────────


async def _env_check_flow(console: Console, db_path: str, settings: Settings) -> PreflightReport:
    with console.status("环境体检中…", spinner="dots"):
        report = await check_environment(db_path)
    console.print(views.build_preflight_panel(report))
    if report.critical_failures and Confirm.ask(
        "检测到关键依赖缺失，是否尝试一键拉起 (start_all.sh --no-web)?",
        default=True,  # type: ignore[arg-type]  # mypy false-positive: rich Confirm default is bool
        console=console,
    ):
        with console.status("拉起依赖中…", spinner="dots"):
            ok, out = await try_auto_start_deps()
        if ok:
            console.print("[green]✓ 依赖拉起完成，复检…[/green]")
            with console.status("复检中…", spinner="dots"):
                report = await check_environment(db_path)
            console.print(views.build_preflight_panel(report))
        else:
            console.print("[red]拉起失败，请手动处理：[/red]")
            console.print(out[-800:])
    return report


# ── helpers ──────────────────────────────────────────────────────────────────


async def _update_group(db_path: str, group_id: str, patch: dict) -> None:
    """Persist a config change via the existing service path (no raw SQL)."""
    from z_winnow.web.services import group_service

    async with aiosqlite.connect(db_path) as db:
        await group_service.update_group(db, group_id, patch)
