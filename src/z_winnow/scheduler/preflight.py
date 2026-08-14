"""Environment preflight for the daily-report scheduler.

Goal: never let the scheduler run blind. Before ``run`` starts (and on demand via
``winnow scheduler doctor``), probe every dependency the pipeline needs and
report a structured pass/fail with a concrete fix hint — so "forgot to start the
Docker containers" fails loudly here instead of silently 404'ing deep in MemOS.

Reuse policy:
  - ciphertalk / memos / llm connectivity -> ``probe_connectivity`` (one call, 8s
    timeout, never raises). DRY with the web config-test endpoint.
  - Docker daemon + container liveness -> ``docker info`` / ``docker ps``
    (pattern lifted from ``.claude/skills/winnow-dev/scripts/check_env.py``).
    No Docker-level healthcheck exists for qdrant/redis/memos-api, so we can NOT
    trust ``docker ps`` alone — we also HTTP-probe MemOS + the Qdrant collection.
  - Qdrant collection -> httpx GET (pattern from ``scripts/clear_all.py``).
  - DB -> aiosqlite connect.

Mock awareness: when ``settings.use_mock_memos`` / ``use_mock_llm`` are True, the
Docker/containers/Qdrant/memos/llm probes are skipped (a mock run needs neither).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

# Stable check names — the dashboard and tests key off these strings.
DOCKER = "docker"
CONTAINERS = "memos_containers"
QDRANT = "qdrant_collection"
MEMOS = "memos_api"
CIPHERTALK = "ciphertalk"
LLM = "llm"
DB = "db"

# Expected MemOS containers (docker-compose.yml container_name values).
_EXPECTED_CONTAINERS = ("winnow-qdrant", "winnow-redis", "winnow-neo4j", "winnow-memos-api")
_QDRANT_COLLECTION_URL = "http://127.0.0.1:6333/collections/neo4j_vec_db"

# Fix hints (shown verbatim on the board / in the doctor panel).
_HINT_DOCKER = "启动 Docker Desktop 或: colima start --cpu 4 --memory 6"
_HINT_CONTAINERS = (
    "cd deployments && docker compose --env-file ../.env up qdrant redis neo4j memos-api -d"
)
_HINT_START_ALL = "bash .claude/skills/winnow-dev/scripts/start_all.sh --no-web   (一键拉起 Colima+四容器+Qdrant collection)"
_HINT_QDRANT = (
    "curl -X PUT http://127.0.0.1:6333/collections/neo4j_vec_db "
    '-H \'Content-Type: application/json\' -d \'{"vectors":{"size":3072,"distance":"Cosine"}}\''
)


@dataclass
class CheckResult:
    """One dependency check."""

    name: str
    status: str  # "ok" | "fail" | "warn" | "skip"
    detail: str
    fix_hint: str = ""
    critical: bool = False  # a fail here blocks the scheduler from starting

    @property
    def icon(self) -> str:
        return {"ok": "✓", "fail": "✗", "warn": "⚠", "skip": "–"}.get(self.status, "?")


@dataclass
class PreflightReport:
    """Aggregated preflight result."""

    checks: list[CheckResult] = field(default_factory=list)

    def add(self, res: CheckResult) -> None:
        self.checks.append(res)

    @property
    def by_name(self) -> dict[str, CheckResult]:
        return {c.name: c for c in self.checks}

    @property
    def critical_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.critical and c.status == "fail"]

    @property
    def ok(self) -> bool:
        return not self.critical_failures

    def get(self, name: str) -> CheckResult | None:
        return self.by_name.get(name)


# ── individual probes (each total: never raises) ────────────────────────────


def _check_docker_daemon() -> CheckResult:
    docker = shutil.which("docker")
    if not docker:
        return CheckResult(DOCKER, "fail", "未找到 docker 可执行文件", _HINT_DOCKER, critical=True)
    try:
        r = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            server = ""
            for line in r.stdout.splitlines():
                if "Server Version:" in line:
                    server = line.split("Server Version:", 1)[1].strip()
                    break
            colima_note = ""
            if shutil.which("colima"):
                try:
                    cr = subprocess.run(
                        ["colima", "status"], capture_output=True, text=True, timeout=5
                    )
                    if cr.returncode == 0:
                        colima_note = " (via Colima)"
                except Exception:
                    pass
            return CheckResult(DOCKER, "ok", f"Server {server or 'running'}{colima_note}".strip())
        return CheckResult(DOCKER, "fail", "Docker 守护进程未运行", _HINT_DOCKER, critical=True)
    except Exception as exc:  # pragma: no cover
        return CheckResult(DOCKER, "fail", f"docker info 失败: {exc}", _HINT_DOCKER, critical=True)


def _check_containers() -> CheckResult:
    docker = shutil.which("docker")
    if not docker:
        return CheckResult(CONTAINERS, "fail", "未找到 docker", _HINT_DOCKER, critical=True)
    try:
        r = subprocess.run(
            [docker, "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        running: dict[str, str] = {}
        for line in r.stdout.strip().splitlines():
            if "\t" in line:
                name, status = line.split("\t", 1)
                running[name] = status
        missing = [c for c in _EXPECTED_CONTAINERS if c not in running]
        if not missing:
            return CheckResult(
                CONTAINERS,
                "ok",
                f"{len(_EXPECTED_CONTAINERS)}/{len(_EXPECTED_CONTAINERS)} 容器运行中",
            )
        detail = f"缺少 {len(missing)}/{len(_EXPECTED_CONTAINERS)}: {', '.join(missing)}"
        return CheckResult(CONTAINERS, "fail", detail, _HINT_CONTAINERS, critical=True)
    except Exception as exc:  # pragma: no cover
        return CheckResult(
            CONTAINERS, "fail", f"docker ps 失败: {exc}", _HINT_CONTAINERS, critical=True
        )


async def _check_qdrant_collection() -> CheckResult:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_QDRANT_COLLECTION_URL)
        if resp.status_code == 200:
            data = resp.json()
            pts = (data.get("result") or {}).get("points_count", "?")
            return CheckResult(QDRANT, "ok", f"neo4j_vec_db 存在 ({pts} points)")
        if resp.status_code == 404:
            return CheckResult(
                QDRANT,
                "warn",
                "collection neo4j_vec_db 不存在 (写入会 404)",
                _HINT_QDRANT,
                critical=True,
            )
        return CheckResult(QDRANT, "warn", f"Qdrant 返回 {resp.status_code}", _HINT_QDRANT)
    except Exception as exc:
        return CheckResult(QDRANT, "fail", f"Qdrant 不可达: {exc}", _HINT_CONTAINERS, critical=True)


def _probe_to_result(name: str, raw: str, *, critical: bool, ok_label: str) -> CheckResult:
    """Translate a probe_connectivity string ("ok" | "fail: …" | "skipped: …")."""
    if raw.startswith("ok"):
        return CheckResult(name, "ok", ok_label, critical=critical)
    if raw.startswith("skip"):
        # skipped = not configured; treat as warn (non-fatal) unless caller marks critical
        return CheckResult(name, "warn", raw, critical=False)
    return CheckResult(name, "fail", raw.removeprefix("fail:").strip() or raw, critical=critical)


async def _check_db(db_path: str) -> CheckResult:
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("SELECT 1")
        return CheckResult(DB, "ok", str(db_path), critical=True)
    except Exception as exc:
        return CheckResult(
            DB, "fail", f"无法打开 SQLite: {exc}", "检查 db_path / 权限", critical=True
        )


# ── aggregate ────────────────────────────────────────────────────────────────


async def check_environment(
    db_path: str,
    *,
    real_llm: bool | None = None,
    real_memos: bool | None = None,
) -> PreflightReport:
    """Run all dependency checks and return a structured report.

    Args:
        db_path: SQLite database path to verify.
        real_llm: force LLM-probe on/off; None = ``not settings.use_mock_llm``.
        real_memos: force MemOS/Docker probes on/off; None = ``not settings.use_mock_memos``.

    Returns:
        :class:`PreflightReport`; ``report.ok`` is True when no *critical* check failed.
    """
    from z_winnow.config.settings import get_settings

    s = get_settings()
    if real_memos is None:
        real_memos = not s.use_mock_memos
    if real_llm is None:
        real_llm = not s.use_mock_llm

    report = PreflightReport()

    # --- data source (always needed, even in mock modes) ---
    probes = await _safe_probe(["ciphertalk"])
    report.add(
        _probe_to_result(
            CIPHERTALK,
            probes.get("ciphertalk", "fail: 探测未运行"),
            critical=True,
            ok_label="数据源可达",
        )
    )

    # --- real-memos-only stack: docker + containers + qdrant + memos api ---
    if real_memos:
        report.add(_check_docker_daemon())
        # Only check containers if docker itself is up (avoids a misleading double-fail).
        if report.get(DOCKER) and report.get(DOCKER).status == "ok":  # type: ignore[union-attr]
            report.add(_check_containers())
        else:
            report.add(
                CheckResult(
                    CONTAINERS, "fail", "Docker 未运行，跳过容器探活", _HINT_DOCKER, critical=True
                )
            )
        report.add(await _check_qdrant_collection())
        mprobes = await _safe_probe(["memos"])
        report.add(
            _probe_to_result(
                MEMOS,
                mprobes.get("memos", "fail: 探测未运行"),
                critical=True,
                ok_label="memos-api 可达",
            )
        )
    else:
        report.add(CheckResult(CONTAINERS, "skip", "mock memos 模式，跳过 Docker/容器"))
        report.add(CheckResult(MEMOS, "skip", "mock memos 模式"))
        report.add(CheckResult(QDRANT, "skip", "mock memos 模式"))

    # --- LLM ---
    if real_llm:
        lprobes = await _safe_probe(["llm"])
        report.add(
            _probe_to_result(
                LLM, lprobes.get("llm", "fail: 探测未运行"), critical=True, ok_label="LLM 可达"
            )
        )
    else:
        report.add(CheckResult(LLM, "skip", "mock llm 模式"))

    # --- DB (always) ---
    report.add(await _check_db(db_path))

    return report


async def _safe_probe(targets: list[str]) -> dict[str, str]:
    """Run probe_connectivity defensively; return {} on any import/runtime error."""
    try:
        from z_winnow.web.services.config_service import probe_connectivity

        return await probe_connectivity({}, targets)
    except Exception as exc:  # pragma: no cover
        logger.warning("preflight: probe_connectivity failed for %s: %s", targets, exc)
        return dict.fromkeys(targets, f"fail: {exc}")


# ── auto-start deps ──────────────────────────────────────────────────────────


def _project_root() -> Path | None:
    """Locate project root (dir containing pyproject.toml) from this package."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


