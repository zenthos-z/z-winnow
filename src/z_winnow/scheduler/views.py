"""Rich renderers for the scheduler CLI (pure presentation — no I/O, no control flow).

Keeping rendering separate from :mod:`interactive` (control flow) lets us unit-test
the board output by rendering into a string-backed Console, and lets the web layer
reuse the underlying :mod:`status` data without pulling Rich in.

Every builder returns a Rich renderable; callers ``console.print(...)`` it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from z_winnow.scheduler import cron_util
from z_winnow.scheduler.preflight import PreflightReport, to_compact_line
from z_winnow.scheduler.status import GroupStatus, SchedulerStatus

__all__ = [
    "build_dashboard",
    "build_group_detail",
    "build_next_fires_table",
    "build_preflight_panel",
    "render_to_str",
]


def render_to_str(renderable: object, *, width: int = 96) -> str:
    """Render a Rich object to a plain string (for tests / logs)."""
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    Console(file=buf, width=width, color_system=None, force_terminal=False).print(renderable)
    return buf.getvalue()


# ── dashboard ────────────────────────────────────────────────────────────────


def _enable_badge(g: GroupStatus) -> Text:
    if not g.is_active:
        return Text("○未激活", style="dim")
    return Text("●启用", style="green") if g.enabled else Text("○停用", style="dim")


def _missing_cell(g: GroupStatus) -> Text:
    if not g.enabled:
        return Text("—", style="dim")
    if g.missing_days == 0:
        return Text("0", style="green")
    if g.missing_days <= 2:
        return Text(f"{g.missing_days} ⚠", style="yellow")
    return Text(f"{g.missing_days} ⚠", style="red")


def _next_fire_cell(g: GroupStatus) -> Text:
    if not g.cron_valid or not g.next_fire:
        return Text("—", style="red" if g.cron and not g.cron_valid else "dim")
    # Compact "07-24 02:00"
    try:
        dt_ = datetime.fromisoformat(g.next_fire)
        return Text(dt_.strftime("%m-%d %H:%M"))
    except ValueError:
        return Text(g.next_fire or "—")


def _last_run_cell(g: GroupStatus) -> Text:
    if not g.last_run_date:
        return Text("—", style="dim")
    # last_run_date is YYYYMMDD
    try:
        d = datetime.strptime(g.last_run_date, "%Y%m%d")
        return Text("✓ " + d.strftime("%m-%d"), style="green")
    except ValueError:
        return Text("✓ " + g.last_run_date, style="green")


def build_dashboard(status: SchedulerStatus, preflight: PreflightReport | None = None) -> Panel:
    """The main status board: per-group table + daemon/health footer."""
    table = Table(
        show_header=True, header_style="bold", expand=True, show_lines=False, pad_edge=False
    )
    table.add_column("#", width=3, style="dim")
    table.add_column("群组")
    table.add_column("状态", width=8)
    table.add_column("Cron", width=12)
    table.add_column("下次触发", width=12)
    table.add_column("上次", width=10)
    table.add_column("缺失", width=8, justify="right")

    for i, g in enumerate(status.groups, 1):
        cron_txt = g.cron if g.cron else Text("(未配)", style="dim")
        if g.cron and not g.cron_valid:
            cron_txt = Text(g.cron, style="red")
        table.add_row(
            str(i),
            g.display_name,
            _enable_badge(g),
            cron_txt if isinstance(cron_txt, (Text, str)) else str(cron_txt),
            _next_fire_cell(g),
            _last_run_cell(g),
            _missing_cell(g),
        )

    if not status.groups:
        table.add_row("—", "[dim]尚未注册任何群组[/dim]", "", "", "", "", "")

    # Footer: daemon liveness + env health.
    liveness_style = {"running": "green", "stale": "yellow", "unknown": "dim"}.get(
        status.daemon_liveness, "dim"
    )
    footer_lines = [
        f"调度器: [bold]{'●' if status.daemon_liveness == 'running' else '○'}[/bold] "
        f"[{liveness_style}]{status.daemon_liveness}[/{liveness_style}]"
        + (f"  上次心跳 {status.daemon_last_tick}" if status.daemon_last_tick else "")
        + f"  时区 {status.tz}  目标=[dim]{status.target_mode}[/dim]"
    ]
    if preflight is not None:
        footer_lines.append("环境: " + to_compact_line(preflight))

    title = "[bold cyan]定时日报调度[/bold cyan]"
    return Panel(
        table, title=title, subtitle="\n".join(footer_lines), border_style="cyan", padding=(0, 1)
    )


# ── preflight / doctor panel ─────────────────────────────────────────────────


def build_preflight_panel(report: PreflightReport) -> Panel:
    table = Table(
        show_header=True, header_style="bold", expand=True, show_lines=False, pad_edge=False
    )
    table.add_column("", width=3)
    table.add_column("检查项", width=18)
    table.add_column("状态", width=8)
    table.add_column("详情")
    table.add_column("修复", overflow="fold")

    label = {
        "docker": "Docker 守护",
        "memos_containers": "MemOS 容器",
        "qdrant_collection": "Qdrant collection",
        "memos_api": "memos-api",
        "ciphertalk": "数据源",
        "llm": "LLM",
        "db": "SQLite DB",
    }
    for c in report.checks:
        style = {"ok": "green", "fail": "bold red", "warn": "yellow", "skip": "dim"}.get(
            c.status, ""
        )
        crit = " [red](关键)[/red]" if c.critical else ""
        table.add_row(
            c.icon,
            label.get(c.name, c.name),
            f"[{style}]{c.status}[/{style}]{crit}",
            c.detail,
            c.fix_hint or "",
        )

    verdict = (
        "[bold green]✓ 全部就绪[/bold green]"
        if report.ok
        else f"[bold red]✗ {len(report.critical_failures)} 项关键缺失，调度器将拒绝启动[/bold red]"
    )
    return Panel(
        table,
        title="[bold]环境体检[/bold]",
        subtitle=verdict,
        border_style="green" if report.ok else "red",
    )


# ── next fires ───────────────────────────────────────────────────────────────


def build_next_fires_table(cron: str, fires: Sequence[datetime]) -> Table:
    table = Table(title=f"未来触发 — {cron}", show_header=True, header_style="bold", pad_edge=False)
    table.add_column("#", width=3, style="dim")
    table.add_column("时间")
    table.add_column("星期", width=6)
    for i, f in enumerate(fires, 1):
        table.add_row(
            str(i),
            f.strftime("%Y-%m-%d %H:%M"),
            ["一", "二", "三", "四", "五", "六", "日"][f.weekday()],
        )
    return table


# ── group detail ─────────────────────────────────────────────────────────────


def build_group_detail(g: GroupStatus, cron_human: str | None = None) -> Panel:
    human = cron_human or cron_util.cron_to_human(g.cron)
    lines = [
        f"[bold]{g.display_name}[/bold]  [dim]{g.group_id}[/dim]",
        f"状态: {_enable_badge(g).plain}   定时: {human}   [dim]({g.cron or '未配置'})[/dim]",
        f"下次触发: {g.next_fire or '—'}",
        f"上次运行: {g.last_run_date or '—'}   缺失天数(回看): {g.missing_days}",
    ]
    if g.cron and not g.cron_valid:
        lines.append("[red]⚠ cron 表达式无效，调度器会跳过此群[/red]")
    return Panel("\n".join(lines), border_style="cyan", padding=(0, 1))
