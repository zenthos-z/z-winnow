"""P1-6: Global asyncio.Lock for SQLite write transaction serialization.

When multiple groups run pipelines concurrently, each opens independent
aiosqlite connections that compete for SQLite's single-writer slot.
WAL mode + busy_timeout=5000ms is a soft mitigation, but under high
concurrency, SQLITE_BUSY errors can still occur.

This module provides a process-global write lock that serializes all
SQLite write transactions. Usage::

    from z_winnow.pipeline.db_lock import db_write_lock

    async with db_write_lock():
        await db.execute("INSERT INTO ...")
        await db.commit()

Or use the convenience wrapper::

    from z_winnow.pipeline.db_lock import with_db_write_lock

    result = await with_db_write_lock(my_async_write_fn, *args, **kwargs)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Module-level lock — single-writer serialization across all groups.
# Initialized lazily on first import; safe for use in async context.
_lock = asyncio.Lock()


async def db_write_lock() -> asyncio.Lock:
    """Acquire the global SQLite write lock.

    Usage::

        async with await db_write_lock():
            await db.execute(...)
            await db.commit()

    Returns:
        The acquired asyncio.Lock instance.
    """
    return _lock


async def with_db_write_lock(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute an async write function under the global DB write lock.

    Args:
        fn: Async function that performs SQLite writes.
        *args: Positional arguments forwarded to fn.
        **kwargs: Keyword arguments forwarded to fn.

    Returns:
        Whatever fn returns.

    Example::

        result = await with_db_write_lock(
            insert_raw_messages, messages=msgs, date="20260428"
        )
    """
    async with _lock:
        return await fn(*args, **kwargs)
