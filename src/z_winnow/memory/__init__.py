"""T-W8-2 / T-W10-E-a / T-W10-E-ba: MemOS memory module — async adapter with 3-mode architecture.

Provides:
- MemOSAdapter: Real httpx-based MemOS adapter (MEMOS_ENABLED=true, WINNOW_ENV≠test)
- MockMemOSAdapter: Deterministic mock adapter with in-memory dict storage (WINNOW_ENV=test)
- DisabledAdapter: Silent empty-result adapter (MEMOS_ENABLED=false) — T-W10-E-ba
- create_memos_adapter(): Factory function — P002 pattern

Architecture (P010 — 3-layer mock):
  Layer 1: MemOSAdapter → real httpx calls
  Layer 2: MockMemOSAdapter → in-memory dict storage, no HTTP
  Layer 3: DisabledAdapter → silent empty returns

Factory dispatch (P002 — factory function + closure):
  WINNOW_ENV=test   → MockMemOSAdapter
  MEMOS_ENABLED=false     → DisabledAdapter (L018: explicit branch)
  otherwise               → MemOSAdapter

T-W10-E-a: Factory moved to factory.py; __init__.py is thin re-export layer.
T-W10-E-ba: DisabledAdapter in disabled_adapter.py; StructuredMemoryItem +
  TreeNodeTextualMemoryMetadata in types.py.
Uses TreeTextMemory module with Neo4j (NEO4J_BACKEND=neo4j-community in docker-compose).
  TreeNodeTextualMemoryMetadata is the primary type for tree-node memories;
  TextualMemoryMetadata retained for backward compat.
"""

from __future__ import annotations

from z_winnow.memory.adapter import MemOSAdapter
from z_winnow.memory.disabled_adapter import DisabledAdapter
from z_winnow.memory.factory import create_memos_adapter
from z_winnow.memory.mock_adapter import MockMemOSAdapter
from z_winnow.memory.types import (
    MemoryResult,
    MemOSAdapterProtocol,
    StructuredMemoryItem,
    TextualMemoryMetadata,
    TreeNodeTextualMemoryMetadata,
)

# T-W10-E-e placeholder: lifecycle.py is not yet implemented.
# Conditional import so other memory submodules remain usable.
try:
    from z_winnow.memory.lifecycle import (
        LifecycleReport,
        scan_lifecycle,
        vacuum,
    )

    _lifecycle_available = True
except ImportError:
    LifecycleReport: object = None  # type: ignore[no-redef]
    scan_lifecycle: object = None  # type: ignore[no-redef]
    vacuum: object = None  # type: ignore[no-redef]
    _lifecycle_available = False

# T-W10-E-c: sync_ops and sync_worker are also optional
try:
    from z_winnow.memory.sync_ops import dispatch_op
    from z_winnow.memory.sync_worker import (
        process_one,
        start_worker,
        stop_worker,
    )

    _sync_available = True
except ImportError:
    dispatch_op: object = None  # type: ignore[no-redef]
    process_one: object = None  # type: ignore[no-redef]
    start_worker: object = None  # type: ignore[no-redef]
    stop_worker: object = None  # type: ignore[no-redef]
    _sync_available = False

__all__ = [
    "DisabledAdapter",
    "LifecycleReport",
    "MemOSAdapter",
    "MemOSAdapterProtocol",
    "MemoryResult",
    "MockMemOSAdapter",
    "StructuredMemoryItem",
    "TextualMemoryMetadata",
    "TreeNodeTextualMemoryMetadata",
    "create_memos_adapter",
    "dispatch_op",
    "process_one",
    "scan_lifecycle",
    "start_worker",
    "stop_worker",
    "vacuum",
]
