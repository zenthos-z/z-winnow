"""RL pipeline — training data extraction, reward computation, and validation.

Provides the four-tuple (state, action, reward, metadata) extraction from
SQLite three-layer data and orchestrator logs, following the JSONL schema
defined in schemas/rl_record_v1.json.
"""

from z_winnow.rl.schema import (
    RLAction,
    RLMetadata,
    RLProvenance,
    RLRecord,
    RLReward,
    RLRewardDimension,
    RLRewardSignal,
    RLState,
)

# Lazy imports — these modules are built incrementally across T-I1~T-I5
try:
    from z_winnow.rl.extractor import (
        extract_records,
        extract_rl_records,
    )
except ImportError:
    extract_records = None  # type: ignore
    extract_rl_records = None  # type: ignore

try:
    from z_winnow.rl.reward import compute_reward
except ImportError:
    compute_reward = None  # type: ignore

try:
    from z_winnow.rl.exporter import (
        compute_weighted_score,
        export_dataset,
    )
except ImportError:
    compute_weighted_score = None  # type: ignore
    export_dataset = None  # type: ignore

__all__ = [
    "RLAction",
    "RLMetadata",
    "RLProvenance",
    "RLRecord",
    "RLReward",
    "RLRewardDimension",
    "RLRewardSignal",
    "RLState",
    "compute_reward",
    "compute_weighted_score",
    "export_dataset",
    "extract_records",
    "extract_rl_records",
]
