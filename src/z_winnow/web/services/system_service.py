"""System service -- wraps config/settings.py and database.py stats queries.

Responsibility: expose non-sensitive system configuration and aggregated
statistics from existing database functions. No new DB queries -- only
orchestration of existing ones.

B5: get_system_config() must mask sensitive keys (anthropic_api_key,
deepseek_api_key, openai_api_key). get_system_stats() aggregates message
counts, pipeline run stats, and queue stats.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from z_winnow.config.settings import get_settings
from z_winnow.pipeline.database import (
    get_message_stats,
    get_sync_queue_stats,
)

logger = logging.getLogger(__name__)


async def check_lark_cli() -> dict[str, Any]:
    """Probe lark-cli readiness (#8). Never throws (P014) — every failure becomes
    a field on the returned dict so the UI can show actionable guidance.

    Readiness = binary on PATH + user identity available + base/drive scopes
    (the uploader uses ``--as user`` with base_create/table_create + attachments).
    """
    import asyncio
    import json
    import shutil

    from z_winnow.pipeline.feishu.lark_cli import lark_bin

    bin_name = lark_bin()
    resolved = shutil.which(bin_name) or ""
    out: dict[str, Any] = {
        "installed": bool(resolved),
        "path": resolved,
        "version": "",
        "authed": False,
        "user_name": "",
        "user_status": "",
        "base_drive_ok": False,
        "note": "",
    }
    if not resolved:
        out["note"] = (
            f"未找到 {bin_name}。需从源码构建 lark-cli（github.com/larksuite/cli）"
            f"并加入 PATH，或设环境变量 LARK_CLI_BIN 指向它。"
        )
        return out

    # version: `lark-cli --version` → plain text
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_name, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        o, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        out["version"] = (o or b"").decode("utf-8", "replace").strip()
    except Exception:
        pass

    # auth status: `lark-cli auth status` → JSON envelope
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_name, "auth", "status",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        o, _e = await asyncio.wait_for(proc.communicate(), timeout=15)
        data: Any = {}
        try:
            data = json.loads((o or b"").decode("utf-8", "replace")) or {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        user = (data.get("identities") or {}).get("user") or {}
        out["authed"] = bool(user.get("available"))
        out["user_name"] = str(user.get("userName") or "")
        out["user_status"] = str(user.get("status") or "")
        scope = str(user.get("scope") or "")
        out["base_drive_ok"] = "base:app:create" in scope and "drive:file:upload" in scope
        if not out["authed"]:
            out["note"] = "未授权用户身份。运行：lark-cli auth login --domain base,drive"
        elif not out["base_drive_ok"]:
            out["note"] = "已授权但缺 base/drive 权限。重新运行：lark-cli auth login --domain base,drive"
    except Exception as exc:  # timeout / unexpected — never propagate
        out["note"] = f"auth status 探测失败：{exc}"

    return out


# Fields to mask from config output
_SENSITIVE_FIELDS = frozenset(
    {
        "anthropic_api_key",
        "deepseek_api_key",
        "openai_api_key",
        "langsmith_api_key",
        "mem0_api_key",
        "ciphertalk_token",
        "web_api_key",
        "vision_api_key",
        "quick_img_api_key",
    }
)

# Non-sensitive fields to include in config output.
# settings.html 系统配置页需要展示的端点 / 模型 / 限额等非敏感字段；
# 密钥与 token 仍由 _SENSITIVE_FIELDS 排除（掩码或不返回）。
_CONFIG_FIELDS = frozenset(
    {
        "db_path",
        "mock_mode",
        "environment",
        "use_mock_llm",
        "use_mock_memos",
        "data_source",
        "log_level",
        "web_port",
        "web_host",
        "feishu_enabled",
        "feishu_env",
        "memos_enabled",
        "enable_enrich",
        # 模型
        "anthropic_model",
        "deepseek_model",
        "orchestrator_model",
        "orchestrator_provider",
        "openai_base_url",
        "openai_model",
        "unified_reporter_model",
        "output_composer_model",
        "topic_tracker_model",
        "vision_model",
        "quick_img_model",
        # MemOS（app 端；Qdrant/Redis 是容器基建，不在 Settings）
        "memos_api_url",
        "memos_cube_prefix",
        "memos_search_timeout",
        "mos_chat_model",
        "mos_embedder_model",
        # 端点 base_url（非密钥）
        "anthropic_base_url",
        "deepseek_base_url",
        "ciphertalk_base_url",
        "vision_base_url",
        "quick_img_base_url",
        # 运行限额 / 超时
        "max_context_tokens",
        "max_parallel_runs",
        "graph_node_timeout",
        "subagent_timeout_seconds",
        "content_enrich_timeout",
        "image_max_concurrency",
        # 存储路径
        "layer3_output_dir",
        "rl_output_dir",
        "reports_dir",
        "memory_dir",
    }
)


async def get_system_config() -> dict[str, Any]:
    """Return non-sensitive fields from Settings.

    B5: Must contain db_path and mock_mode but NOT contain
    anthropic_api_key, deepseek_api_key, or openai_api_key.
    """
    settings = get_settings()
    all_fields = settings.model_dump()

    result: dict[str, Any] = {}
    for key in _CONFIG_FIELDS:
        if key in all_fields:
            result[key] = all_fields[key]

    # Computed properties
    result["mock_mode"] = settings.use_mock_llm
    result["use_mock_llm"] = settings.use_mock_llm
    result["use_mock_memos"] = settings.use_mock_memos

    # Explicitly ensure sensitive keys are absent
    for sensitive in _SENSITIVE_FIELDS:
        result.pop(sensitive, None)

    return result


async def get_system_stats(db_path: str | None = None) -> dict[str, Any]:
    """Return aggregated system statistics from database.py functions.

    B5: Returns dict with keys message_count, pipeline_runs, queue_stats.
    No new DB queries -- only orchestration of existing ones.

    Args:
        db_path: SQLite database path. Defaults to Settings.db_path.

    Returns:
        Dict with message_count, pipeline_runs, queue_stats.
    """
    resolved_db = db_path or get_settings().db_path

    message_stats: dict[str, int] = {"total": 0, "sanitized": 0}
    pipeline_runs: dict[str, int] = {"total": 0}
    queue_stats: dict[str, int] = {
        "pending": 0,
        "processing": 0,
        "done": 0,
        "failed": 0,
        "total": 0,
    }

    try:
        async with aiosqlite.connect(resolved_db) as conn:
            # Message stats from existing function
            try:
                message_stats = await get_message_stats(conn)
            except Exception as e:
                logger.warning("Failed to get message stats: %s", e)

            # Pipeline runs count -- direct query (no existing function)
            try:
                cursor = await conn.execute("SELECT COUNT(*) FROM pipeline_runs")
                row = await cursor.fetchone()
                pipeline_runs["total"] = row[0] if row else 0
            except Exception as e:
                logger.warning("Failed to get pipeline run count: %s", e)

            # Sync queue stats from existing function
            try:
                queue_stats = await get_sync_queue_stats(conn)
            except Exception as e:
                logger.warning("Failed to get sync queue stats: %s", e)
    except Exception as e:
        logger.warning("Failed to connect to database for system stats: %s", e)

    return {
        "message_count": message_stats.get("total", 0),
        "pipeline_runs": pipeline_runs,
        "queue_stats": queue_stats,
    }


__all__ = [
    "get_system_config",
    "get_system_stats",
]
