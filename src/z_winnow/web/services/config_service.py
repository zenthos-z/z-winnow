"""Config write service — backs the onboarding wizard's「保存并重启」.

Two write targets (see plans/gleaming-drifting-frost.md):
- ``data/config_overrides.json`` — app-level Settings overrides (incl. secrets),
  applied by ``get_settings()`` as constructor kwargs (highest priority, beats
  env vars / .env).
- project-root ``.env`` — memos-api container infra vars (QDRANT_URL /
  REDIS_URL / REDIS_PASSWORD) consumed by docker-compose.

Safety: validate-before-persist (``Settings(**candidate)`` must construct),
atomic write + ``.bak``, secret merge semantics (None=keep, ""=clear),
safe-boot on corrupt override file (handled in settings._load_overrides).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from z_winnow.config.settings import (
    _OVERRIDE_PATH,
    Settings,
    get_settings,
    reset_settings,
)

logger = logging.getLogger(__name__)

# Wizard must not touch these via「保存并重启」.
# - web_port/web_host: browser cannot follow a new port/host.
# - sqlite_db_path: read-only @property (must go through db_path).
# - web_api_key: changing it via the wizard would lock the user out (login uses it).
_FORBIDDEN_FIELDS = frozenset({"web_port", "web_host", "sqlite_db_path", "web_api_key"})

# Infra vars written to project .env for the memos-api container (compose-sourced).
_INFRA_KEYS = frozenset(
    {"QDRANT_URL", "QDRANT_HOST", "QDRANT_PORT", "REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"}
)

_write_lock = threading.Lock()


# ── override file (app Settings) ──────────────────────────────
def _read_override_raw() -> dict[str, Any]:
    """Read data/config_overrides.json as a raw dict (safe-boot: {} on any error)."""
    try:
        if not _OVERRIDE_PATH.exists():
            return {}
        raw = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # safe-boot
        logger.warning("override read failed (%s); treating as empty", exc)
        return {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write: .bak backup → tempfile → chmod 0600 → os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.parent / (path.name + ".bak")
        bak.write_bytes(path.read_bytes())
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def apply_config_update(
    values: dict[str, Any],
    infra: dict[str, str] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate + persist a config delta from the wizard.

    Args:
        values: {Settings field_name: value}. Omitted key = keep existing.
                value None = keep; "" = explicit clear.
        infra: {compose env var: value} for memos-api container (.env).
        dry_run: validate only, do not write.

    Returns:
        {ok, applied_fields, infra_written, warnings}.

    Raises:
        ValueError: unknown/forbidden field, or invalid value details.
        ValidationError: if the merged config fails Settings validation.
    """
    infra = infra or {}
    values = {k: v for k, v in values.items() if v is not None}  # None → keep

    # reject forbidden / unknown field names
    forbidden = _FORBIDDEN_FIELDS & values.keys()
    if forbidden:
        raise ValueError(f"不可通过向导修改的字段: {sorted(forbidden)}")
    valid_fields = set(Settings.model_fields)
    unknown = set(values) - valid_fields
    if unknown:
        raise ValueError(f"未知的配置字段: {sorted(unknown)}")

    # merge onto existing override baseline (preserves unprovided secrets)
    candidate = _read_override_raw()
    candidate.update(values)  # "" (clear) also flows through here

    # VALIDATE before any write — bad values must not brick boot
    try:
        Settings(**candidate)
    except ValidationError as exc:
        # surface a compact, human-readable error
        raise ValueError(_format_validation_error(exc)) from exc

    applied_fields = sorted(values.keys())
    if dry_run:
        return {"ok": True, "applied_fields": applied_fields, "infra_written": [], "warnings": ["dry-run 未写入"]}

    with _write_lock:
        _atomic_write_json(_OVERRIDE_PATH, candidate)
        reset_settings()  # next get_settings() re-reads the override file
        infra_written: list[str] = []
        warnings: list[str] = []
        if infra:
            bad_infra = set(infra) - _INFRA_KEYS
            if bad_infra:
                raise ValueError(f"未知的 infra 变量: {sorted(bad_infra)}")
            infra_written = _write_infra_env(infra)
            warnings.append(
                "infra 已写入 .env，需手动重启 memos-api 容器："
                "`docker compose restart memos-api qdrant redis`（Qdrant/Redis host/port 在 compose 里硬编码，改地址还需编辑 docker-compose.yml）"
            )

    logger.info("config override applied: fields=%s infra=%s", applied_fields, infra_written)
    return {"ok": True, "applied_fields": applied_fields, "infra_written": infra_written, "warnings": warnings}


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "配置校验失败 — " + "; ".join(parts) if parts else "配置校验失败"


