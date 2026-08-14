"""T-D1: pydantic-settings configuration center.

Typed configuration for all API keys, model configs, database, CipherTalk,
LangSmith, and operational limits. Sensitive values are masked in __repr__.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _mask(value: str | None) -> str:
    """Mask a sensitive string value, showing only first/last few chars."""
    if value is None:
        return "None"
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


class Settings(BaseSettings):
    """Typed configuration center for z-winnow.

    All values are loaded from environment variables / .env file.
    Sensitive fields (API keys, tokens) are masked in __repr__.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
        populate_by_name=True,  # Allow construction by field name alongside aliases
    )

    # ============ API Keys ============
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        description="Anthropic API key (sk-ant-...)",
    )
    deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        description="DeepSeek API key (sk-...)",
    )
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_OPENAI_API_KEY", "OPENAI_API_KEY"),
        description="OpenAI API key (compatible with DeepSeek)",
    )
    langsmith_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_LANGSMITH_API_KEY", "LANGSMITH_API_KEY"),
        description="LangSmith API key (lsv2_...)",
    )
    # Custom API base URLs for third-party proxies
    anthropic_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL"),
        description="Custom Anthropic-compatible API endpoint (third-party proxy)",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias=AliasChoices("WINNOW_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"),
        description="DeepSeek API endpoint",
    )
    openai_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
        description="Generic OpenAI-compatible endpoint (OpenAI/Gemini/OpenRouter/SiliconFlow)",
    )
    openai_model: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_OPENAI_MODEL", "OPENAI_MODEL"),
        description="Default model for the generic openai-compatible path (empty = fall back to orchestrator_model)",
    )

    # ============ CipherTalk ============
    ciphertalk_base_url: str = Field(
        default="http://127.0.0.1:5031",
        validation_alias=AliasChoices("WINNOW_CIPHERTALK_BASE_URL", "CIPHERTALK_BASE_URL"),
        description="CipherTalk API base URL",
    )
    ciphertalk_token: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_CIPHERTALK_TOKEN", "CIPHERTALK_TOKEN"),
        description="CipherTalk API authentication token",
    )
    # ============ WeFlow (legacy /api/v1/ data source on port 5031) ============
    weflow_base_url: str = Field(
        default="http://127.0.0.1:5031",
        validation_alias=AliasChoices("WINNOW_WEFLOW_BASE_URL", "WEFLOW_BASE_URL"),
        description="WeFlow API base URL (legacy /api/v1/ data source)",
    )
    weflow_token: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_WEFLOW_TOKEN", "WEFLOW_TOKEN"),
        description="WeFlow API access token (Bearer header); empty = no auth header",
    )
    data_source: str = Field(
        default="ciphertalk",
        validation_alias=AliasChoices("WINNOW_DATA_SOURCE", "DATA_SOURCE"),
        description=(
            "Data source for chat messages: 'ciphertalk' (default, /v1/ API) "
            "or 'weflow' (legacy /api/v1/ API)."
        ),
    )
    mem0_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_MEM0_API_KEY", "MEM0_API_KEY"),
        description="mem0 API key (reserved for future)",
    )

    # ============ Model Configuration ============
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        validation_alias=AliasChoices("WINNOW_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
        description="Default Anthropic model ID",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("WINNOW_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
        description="Default DeepSeek model ID (deepseek-v4-flash; legacy deepseek-chat/reasoner 已废弃)",
    )
    orchestrator_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("WINNOW_ORCHESTRATOR_MODEL", "ORCHESTRATOR_MODEL"),
        description="Orchestrator model ID (默认 DeepSeek; override via WINNOW_ORCHESTRATOR_MODEL)",
    )
    orchestrator_provider: str = Field(
        default="deepseek",
        validation_alias=AliasChoices("WINNOW_ORCHESTRATOR_PROVIDER", "ORCHESTRATOR_PROVIDER"),
        description="Which provider path the orchestrator routes to: deepseek|openai|anthropic (default deepseek)",
    )

    # Per-subagent model overrides (empty = inherit orchestrator)
    unified_reporter_model: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_UNIFIED_REPORTER_MODEL", "UNIFIED_REPORTER_MODEL"),
        description="unified-reporter subagent model (replaces daily+resource+engineering)",
    )
    output_composer_model: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_OUTPUT_COMPOSER_MODEL", "OUTPUT_COMPOSER_MODEL"),
        description="output-composer subagent model",
    )
    topic_tracker_model: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_TOPIC_TRACKER_MODEL", "TOPIC_TRACKER_MODEL"),
        description="topic-tracker subagent model",
    )

    # ============ Topic Classifier ============
    # T-W10-A-c: Semantic matching thresholds for topic lifecycle classification.
    # A013: These are Field definitions (class-level metadata), not module-level
    # constants. Actual values are read at runtime via get_settings() ensuring
    # monkeypatch and env var overrides take effect.

    topic_classifier_core_semantic_threshold: float = Field(
        default=0.85,
        validation_alias=AliasChoices(
            "WINNOW_TOPIC_CLASSIFIER_CORE_SEMANTIC_THRESHOLD",
            "TOPIC_CLASSIFIER_CORE_SEMANTIC_THRESHOLD",
        ),
        description=(
            "Cosine similarity threshold for core topic semantic matching. "
            "A topic is classified as 'core' if cosine(topic_emb, core_topic_emb) >= this value. "
            "Default 0.85. Lower values → more topics match as core."
        ),
    )
    topic_classifier_continuous_threshold: float = Field(
        default=0.75,
        validation_alias=AliasChoices(
            "WINNOW_TOPIC_CLASSIFIER_CONTINUOUS_THRESHOLD",
            "TOPIC_CLASSIFIER_CONTINUOUS_THRESHOLD",
        ),
        description=(
            "Cosine similarity threshold for continuous topic matching (MemOS recall). "
            "Reserved for T-W10-A-d (MemOS historical recall step). Default 0.75."
        ),
    )
    topic_classifier_continuous_window_days: int = Field(
        default=14,
        validation_alias=AliasChoices(
            "WINNOW_TOPIC_CLASSIFIER_CONTINUOUS_WINDOW_DAYS",
            "TOPIC_CLASSIFIER_CONTINUOUS_WINDOW_DAYS",
        ),
        description=(
            "Lookback window in days for continuous topic MemOS recall. "
            "Reserved for T-W10-A-d. Default 14 days."
        ),
    )

    # ============ Database ============
    database_url: str = Field(
        default="sqlite:///data/winnow.db",
        validation_alias=AliasChoices("WINNOW_DATABASE_URL", "DATABASE_URL"),
        description="SQLite database URL",
    )

    # ============ LangSmith ============
    langsmith_project: str = Field(
        default="z-winnow",
        validation_alias=AliasChoices("WINNOW_LANGSMITH_PROJECT", "LANGSMITH_PROJECT"),
        description="LangSmith project name",
    )
    langsmith_tracing_v2: bool = Field(
        default=True,
        validation_alias=AliasChoices("WINNOW_LANGSMITH_TRACING_V2", "LANGSMITH_TRACING_V2"),
        description="Enable LangSmith v2 tracing",
    )

    # ============ Limits ============
    max_context_tokens: int = Field(
        default=128000,
        validation_alias=AliasChoices("WINNOW_MAX_CONTEXT_TOKENS", "MAX_CONTEXT_TOKENS"),
        description="Maximum context tokens for subagents",
    )
    max_parallel_runs: int = Field(
        default=3,
        validation_alias=AliasChoices("WINNOW_MAX_PARALLEL_RUNS", "MAX_PARALLEL_RUNS"),
        description="Maximum number of pipeline runs allowed concurrently",
    )
    max_parallel_groups: int = Field(
        default=3,
        validation_alias=AliasChoices("WINNOW_MAX_PARALLEL_GROUPS", "MAX_PARALLEL_GROUPS"),
        description="Maximum number of groups running concurrently in batch generation (分群并行上限)",
    )

    # ================================================================
    # Timeout Configuration (P008: 超时 = 历史 P95 耗时 × 1.5)
    # ================================================================
    #
    # 两类超时区分 (L020):
    #   - 派发超时 (dispatch timeout): Box0/harness 等待 agent 返回的最长时限.
    #     轻型任务 300s, 复杂任务 (>=3 文件 + >=15 tests) 600-900s.
    #     Box0 daemon 300s 硬超时直接 SIGKILL (A009 风险).
    #   - 执行超时 (execution timeout): agent/node 内部完成操作的时限.
    #     按 P008 模型校准: 历史 P95 耗时 × 1.5.
    #
    # P008 推算依据 (基于 progress.json timings):
    #   - data_fetch ~10s, content_enrich ~180s, subagents ~30-60s each
    #   - P95 ≈ 200s → × 1.5 = 300s (GRAPH_NODE_TIMEOUT 默认值)
    #
    # A009 风险: 所有节点执行超时 ≤ 300s (Box0 daemon 硬杀阈值).
    # 复杂任务 (content_enrich) 建议 180s 执行超时 < 300s 硬杀阈值.

    graph_node_timeout: int = Field(
        default=300,
        validation_alias=AliasChoices("WINNOW_GRAPH_NODE_TIMEOUT", "GRAPH_NODE_TIMEOUT"),
        description=(
            "默认 graph 节点超时 (秒). P008 推算: 历史 P95 耗时 ~200s × 1.5 = 300s. "
            "覆盖 data_fetch, content_enrich, orchestrator, output_composer, persist 等节点."
        ),
    )
    subagent_timeout_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "WINNOW_SUBAGENT_TIMEOUT_SECONDS", "SUBAGENT_TIMEOUT_SECONDS"
        ),
        description=(
            "子 agent 执行超时 (秒). P008 推算: 历史 subagent P95 ~60-80s × 1.5 ≈ 120s. "
            "此为执行超时 (agent/node 内部完成), 非派发超时 (harness 等待)."
        ),
    )

    # ============ Logging ============
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("WINNOW_LOG_LEVEL", "LOG_LEVEL"),
        description="Log level: DEBUG, INFO, WARNING, ERROR",
    )

    # ============ Storage Paths ============
    # W16-B2: db_path is the single source of truth for the SQLite file path.
    # P083: sqlite_db_path was a pure alias (no semantic inversion), so its 2 legacy
    # env aliases are merged into db_path's AliasChoices (no model_validator layer needed,
    # same pattern as layer3_output_dir). WINNOW_DB_PATH is declared first so it wins
    # when both it and the legacy WINNOW_SQLITE_DB_PATH are set (AliasChoices matches
    # the first env var present, in declaration order).
    # L050/A026: sqlite_db_path is now a read-only @property mirror of db_path
    # (see Computed Properties) — not a second independent source of truth.
    # S7: L1-2 path convergence — hardcoded paths in rl/exporter
    # and pipeline/database now read from Settings instead of module-level constants.
    db_path: str = Field(
        default="data/winnow.db",
        validation_alias=AliasChoices(
            "WINNOW_DB_PATH",
            "DB_PATH",
            "WINNOW_SQLITE_DB_PATH",
            "SQLITE_DB_PATH",
        ),
        description="Canonical SQLite database file path (single source of truth)",
    )
    layer3_output_dir: str = Field(
        default="data/processed",
        validation_alias=AliasChoices(
            "WINNOW_LAYER3_OUTPUT_DIR",
            "LAYER3_OUTPUT_DIR",
            "WINNOW_PROCESSED_DATA_DIR",
            "PROCESSED_DATA_DIR",
        ),
        description="Layer 3 processed JSON output directory",
    )
    deployment_target: Literal["local", "ecs"] = Field(
        default="local",
        validation_alias=AliasChoices("WINNOW_DEPLOYMENT_TARGET", "DEPLOYMENT_TARGET"),
        description=(
            "部署目标：local（单库，本地开发/stdio MCP）或 ecs（双库 — "
            "l3_snapshot.db 只读 + feedback_inbox.db 读写）。MCP server.py 按"
            "此切换 get_l3_db / get_inbox_db 路由（阶段 2.3）。"
            "见 docs/mcp-platform-checkpoint.md §4.3。"
        ),
    )
    l3_snapshot_path: str = Field(
        default="data/l3_snapshot.db",
        validation_alias=AliasChoices("WINNOW_L3_SNAPSHOT_PATH", "L3_SNAPSHOT_PATH"),
        description=(
            "ECS 模式下 L3 只读快照库路径。sync push 用 sqlite .backup 生成"
            "（一致性快照），rsync 到 ECS 此路径。仅 deployment_target=ecs 生效。"
        ),
    )
    feedback_inbox_path: str = Field(
        default="data/feedback_inbox.db",
        validation_alias=AliasChoices("WINNOW_FEEDBACK_INBOX_PATH", "FEEDBACK_INBOX_PATH"),
        description=(
            "ECS 模式下反馈 Inbox 读写库路径。submit_feedback 写入此库；"
            "sync pull 拉回本地主库 merge 后清空。仅 deployment_target=ecs 生效。"
        ),
    )
    # ============ ECS sync（阶段 2.1 push/pull 目标）============
    ecs_ssh_host: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_ECS_SSH_HOST", "ECS_SSH_HOST"),
        description="sync push/pull 目标 ECS 公网地址（如 203.0.113.10）。空 = 未配置。",
    )
    ecs_ssh_user: str = Field(
        default="root",
        validation_alias=AliasChoices("WINNOW_ECS_SSH_USER", "ECS_SSH_USER"),
        description="sync SSH 登录用户（默认 root）。",
    )
    ecs_ssh_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_ECS_SSH_KEY", "ECS_SSH_KEY"),
        description=(
            "sync SSH 私钥本地路径（相对项目根或绝对）。"
            "默认 .claude/skills/winnow-dev/ecs-secrets/id_rsa（已 .gitignore，勿入库）。"
        ),
    )
    ecs_data_dir: str = Field(
        default="/opt/winnow-mcp-data",
        validation_alias=AliasChoices("WINNOW_ECS_DATA_DIR", "ECS_DATA_DIR"),
        description=(
            "ECS 上 MCP 数据卷目录（容器 /app/data 的宿主挂载点）。"
            "l3_snapshot.db / feedback_inbox.db / processed/ 均在此下。"
        ),
    )
    ecs_container_name: str = Field(
        default="winnow-mcp",
        validation_alias=AliasChoices("WINNOW_ECS_CONTAINER_NAME", "ECS_CONTAINER_NAME"),
        description=(
            "ECS 上 MCP 容器名。sync pull 用 docker exec 此容器清 feedback_inbox"
            "（容器内有 python3，避免依赖宿主 sqlite3 CLI）。"
        ),
    )

    # ============ 对象存储 Cloudflare R2（附件公网下载；MCP 远程消费）============
    # pipeline 跑完自动上传 resource.local_path 指向的文件到 R2，记 cloud_url；
    # MCP（ECS）透传 cloud_url 给远程 agent 直接从 R2 下载（ECS 无 serve 路由）。
    # 凭证用 S3 兼容 Access Key ID + Secret（R2 控制台 → Manage R2 API Tokens
    # → 新建『S3 API』token；cfut_ 开头是账户管理 token，不能用于 S3 上传）。
    r2_upload_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINNOW_R2_UPLOAD_ENABLED", "R2_UPLOAD_ENABLED"),
        description="上传附件到 Cloudflare R2（output_composer 后 + CLI 回填）；MCP 公网下载。",
    )
    r2_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_R2_ENDPOINT", "R2_ENDPOINT"),
        description="R2 S3 兼容 endpoint，如 https://<account_id>.r2.cloudflarestorage.com",
    )
    r2_bucket: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_R2_BUCKET", "R2_BUCKET"),
        description="R2 桶名",
    )
    r2_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
        description="R2 S3 兼容 Access Key ID",
    )
    r2_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
        description="R2 S3 兼容 Secret Access Key（__repr__ 脱敏）",
    )
    r2_presigned_expiry: int = Field(
        default=3600,
        validation_alias=AliasChoices("WINNOW_R2_PRESIGNED_EXPIRY", "R2_PRESIGNED_EXPIRY"),
        description=(
            "私有桶预签名 URL 有效期（秒），默认 3600（1h）。MCP serve 时按 cloud_key 生成 cloud_url。"
        ),
    )
    r2_https_proxy: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_R2_HTTPS_PROXY", "R2_HTTPS_PROXY"),
        description=(
            "访问 R2 的 HTTPS 代理（如 http://127.0.0.1:7897）。仅本地直连 R2 网络不通时配"
            "（boto3 直连签名读在国内常卡死，走 Clash 代理 0.9s 通）。ECS 无需配（只预签名不发网络）。"
        ),
    )
    # ============ MCP 鉴权（key → 成员 + 群组白名单）============
    mcp_keys_path: str = Field(
        default="config/mcp_keys.yaml",
        validation_alias=AliasChoices("WINNOW_MCP_KEYS_PATH", "MCP_KEYS_PATH"),
        description=(
            "MCP API key 注册表（key→成员/群组权限）。gitignored；CLI 管理"
            "（winnow mcp-key）；sync push 推 ECS。详见 mcp_server/mcp_keys.py。"
        ),
    )
    rl_output_dir: str = Field(
        default="data/rl",
        validation_alias=AliasChoices("WINNOW_RL_OUTPUT_DIR", "RL_OUTPUT_DIR"),
        description="RL training data output directory",
    )
    memory_dir: str = Field(
        default="memory",
        validation_alias=AliasChoices("WINNOW_MEMORY_DIR", "MEMORY_DIR"),
        description="Directory for versioned memory files",
    )
    reports_dir: str = Field(
        default="reports",
        validation_alias=AliasChoices(
            "WINNOW_REPORTS_DIR",
            "REPORTS_DIR",
            "WINNOW_REPORT_OUTPUT_DIR",
            "REPORT_OUTPUT_DIR",
        ),
        description="Directory for generated reports",
    )

    # ============ Content Enrichment ============
    # T-W12-5: Converged from os.getenv() in content_enrich/__init__.py
    content_enrich_timeout: int = Field(
        default=180,
        validation_alias=AliasChoices("WINNOW_CONTENT_ENRICH_TIMEOUT", "CONTENT_ENRICH_TIMEOUT"),
        description="Content enrichment timeout in seconds (image analysis + link prefetch)",
    )
    enable_enrich: bool = Field(
        default=True,
        validation_alias=AliasChoices("WINNOW_ENABLE_ENRICH", "ENABLE_ENRICH"),
        description="Enable content enrichment (image analysis + link prefetch)",
    )
    media_download_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("WINNOW_MEDIA_DOWNLOAD_ENABLED", "MEDIA_DOWNLOAD_ENABLED"),
        description="Download image/file media to local attachments/ during content_enrich (#9.3)",
    )
    media_max_bytes: int = Field(
        default=50 * 1024 * 1024,
        validation_alias=AliasChoices("WINNOW_MEDIA_MAX_BYTES", "MEDIA_MAX_BYTES"),
        description="Per-file media download size cap in bytes (exceed → skip). Default 50MB",
    )
    media_download_timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("WINNOW_MEDIA_DOWNLOAD_TIMEOUT", "MEDIA_DOWNLOAD_TIMEOUT"),
        description="Per-file media download timeout in seconds",
    )

    wechat_file_storage_dir: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_WECHAT_FILE_STORAGE_DIR", "WECHAT_FILE_STORAGE_DIR"),
        description=(
            "WeChat local file storage root dir (e.g. SMB path to "
            "xwechat_files/wxid_xxx/msg/file).  Subdirectories are expected to be "
            "YYYY-MM/.  Used by patch_resources_local_path to copy matching files "
            "into attachments/ for resource linking."
        ),
    )

    vision_max_tokens: int = Field(
        default=16384,
        validation_alias=AliasChoices("WINNOW_VISION_MAX_TOKENS", "VISION_MAX_TOKENS"),
        description="Vision API max output tokens per image (default 16384)",
    )

    # ============ Web Server ============
    # T-W12-5: Converged from os.getenv() in web/app.py and cli.py
    # T-W14-2: API key for write-operation authentication middleware
    web_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_WEB_API_KEY", "WEB_API_KEY"),
        description="API key for authenticating write operations (POST/PUT/PATCH/DELETE). Empty = auth disabled.",
    )
    web_port: int = Field(
        default=8100,
        validation_alias=AliasChoices("WINNOW_WEB_PORT", "WEB_PORT"),
        description="Web dashboard listening port",
    )
    web_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("WINNOW_WEB_HOST", "WEB_HOST"),
        description="Web dashboard listening host",
    )

    # ============ Daily-report Scheduler (T-SCHED) ============
    # cron-driven daily report automation; runs standalone via `winnow scheduler`.
    scheduler_tz: str = Field(
        default="Asia/Shanghai",
        validation_alias=AliasChoices("WINNOW_SCHEDULER_TZ", "SCHEDULER_TZ"),
        description="Timezone for cron evaluation + 'today' derivation in the scheduler.",
    )
    scheduler_poll_interval_s: int = Field(
        default=60,
        validation_alias=AliasChoices(
            "WINNOW_SCHEDULER_POLL_INTERVAL_S", "SCHEDULER_POLL_INTERVAL_S"
        ),
        description="Daemon poll cadence (seconds); the loop also aligns to minute boundaries.",
    )
    scheduler_backfill_days: int = Field(
        default=7,
        validation_alias=AliasChoices("WINNOW_SCHEDULER_BACKFILL_DAYS", "SCHEDULER_BACKFILL_DAYS"),
        description="Lookback window (days) for startup downtime back-fill.",
    )
    scheduler_lookback_days: int = Field(
        default=7,
        validation_alias=AliasChoices("WINNOW_SCHEDULER_LOOKBACK_DAYS", "SCHEDULER_LOOKBACK_DAYS"),
        description="Lookback window for the status board's 'missing days' column.",
    )
    scheduler_max_parallel: int | None = Field(
        default=None,
        validation_alias=AliasChoices("WINNOW_SCHEDULER_MAX_PARALLEL", "SCHEDULER_MAX_PARALLEL"),
        description="Max concurrent group runs; None = reuse max_parallel_groups.",
    )
    scheduler_embedded_in_web: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "WINNOW_SCHEDULER_EMBEDDED_IN_WEB", "SCHEDULER_EMBEDDED_IN_WEB"
        ),
        description="If True, start the scheduler inside the web lifespan (default off — run standalone).",
    )
    scheduler_default_report_types: str = Field(
        default="daily",
        validation_alias=AliasChoices(
            "WINNOW_SCHEDULER_DEFAULT_REPORT_TYPES", "SCHEDULER_DEFAULT_REPORT_TYPES"
        ),
        description="Comma-separated report types generated per scheduled run (default 'daily').",
    )

    # ============ Pipeline Defaults ============
    # T-W12-5: Converged from os.getenv() in pipeline/__init__.py
    group_name: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_GROUP_NAME", "GROUP_NAME"),
        description="Default group name for pipeline runs",
    )

    # ============ Feishu (Lark) Output ============
    feishu_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINNOW_FEISHU_ENABLED", "FEISHU_ENABLED"),
        description="Enable Feishu Bitable upload output",
    )
    feishu_env: str = Field(
        default="test",
        validation_alias=AliasChoices("WINNOW_FEISHU_ENV", "FEISHU_ENV"),
        description="Feishu environment label (informational; default: test).",
    )

    # ============ Vision / Image Analysis ============
    # T-W12-5: Converged from os.getenv() in content_enrich/image_analyzer.py
    vision_model: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_VISION_MODEL", "VISION_MODEL"),
        description="Vision model name for image analysis (empty = disabled)",
    )
    vision_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_VISION_BASE_URL", "VISION_BASE_URL"),
        description="Vision API base URL for OpenAI-compatible proxy",
    )
    vision_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_VISION_API_KEY", "VISION_API_KEY"),
        description="Vision API key for OpenAI-compatible proxy",
    )
    mcp_image_analysis: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINNOW_MCP_IMAGE_ANALYSIS", "MCP_IMAGE_ANALYSIS"),
        description="Enable MCP image analysis mode",
    )
    image_max_concurrency: int = Field(
        default=20,
        validation_alias=AliasChoices("WINNOW_IMAGE_MAX_CONCURRENCY", "IMAGE_MAX_CONCURRENCY"),
        description="Max concurrent image analysis requests",
    )
    image_max_file_size_mb: int = Field(
        default=20,
        validation_alias=AliasChoices("WINNOW_IMAGE_MAX_FILE_SIZE_MB", "IMAGE_MAX_FILE_SIZE_MB"),
        description="Max image file size in MB",
    )
    supported_image_formats: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_SUPPORTED_IMAGE_FORMATS", "SUPPORTED_IMAGE_FORMATS"),
        description=(
            "Comma-separated list of supported image formats (empty = default: "
            "png, jpg, jpeg, gif, webp)"
        ),
    )
    mcp_image_endpoint: str = Field(
        default="http://127.0.0.1:8080/analyze_image",
        validation_alias=AliasChoices("WINNOW_MCP_IMAGE_ENDPOINT", "MCP_IMAGE_ENDPOINT"),
        description="MCP image analysis endpoint URL",
    )

    # ============ Quick Img (DMX Gemini) ============
    quick_img_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WINNOW_QUICK_IMG_API_KEY", "QUICK_IMG_API_KEY", "DMX_API_KEY"
        ),
        description="DMX Gemini API key for daily report image generation",
    )
    quick_img_base_url: str = Field(
        default="https://www.dmxapi.cn",
        validation_alias=AliasChoices(
            "WINNOW_QUICK_IMG_BASE_URL", "QUICK_IMG_BASE_URL", "DMX_BASE_URL"
        ),
        description="DMX API base URL (Gemini 原生代理，无 /v1 后缀)",
    )
    quick_img_model: str = Field(
        default="gemini-3.1-flash-image",
        validation_alias=AliasChoices("WINNOW_QUICK_IMG_MODEL", "QUICK_IMG_MODEL", "DMX_MODEL_ID"),
        description="DMX image generation model ID (Gemini native generateContent)",
    )

    # ============ Image Generation (#9.2 日报配图) ============
    image_gen_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINNOW_IMAGE_GEN_ENABLED", "IMAGE_GEN_ENABLED"),
        description="飞书上传时是否自动挂载日报配图（默认关；配图由 gen-image CLI 独立生成）",
    )
    image_gen_ratio: str = Field(
        default="4:5",
        validation_alias=AliasChoices("WINNOW_IMAGE_GEN_RATIO", "IMAGE_GEN_RATIO"),
        description="配图宽高比 (4:5/1:1/16:9/9:16 等)",
    )
    image_gen_size: str = Field(
        default="2K",
        validation_alias=AliasChoices("WINNOW_IMAGE_GEN_SIZE", "IMAGE_GEN_SIZE"),
        description="配图分辨率 (0.5K/1K/2K/4K)；2K 适合含文字信息图",
    )
    image_gen_count: int = Field(
        default=1,
        validation_alias=AliasChoices("WINNOW_IMAGE_GEN_COUNT", "IMAGE_GEN_COUNT"),
        description="单次生成张数 (同 prompt 抽卡，>1 时命名 cover_01.png...)",
    )
    image_gen_timeout: int = Field(
        default=300,
        validation_alias=AliasChoices("WINNOW_IMAGE_GEN_TIMEOUT", "IMAGE_GEN_TIMEOUT"),
        description="DMX 生图请求超时秒数 (生图慢，默认 300)",
    )

    # ============ mem0 (reserved) ============
    mem0_org_id: str = Field(
        default="",
        validation_alias=AliasChoices("WINNOW_MEM0_ORG_ID", "MEM0_ORG_ID"),
        description="mem0 organization ID (reserved)",
    )

    # ============ MemOS Memory System ============
    # S3: memos_enabled defaults to True — MemOS is a required service in production.
    # Test environments use WINNOW_ENV=test for MockMemOSAdapter.
    # P016: No MemOS SDK imports at module level; lazy import at call sites only.
    memos_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("WINNOW_MEMOS_ENABLED", "MEMOS_ENABLED"),
        description=(
            "Enable MemOS memory system. Defaults to True (S3: MemOS is required). "
            "Test environments set WINNOW_ENV=test for MockMemOSAdapter. "
            "When False, all MemOS code paths are no-ops with zero side effects."
        ),
    )
    memos_api_url: str = Field(
        default="http://localhost:8000",
        validation_alias=AliasChoices("WINNOW_MEMOS_API_URL", "MEMOS_API_URL"),
        description="MemOS API server URL (e.g. http://localhost:8000 or http://memos-api:8000)",
    )
    memos_cube_prefix: str = Field(
        default="winnow",
        validation_alias=AliasChoices("WINNOW_MEMOS_CUBE_PREFIX", "MEMOS_CUBE_PREFIX"),
        description="Prefix for MemOS Cube names to distinguish deployment instances",
    )
    memos_search_timeout: int = Field(
        default=60,
        validation_alias=AliasChoices("WINNOW_MEMOS_SEARCH_TIMEOUT", "MEMOS_SEARCH_TIMEOUT"),
        description="Timeout in seconds for each MemOS search_memories call (default 60s)",
    )

    # MemOS model configuration
    mos_chat_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("WINNOW_MOS_CHAT_MODEL", "MOS_CHAT_MODEL"),
        description="Chat model used by MemOS. NOTE: bare model name without "
        "'provider/' prefix — MemOS rejects prefixed names (e.g. openai/gpt-4o-mini).",
    )
    mos_embedder_model: str = Field(
        default="openai/text-embedding-3-large",
        validation_alias=AliasChoices("WINNOW_MOS_EMBEDDER_MODEL", "MOS_EMBEDDER_MODEL"),
        description="Embedding model used by MemOS",
    )
    mos_embedder_backend: str = Field(
        default="openai",
        validation_alias=AliasChoices("WINNOW_MOS_EMBEDDER_BACKEND", "MOS_EMBEDDER_BACKEND"),
        description="Embedding backend provider for MemOS",
    )
    embedding_dimension: int = Field(
        default=3072,
        validation_alias=AliasChoices("WINNOW_EMBEDDING_DIMENSION", "EMBEDDING_DIMENSION"),
        description="Embedding vector dimension (must match model)",
    )
    mos_reranker_backend: str = Field(
        default="openai",
        validation_alias=AliasChoices("WINNOW_MOS_RERANKER_BACKEND", "MOS_RERANKER_BACKEND"),
        description="Reranker backend provider for MemOS",
    )

    # ============ Environment / Mock Control (T-W12-8) ============
    # S7: Unified mock mode switch matrix.
    #
    # Design:
    #   - environment: "production" (default) or "test" (all mocked)
    #   - Per-service override booleans (take precedence over environment)
    #   - Computed properties use_mock_* combine both
    #   - Old env vars emit DeprecationWarning via model_validator
    #
    # P080: DeprecationWarning mapping uses AliasChoices + model_validator.
    # A013: No module-level constants — all reads via get_settings() at call time.

    environment: str = Field(
        default="production",
        validation_alias=AliasChoices("WINNOW_ENV", "ENVIRONMENT"),
        description=(
            "Global environment mode: 'production' (real services) or 'test' (mock all). "
            "Per-service overrides: WINNOW_MOCK_LLM, WINNOW_MOCK_MEMOS."
        ),
    )
    mock_llm: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINNOW_MOCK_LLM", "MOCK_LLM"),
        description="Force LLM mock mode (overrides environment for LLM calls).",
    )
    mock_memos: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINNOW_MOCK_MEMOS", "MOCK_MEMOS"),
        description="Force MemOS mock mode (overrides environment for MemOS adapter).",
    )

    # ============ Validators ============

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v_upper = v.upper().strip()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v_upper not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}, got '{v}'")
        return v_upper

    @field_validator("environment", mode="before")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        """T-W12-8: Validate environment value."""
        v_lower = v.lower().strip()
        valid = {"production", "test"}
        if v_lower not in valid:
            raise ValueError(f"environment must be one of {valid}, got '{v}'")
        return v_lower

    @field_validator("orchestrator_provider", mode="before")
    @classmethod
    def _validate_orchestrator_provider(cls, v: str) -> str:
        """Clamp to a supported provider; bad value falls back to deepseek (safe-boot,
        never bricks) rather than raising — the wizard override may write a typo."""
        valid = {"anthropic", "deepseek", "openai"}
        v_lower = (v or "").lower().strip()
        if v_lower not in valid:
            logging.getLogger(__name__).warning(
                "orchestrator_provider must be one of %s, got '%s' — falling back to 'deepseek'",
                sorted(valid),
                v,
            )
            return "deepseek"
        return v_lower

    @field_validator("data_source", mode="before")
    @classmethod
    def _validate_data_source(cls, v: str) -> str:
        """Clamp to a supported data source; bad value falls back to 'ciphertalk'
        (safe-boot, never bricks) rather than raising — the wizard override may
        write a typo."""
        valid = {"ciphertalk", "weflow"}
        v_lower = (v or "").lower().strip()
        if v_lower not in valid:
            logging.getLogger(__name__).warning(
                "data_source must be one of %s, got '%s' — falling back to 'ciphertalk'",
                sorted(valid),
                v,
            )
            return "ciphertalk"
        return v_lower

    @model_validator(mode="before")
    @classmethod
    def _handle_deprecated_mock_vars(cls, data: object) -> object:
        """T-W12-8: Handle deprecated mock env vars with DeprecationWarning.

        Maps old env vars to new fields:
          - WINNOW_REAL_LLM=false → mock_llm=True (inverted)

        New-style env vars take precedence over deprecated ones.
        """
        import os
        import warnings

        if not isinstance(data, dict):
            return data

        # WINNOW_REAL_LLM=false → mock_llm=True (inverted logic)
        real_llm = os.getenv("WINNOW_REAL_LLM", "").strip().lower()
        if real_llm in ("false", "0", "no", "off"):
            warnings.warn(
                "WINNOW_REAL_LLM is deprecated. Use WINNOW_MOCK_LLM=true.",
                DeprecationWarning,
                stacklevel=2,
            )
            if "mock_llm" not in data:
                data["mock_llm"] = True

        return data

    # _validate_required_keys removed.
    # Validation is deferred to create_model() at call time,
    # allowing mock-mode and third-party proxy usage without Anthropic key.

    # ============ Computed Properties ============

    # W16-B2: read-only mirror of db_path. sqlite_db_path was previously an
    # independent Field with its own env aliases; it is now a derived alias so
    # db_path is the single source of truth (L050/A026). Read-side consumers that
    # still read settings.sqlite_db_path get the same value transparently.
    # Deliberately has NO setter: writes must go through db_path (or its env var)
    # to preserve the single-source-of-truth invariant.
    @property
    def sqlite_db_path(self) -> str:
        """Read-only mirror of db_path (legacy attribute name)."""
        return self.db_path

    @property
    def effective_unified_reporter_model(self) -> str:
        """Resolve subagent model: specific override or orchestrator default."""
        return self.unified_reporter_model or self.orchestrator_model

    @property
    def effective_output_composer_model(self) -> str:
        return self.output_composer_model or self.orchestrator_model

    @property
    def effective_topic_tracker_model(self) -> str:
        return self.topic_tracker_model or self.orchestrator_model

    @property
    def anthropic_api_key_available(self) -> bool:
        """Check if the Anthropic API key is configured."""
        return bool(self.anthropic_api_key)

    @property
    def deepseek_api_key_available(self) -> bool:
        """Check if the DeepSeek API key is configured."""
        return bool(self.deepseek_api_key)

    # ============ Mock Control Computed Properties (T-W12-8) ============

    @property
    def use_mock_llm(self) -> bool:
        """Effective mock mode for LLM. True = skip real LLM calls.

        True when either mock_llm is set OR environment == "test".
        """
        return self.mock_llm or self.environment == "test"

    @property
    def use_mock_memos(self) -> bool:
        """Effective mock mode for MemOS. True = use MockMemOSAdapter.

        True when either mock_memos is set OR environment == "test".
        """
        return self.mock_memos or self.environment == "test"

    # ============ Data Source Resolution ============

    @property
    def effective_data_base_url(self) -> str:
        """Resolve the active data source base URL per data_source setting.

        Single source of truth for 'which base_url to call' — callers (builder,
        runs, pipeline.run, cli ingest, config probe) read this instead of
        hardcoding ciphertalk_base_url.
        """
        if self.data_source == "weflow":
            return self.weflow_base_url or "http://127.0.0.1:5031"
        return self.ciphertalk_base_url or "http://127.0.0.1:5031"

    @property
    def effective_data_token(self) -> str:
        """Resolve the active data source token per data_source setting."""
        if self.data_source == "weflow":
            return self.weflow_token
        return self.ciphertalk_token

    # ============ Display ============

    def __repr__(self) -> str:
        """Mask sensitive values in repr output."""
        fields = []
        sensitive = {
            "anthropic_api_key",
            "deepseek_api_key",
            "openai_api_key",
            "langsmith_api_key",
            "mem0_api_key",
            "vision_api_key",
            "ciphertalk_token",
            "weflow_token",
            "r2_secret_access_key",
        }
        for name, value in self.model_dump().items():
            if name in sensitive:
                fields.append(f"{name}='{_mask(value)}'")
            else:
                fields.append(f"{name}={value!r}")
        return f"Settings({', '.join(fields)})"

    def __str__(self) -> str:
        return self.__repr__()


