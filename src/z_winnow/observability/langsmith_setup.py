"""T-K1: LangSmith SDK initialization and global trace context management.

Provides:
- LangSmithSetup: Pydantic model for LangSmith configuration (strict mode, EXP-004).
- init_langsmith(): Initialize LangSmith tracing from Settings with graceful degradation.
- is_langsmith_enabled(): Quick check if LangSmith tracing is active.
- _mask_key(): Utility to mask API keys in log output (EXP-010).

Graceful degradation: when LANGCHAIN_API_KEY is not configured, tracing is
silently disabled with a single warning log — the application continues to
run without interruption.

Architecture references:
- docs/architecture-detail.md §附录B (LangSmith env vars)
- EXP-004: Pydantic strict(extra='forbid') for schema drift prevention
- EXP-010: API key from Settings, auto-masked in logs
- EXP-011: structlog JSON compatibility via LOG_FORMAT
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ============================================================
# LangSmithSetup — Configuration model (EXP-004: strict mode)
# ============================================================


class LangSmithSetup(BaseModel, extra="forbid"):
    """LangSmith tracing configuration, loaded from Settings.

    Uses Pydantic strict mode (extra='forbid') to prevent schema drift.
    All fields map to LangSmith environment variable conventions.

    Attributes:
        api_key: LangSmith API key (lsv2_...). None if not configured.
        project: LangSmith project name for grouping traces.
        tracing_v2: Whether to use LangSmith v2 tracing API.
        endpoint: LangSmith API endpoint (default: https://api.smith.langchain.com).
        enabled: Computed — True when api_key is configured and tracing_v2 is enabled.
    """

    api_key: str | None = Field(
        default=None,
        description="LangSmith API key (lsv2_...) — None if not configured",
    )
    project: str = Field(
        default="z-winnow",
        description="LangSmith project name for trace grouping",
    )
    tracing_v2: bool = Field(
        default=True,
        description="Enable LangSmith v2 tracing API",
    )
    endpoint: str = Field(
        default="https://api.smith.langchain.com",
        description="LangSmith API endpoint URL",
    )

    @property
    def enabled(self) -> bool:
        """Whether LangSmith tracing is effectively enabled.

        Requires both the API key to be configured AND tracing_v2=True.
        """
        return bool(self.api_key) and self.tracing_v2


# ============================================================
# _mask_key — Helper for safe logging (EXP-010)
# ============================================================


def _mask_key(value: str | None) -> str:
    """Mask a sensitive string value, showing only first/last few chars."""
    if value is None:
        return "None"
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


# ============================================================
# _setup_instance — Module-level singleton
# ============================================================

_setup_instance: LangSmithSetup | None = None


# ============================================================
# Public API
# ============================================================


def init_langsmith(force: bool = False) -> LangSmithSetup:
    """Initialize LangSmith tracing from the project Settings.

    Reads LangSmith configuration from the global Settings singleton
    (config.settings.get_settings()) and sets the standard LangSmith
    environment variables: LANGCHAIN_TRACING_V2, LANGCHAIN_API_KEY,
    LANGCHAIN_PROJECT, LANGCHAIN_ENDPOINT.

    Graceful degradation (EXP-010): if LANGCHAIN_API_KEY is not configured,
    emit a single warning log and return a disabled setup. The application
    continues to run without tracing — no crash, no hard error.

    Idempotent: calling init_langsmith() multiple times is safe. Subsequent
    calls return the cached setup instance (unless force=True).

    Args:
        force: If True, re-initialize from Settings even if already initialized.

    Returns:
        LangSmithSetup with the effective configuration. Check ``.enabled``
        to determine if tracing is active.

    Raises:
        No exceptions — all errors are gracefully handled via warnings.

    Example:
        >>> from z_winnow.observability.langsmith_setup import init_langsmith
        >>> setup = init_langsmith()
        >>> if setup.enabled:
        ...     print(f"LangSmith tracing active in project: {setup.project}")
        ... else:
        ...     print("LangSmith tracing disabled")
    """
    global _setup_instance

    if _setup_instance is not None and not force:
        return _setup_instance

    # Load from Settings (EXP-010: key from Settings, not hardcoded)
    try:
        from z_winnow.config.settings import get_settings

        settings = get_settings()
        api_key = settings.langsmith_api_key or None
        project = settings.langsmith_project or "z-winnow"
        tracing_v2 = settings.langsmith_tracing_v2
    except Exception:
        # If Settings cannot be loaded at all (e.g., missing .env during tests),
        # fall back to environment variables directly.
        logger.debug("Settings unavailable, reading LangSmith config from env directly")
        api_key = os.getenv("LANGSMITH_API_KEY") or None
        project = os.getenv("LANGSMITH_PROJECT", "z-winnow")
        tracing_v2 = os.getenv("LANGSMITH_TRACING_V2", "true").lower() != "false"

    # Graceful degradation: no API key → disabled but not crashing
    if not api_key:
        logger.warning(
            "LangSmith API key not configured — tracing disabled. "
            "Set LANGSMITH_API_KEY (or WINNOW_LANGSMITH_API_KEY) in your .env file "
            "to enable full observability."
        )
        _setup_instance = LangSmithSetup(
            api_key=None,
            project=project,
            tracing_v2=False,
        )
        _unset_langsmith_env()
        return _setup_instance

    # Set LangSmith environment variables (standard SDK convention)
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if tracing_v2 else "false"
    os.environ["LANGCHAIN_API_KEY"] = api_key
    os.environ["LANGCHAIN_PROJECT"] = project
    os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"

    # Prevent system proxy from interfering with LangSmith API calls.
    # Windows registry may have ProxyServer set even when ProxyEnable=0,
    # and requests/httpx can pick it up via trust_env=True (default).
    os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",api.smith.langchain.com").lstrip(
        ","
    )

    # Patch LangSmith SDK background trace send timeout (hardcoded to 10s read).
    # Cross-Pacific latency from China requires more headroom.
    try:
        import langsmith.client as _ls_client

        _ls_client._TRACING_SEND_TIMEOUT = (10, 60)  # (connect_s, read_s) — was (3, 10)
    except (ImportError, AttributeError):
        pass

    # Pre-create cached client with higher timeout and disable proxy auto-detect.
    try:
        import requests as _requests  # type: ignore[import-untyped]
        from langsmith.run_trees import get_cached_client

        _ls_session = _requests.Session()
        _ls_session.trust_env = False
        get_cached_client(
            timeout_ms=(30_000, 120_000),
            session=_ls_session,
        )
    except (ImportError, TypeError, Exception):
        # Fallback: pre-create without custom session (older SDK versions)
        try:
            from langsmith.run_trees import get_cached_client

            get_cached_client(timeout_ms=(30_000, 120_000))
        except (ImportError, Exception):
            pass

    _setup_instance = LangSmithSetup(
        api_key=api_key,
        project=project,
        tracing_v2=tracing_v2,
    )

    logger.info(
        "LangSmith tracing initialized: project=%s, v2=%s, key=%s",
        project,
        tracing_v2,
        _mask_key(api_key),
    )

    return _setup_instance


def is_langsmith_enabled() -> bool:
    """Check if LangSmith tracing is currently active.

    Returns True only if init_langsmith() has been called and the API
    key was successfully configured.

    Returns:
        bool: True if LangSmith tracing is enabled and active.

    Example:
        >>> if is_langsmith_enabled():
        ...     # Attach LangSmith callbacks to graph runs
        ...     pass
    """
    if _setup_instance is None:
        return False
    return _setup_instance.enabled


def get_langsmith_setup() -> LangSmithSetup:
    """Get the current LangSmith setup instance.

    Returns the cached setup. If init_langsmith() has not been called,
    returns a disabled default setup.

    Returns:
        LangSmithSetup: The current configuration (may be disabled).

    Example:
        >>> setup = get_langsmith_setup()
        >>> print(setup.project)
        'z-winnow'
    """
    if _setup_instance is not None:
        return _setup_instance
    return LangSmithSetup(api_key=None, tracing_v2=False)


def get_langsmith_callbacks(
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[Any]:
    """Get LangSmith callback handlers for a graph run.

    LangGraph 1.1+ auto-traces via LANGCHAIN_TRACING_V2 env var.
    Manual callbacks are no longer needed and may cause
    'Client' object has no attribute 'run_inline' errors.

    Returns:
        list: Always empty — tracing is handled automatically by LangGraph.
    """
    # Auto-tracing via env vars set by init_langsmith() is sufficient.
    return []


def _unset_langsmith_env() -> None:
    """Remove LangSmith environment variables to disable tracing.

    Called during graceful degradation to ensure that LangChain's
    auto-tracing doesn't attempt to use an unconfigured key.
    """
    for var in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    ):
        os.environ.pop(var, None)


def reset_langsmith() -> None:
    """Reset the LangSmith setup singleton (for testing).

    Removes the environment variables and clears the cached setup.
    """
    global _setup_instance
    _unset_langsmith_env()
    _setup_instance = None
    logger.debug("LangSmith setup reset")