# ── infra .env (compose-sourced; no python-dotenv dep) ─────────
def _write_infra_env(infra: dict[str, str]) -> list[str]:
    env_path = Path(".env")
    if env_path.exists():
        (env_path.parent / ".env.bak").write_bytes(env_path.read_bytes())
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    written: list[str] = []
    for key, value in infra.items():
        needle = key + "="
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(needle) or ln.startswith("export " + needle)), None)
        line = f"{key}={value}"
        if idx is None:
            lines.append(line)
        else:
            lines[idx] = line
        written.append(key)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


# ── connectivity probe (test endpoint) ────────────────────────
async def probe_connectivity(
    values: dict[str, Any], targets: list[str] | None = None
) -> dict[str, str]:
    """Probe LLM / CipherTalk / MemOS / Vision with candidate values (no persistence).

    Candidate values are merged ONTO live settings, so already-configured-but-masked
    secrets (which the wizard can't echo back) are still probed when the wizard leaves
    them blank; typed values override. ``targets`` limits which probes run (per-step
    testing); None = all. Each probe is best-effort with an 8s timeout; returns
    {target: "ok" | "fail: …" | "skipped: …"}. Transient clients — live singleton untouched.
    """
    all_targets = ("ciphertalk", "memos", "llm", "vision")
    want = set(targets) & set(all_targets) if targets else set(all_targets)

    merged: dict[str, Any] = {}
    with contextlib.suppress(Exception):  # safe: probe without live baseline
        merged.update(get_settings().model_dump())
    for k, v in (values or {}).items():
        if v not in (None, ""):
            merged[k] = v

    results: dict[str, str] = dict.fromkeys(all_targets, "skipped")
    jobs: list[tuple[str, Any]] = []
    if "ciphertalk" in want:
        jobs.append(("ciphertalk", _probe_ciphertalk(merged)))
    if "memos" in want:
        jobs.append(("memos", _probe_memos(merged)))
    if "llm" in want:
        jobs.append(("llm", _probe_llm(merged)))
    if "vision" in want:
        jobs.append(("vision", _probe_vision(merged)))
    if jobs:
        outs = await asyncio.gather(*[c for _, c in jobs], return_exceptions=True)
        for (name, _), out in zip(jobs, outs, strict=False):
            results[name] = out if isinstance(out, str) else f"fail: {_short(out)}"
    return results


async def _probe_ciphertalk(values: dict[str, Any]) -> str:
    """Probe the active data source connectivity.

    按 values["data_source"] 选客户端: weflow → WeFlowClient (GET /api/v1/health),
    ciphertalk → CipherTalkClient (GET /v1/health). target 名保留 'ciphertalk'
    (前端契约), 内部按 data_source 选探哪个端点.
    """
    source = (values.get("data_source") or "ciphertalk").lower().strip()
    if source == "weflow":
        url = values.get("weflow_base_url")
        token = values.get("weflow_token")
        if not url:
            return "skipped: 未提供 weflow_base_url"
        from z_winnow.pipeline.weflow_client import WeFlowClient

        client = WeFlowClient(base_url=url, token=token or "")
    else:
        url = values.get("ciphertalk_base_url")
        token = values.get("ciphertalk_token")
        if not url:
            return "skipped: 未提供 ciphertalk_base_url"
        from z_winnow.pipeline.cipher_talk_client import CipherTalkClient

        client = CipherTalkClient(base_url=url, token=token or "")
    try:
        await asyncio.wait_for(client.health_check(), timeout=8.0)
        return "ok"
    except TimeoutError:
        return "fail: 超时（8s）"
    except Exception as exc:
        return f"fail: {_short(exc)}"
    finally:
        await client.close()