# ============ Runtime Override Source (init wizard「保存并重启」) ============
# data/config_overrides.json is written by the web onboarding wizard's
# 「保存并重启」action. It is the HIGHEST-effective-priority source (after
# explicit constructor kwargs), beating env vars / .env so changes take effect
# even in containerized deployments that set the same vars in docker-compose.
# safe-boot: a missing / corrupt / unparseable file is treated as an empty
# override and NEVER blocks Settings() construction (prevents crash-loops).
_OVERRIDE_PATH = Path("data/config_overrides.json")


def _load_overrides() -> dict[str, Any]:
    """Read data/config_overrides.json → dict of {field_name: value}.

    Written by the web onboarding wizard's「保存并重启」. Applied by get_settings()
    as **constructor kwargs** (highest priority in pydantic-settings), so it beats
    env vars / .env — changes take effect even in containerized deployments that
    set the same vars in docker-compose (e.g. MOS_CHAT_MODEL).

    safe-boot: missing / corrupt / unparseable file → empty dict (never blocks
    Settings() construction). Only real Settings field names are returned.
    """
    try:
        if not _OVERRIDE_PATH.exists():
            return {}
        raw = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        valid = set(Settings.model_fields)
        return {str(k): v for k, v in raw.items() if str(k) in valid}
    except Exception as exc:  # safe-boot: corrupt file must not brick boot
        logging.getLogger(__name__).warning(
            "config overrides unreadable at %s (%s); ignoring overrides",
            _OVERRIDE_PATH,
            exc,
        )
        return {}


# ============ Global Singleton ============

_settings_lock = threading.Lock()
_settings_instance: Settings | None = None


def get_settings() -> Settings:
    """Get the global Settings singleton, loading from .env.

    Thread-safe lazy initialization. The singleton can be overridden
    in tests by calling get_settings(force_reload=True) or by setting
    environment variables before the first call.

    Returns:
        Settings: The singleton configuration instance.

    Raises:
        ValidationError: If required config values (e.g. ANTHROPIC_API_KEY) are missing.
    """
    global _settings_instance
    if _settings_instance is None:
        with _settings_lock:
            if _settings_instance is None:  # Double-checked locking
                # Inject runtime overrides (data/config_overrides.json) as kwargs —
                # highest priority, beats env vars / .env (init_settings wins).
                _settings_instance = Settings(**_load_overrides())  # type: ignore[call-arg]
    return _settings_instance


def reset_settings() -> None:
    """Reset the global settings singleton (for testing)."""
    global _settings_instance
    with _settings_lock:
        _settings_instance = None
