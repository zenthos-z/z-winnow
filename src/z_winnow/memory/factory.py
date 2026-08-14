"""T-W10-E-a / T-W10-E-ba / T-W12-6 / T-W12-8: MemOS adapter factory — unified create_memos_adapter().

P002: Factory function pattern — single entry point for adapter creation.
All modules must obtain adapters through this factory, never instantiate directly.

Dispatch logic (A013: reads settings at call time, not module import time):
  1. Settings.use_mock_memos=True  → MockMemOSAdapter (in-memory dict storage)
  2. otherwise                     → MemOSAdapter (real httpx client)

T-W12-8: Mock control now via Settings.use_mock_memos (unified switch matrix).
  Uses Settings.use_mock_memos for dispatch.

T-W12-6: Simplified dispatch per S3 (MemOS is required service):
  - Removed MEMOS_ENABLED=false → DisabledAdapter branch.
    Production always uses RealAdapter; DisabledAdapter is only for
    test environments and must be explicitly imported.
  - Removed ImportError fallback to MockMemOSAdapter.
    If real adapter import fails, the factory raises (fail-fast per S3).
  - L067: DisabledAdapter preserved in disabled_adapter.py for test use
    but NOT selected by the factory in any path.

P016: Lazy import — MemOS SDK / real adapter only imported when needed.
"""

from __future__ import annotations

import logging

from z_winnow.memory.types import MemOSAdapterProtocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P002: Factory function — single entry point for adapter creation
# ---------------------------------------------------------------------------


def create_memos_adapter(
    base_url: str | None = None,
) -> MemOSAdapterProtocol:
    """Create the appropriate MemOS adapter based on environment config.

    T-W12-8: Uses Settings.use_mock_memos for adapter dispatch.
    A013: All settings reads happen at call time (not module import),
    ensuring monkeypatch works correctly in tests.

    Dispatch logic (P002 — factory function):
      1. Settings.use_mock_memos=True  → MockMemOSAdapter (in-memory dict storage)
      2. Otherwise                     → MemOSAdapter (real httpx client)

    L067: DisabledAdapter is NOT selected by the factory. It is preserved
    in disabled_adapter.py for explicit test use only.

    Args:
        base_url: MemOS API base URL (only used in real mode).
                  Defaults to Settings.memos_api_url or
                  "http://127.0.0.1:8000".

    Returns:
        An adapter instance conforming to MemOSAdapterProtocol.

    Example:
        >>> adapter = create_memos_adapter()
        >>> results = await adapter.search_memories(
        ...     query="登录问题",
        ...     user_id="user1",
        ...     readable_cube_ids=["cube1"],
        ... )
    """
    # T-W12-8: Unified mock control via Settings (S7 配置单源)
    # A013: Read at call time (not module import time)
    from z_winnow.config.settings import get_settings

    settings = get_settings()

    # P010 Layer 2: Mock mode — deterministic in-memory storage, no HTTP
    if settings.use_mock_memos:
        logger.info("MemOS adapter: mock mode (WINNOW_ENV/MOCK_MEMOS)")
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        return MockMemOSAdapter()

    # P010 Layer 1: Real mode — httpx client (S3: required service)
    # P016: Lazy import — MemOSAdapter only loaded when needed
    # T-W12-6: No MEMOS_ENABLED check, no ImportError fallback (fail-fast per S3)
    # T-W12-8: Use Settings.memos_api_url instead of os.getenv
    url: str = base_url or settings.memos_api_url
    logger.info("MemOS adapter: real mode (base_url=%s)", url)

    from z_winnow.memory.adapter import MemOSAdapter

    return MemOSAdapter(base_url=url)