async def _probe_memos(values: dict[str, Any]) -> str:
    url = values.get("memos_api_url")
    if not url:
        return "skipped: 未提供 memos_api_url"
    from z_winnow.memory.adapter import MemOSAdapter

    adapter = MemOSAdapter(base_url=url)  # direct real adapter (bypass mock dispatch)
    try:
        await asyncio.wait_for(adapter.health_check(), timeout=8.0)
        return "ok"
    except TimeoutError:
        return "fail: 超时（8s）"
    except Exception as exc:
        return f"fail: {_short(exc)}"
    finally:
        await adapter.close()


async def _probe_llm(values: dict[str, Any]) -> str:
    """Best-effort LLM ping for OpenAI-compatible endpoints.

    Anthropic-native base URLs aren't OpenAI-compatible → skipped. Uses an
    explicit api_key/base_url/model so the singleton settings are untouched.
    """
    base_url = (
        values.get("openai_base_url")
        or values.get("anthropic_base_url")
        or values.get("deepseek_base_url")
        or ""
    )
    api_key = values.get("openai_api_key") or values.get("deepseek_api_key") or values.get("anthropic_api_key") or ""
    model = (
        values.get("orchestrator_model")
        or values.get("openai_model")
        or values.get("deepseek_model")
        or values.get("anthropic_model")
        or ""
    )
    if base_url and "anthropic.com" in base_url:
        return "skipped: Anthropic 原生端点非 OpenAI 兼容（需走兼容代理才能探测）"
    if not base_url and api_key:
        base_url = "https://api.openai.com/v1"  # official OpenAI default
    if not (base_url and api_key and model):
        return "skipped: 缺少 base_url / api_key / model"
    try:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model, max_tokens=16, timeout=8)
        await asyncio.wait_for(llm.ainvoke("ping"), timeout=8.0)
        return "ok"
    except TimeoutError:
        return "fail: 超时（8s）"
    except Exception as exc:
        msg = _short(exc)
        # never echo a raw 401 body that might contain a key fragment
        if "401" in msg or "Unauthorized" in msg:
            return "fail: 鉴权失败（API key 无效?）"
        # null/truncated choices => endpoint reachable + authed, just cut short by max_tokens
        if "null" in msg and "choices" in msg:
            return "ok"
        return f"fail: {msg}"


async def _probe_vision(values: dict[str, Any]) -> str:
    """Best-effort Vision ping (OpenAI-compatible, e.g. qwen3-vl-flash on DashScope)."""
    base_url = values.get("vision_base_url") or ""
    api_key = values.get("vision_api_key") or ""
    model = values.get("vision_model") or ""
    if not (base_url and api_key and model):
        return "skipped: 缺少 vision_base_url / vision_api_key / vision_model"
    try:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model, max_tokens=16, timeout=8)
        await asyncio.wait_for(llm.ainvoke("ping"), timeout=8.0)
        return "ok"
    except TimeoutError:
        return "fail: 超时（8s）"
    except Exception as exc:
        msg = _short(exc)
        if "401" in msg or "Unauthorized" in msg:
            return "fail: 鉴权失败（vision api_key 无效?）"
        if "null" in msg and "choices" in msg:
            return "ok"
        return f"fail: {msg}"


def _short(exc: BaseException) -> str:
    msg = str(exc).replace("\n", " ").strip()
    return msg[:120]


def trigger_infra_restart() -> str | None:
    """Spawn ``deployments/restart-deps.sh`` detached so it survives the app's execv.

    Called by PUT /system/config when infra vars were written — recreates the
    Qdrant / Redis / memos-api containers so the new project ``.env`` (e.g.
    ``REDIS_PASSWORD``) takes full effect (``docker compose restart`` alone would
    NOT re-read env interpolation). Runs in a new session (detached); logs to
    ``data/restart-deps.log``. Returns the script path on success, None otherwise.
    """
    import subprocess

    project_root = Path(__file__).resolve().parents[4]
    script = project_root / "deployments" / "restart-deps.sh"
    if not script.exists():
        logger.warning("infra restart script not found: %s", script)
        return None
    try:
        subprocess.Popen(
            ["bash", str(script)],
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach: survives parent os.execv
            env={**os.environ},
        )
        logger.info("infra restart script spawned (detached): %s", script)
        return str(script)
    except Exception as exc:  # must not break the save flow
        logger.warning("failed to spawn infra restart script: %s", exc)
        return None


__all__ = ["apply_config_update", "probe_connectivity", "trigger_infra_restart"]
