"""z-winnow — LangGraph multi-agent daily report system for WeChat group chat analysis.

Overview:
    Analyzes WeChat group chat messages via CipherTalk to generate
    structured daily reports with topic tracking and issue analysis.

Package layout:
    graph/        — LangGraph StateGraph construction + error recovery
    pipeline/     — CipherTalk API client + SQLite three-layer storage + provenance
    subagents/    — 4 analysis subagents (daily, resources, engineering, composer)
    orchestrator/ — DeepAgent-based task decomposition and scheduling
    outputs/      — Feishu Bitable, image generation
    rl/           — RL training data extraction, reward computation, validation
    config/       — pydantic-settings configuration + model factory
    models/       — Pydantic data models for subagent I/O
    schemas/      — JSON Schema definitions for data interchange
    templates/    — Jinja2 report templates and Markdown renderer
    compat/       — Legacy winnow Claude Code Skill compatibility layer
    storage.py    — Unified async storage interface

Quick start:
    >>> from z_winnow.pipeline import run
    >>> stats = await run("20260428", group_name="Your Group")
    >>> print(stats)
    {'total': 250, 'new': 230, 'sanitized': 0, 'context_count': 12}

CLI:
    $ poetry run winnow ingest --date 2026-04-28 --group "Your Group"
    $ poetry run winnow trace --server-id 1234567890
    $ poetry run winnow export --start 20260401 --end 20260428

Version:
    0.1.0 — initial release with LangGraph orchestration, SQLite storage,
    and 4 subagent analysis pipeline.
"""
