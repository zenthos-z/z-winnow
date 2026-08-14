"""Models package — Pydantic models for data schemas and subagent I/O.

Provides shared data models used throughout the system for structured
data interchange between pipeline, graph, and subagent layers.

All models use Pydantic v2 with field-level validation and JSON Schema
export support.

Related packages:
    schemas/     — JSON Schema definitions (.json files)
    subagents/   — Subagent-specific I/O models (via contracts.py)
    rl/          — RL training data schema (RLRecord, RLState, etc.)

Note:
    Most subagent I/O models live in ``subagents/contracts.py`` rather
    than this package. This package contains models shared across
    multiple subsystems.
"""