async def try_auto_start_deps() -> tuple[bool, str]:
    """Best-effort: start Colima + the four MemOS containers + ensure Qdrant collection.

    Prefers the project's ``start_all.sh --no-web`` (handles Colima, container
    start, memos-api readiness wait, and collection creation). Falls back to a
    bare ``docker compose up``. Returns ``(success, captured_output)``.
    """
    root = _project_root()
    if root is None:
        return False, "无法定位项目根目录（找不到 pyproject.toml）"

    script = root / ".claude" / "skills" / "winnow-dev" / "scripts" / "start_all.sh"

    def _run() -> tuple[bool, str]:
        env = {**os.environ, "WINNOW_PROJECT_ROOT": str(root)}
        if script.is_file():
            cmd: list[str] = ["bash", str(script), "--no-web"]
            cwd = str(root)
        else:
            # Fallback: bring up the four containers directly.
            deployments = root / "deployments"
            if not deployments.is_dir():
                return False, f"找不到 {script} 且无 deployments/ 目录"
            dc = shutil.which("docker") or "docker"
            cmd = [
                dc,
                "compose",
                "--env-file",
                "../.env",
                "up",
                "qdrant",
                "redis",
                "neo4j",
                "memos-api",
                "-d",
            ]
            cwd = str(deployments)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=cwd, env=env)
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            return r.returncode == 0, out.strip()
        except Exception as exc:  # pragma: no cover
            return False, f"启动失败: {exc}"

    return await asyncio.to_thread(_run)


def to_compact_line(report: PreflightReport) -> str:
    """One-line health summary for the dashboard footer, e.g. 'Docker✓ 容器4/4✓ ...'."""
    parts: list[str] = []
    label = {
        DOCKER: "Docker",
        CONTAINERS: "容器",
        QDRANT: "Qdrant",
        MEMOS: "memos",
        CIPHERTALK: "数据源",
        LLM: "LLM",
        DB: "DB",
    }
    for name, lab in label.items():
        c = report.get(name)
        if c is None or c.status == "skip":
            continue
        parts.append(f"{lab}{c.icon}")
    return "  ".join(parts) if parts else "环境: (无检查)"


__all__: list[str] = [
    "CIPHERTALK",
    "CONTAINERS",
    "DB",
    "DOCKER",
    "LLM",
    "MEMOS",
    "QDRANT",
    "CheckResult",
    "PreflightReport",
    "check_environment",
    "to_compact_line",
    "try_auto_start_deps",
]
