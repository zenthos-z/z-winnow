"""CT-2: Tests for unified_reporter prompt dynamic injection.

Covers inject_custom_table_prompts, build_system_prompt with custom_tables,
and node_unified_reporter custom_tables integration.

P078: Uses real SQLite :memory: for DB-dependent tests (AC5, AC6).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from z_winnow.subagents.unified_reporter.prompt import (
    _TASK3_ENGINEERING_ANALYSIS,
    SYSTEM_PROMPT,
    build_system_prompt,
    inject_custom_table_prompts,
)

# ──────────────────────────────────────────────
# AC1: inject_custom_table_prompts function exists
# ──────────────────────────────────────────────


class TestInjectFunctionExists:
    """AC1: prompt.py 包含 inject_custom_table_prompts 函数."""

    def test_function_is_callable(self) -> None:
        """inject_custom_table_prompts is a callable function."""
        assert callable(inject_custom_table_prompts)

    def test_function_accepts_correct_signature(self) -> None:
        """inject_custom_table_prompts accepts (str, list[dict])."""
        result = inject_custom_table_prompts("test", [])
        assert isinstance(result, str)

    def test_system_prompt_has_no_task3(self) -> None:
        """SYSTEM_PROMPT constant no longer contains Task 3."""
        assert "### 任务 3 — 工程问题分析" not in SYSTEM_PROMPT
        assert "### 任务 1 — 日报与议题分析" in SYSTEM_PROMPT
        assert "### 任务 2 — 资源提取" in SYSTEM_PROMPT

    def test_task3_constant_exists(self) -> None:
        """_TASK3_ENGINEERING_ANALYSIS contains the old Task 3 text."""
        assert "### 任务 3 — 工程问题分析" in _TASK3_ENGINEERING_ANALYSIS
        assert "只提取真实讨论的工程问题" in _TASK3_ENGINEERING_ANALYSIS


# ──────────────────────────────────────────────
# AC2: inject_custom_table_prompts appends to end
# ──────────────────────────────────────────────


class TestInjectAppendsToEnd:
    """AC2: inject_custom_table_prompts 正确拼接 skill prompt 到 system prompt 末尾."""

    BASE = "You are an expert analyst."

    def test_custom_prompt_appended_to_end(self) -> None:
        """Custom skill prompt is appended at the end of system prompt."""
        tables: list[dict[str, Any]] = [
            {
                "kind": "engineering",
                "enabled": True,
                "config": {"prompt": "分析工程问题，重点关注部署和性能。"},
            }
        ]
        result = inject_custom_table_prompts(self.BASE, tables)
        assert result.endswith("分析工程问题，重点关注部署和性能。")
        assert result.startswith(self.BASE)

    def test_multiple_tables_all_appended(self) -> None:
        """Multiple custom table prompts all get appended."""
        tables: list[dict[str, Any]] = [
            {
                "kind": "engineering",
                "enabled": True,
                "config": {"prompt": "ENG_PROMPT"},
            },
            {
                "kind": "security",
                "enabled": True,
                "config": {"prompt": "SEC_PROMPT"},
            },
        ]
        result = inject_custom_table_prompts(self.BASE, tables)
        assert "ENG_PROMPT" in result
        assert "SEC_PROMPT" in result
        assert result.startswith(self.BASE)

    def test_table_without_prompt_skipped(self) -> None:
        """Table without a prompt in config is skipped (falls back to old Task 3)."""
        tables: list[dict[str, Any]] = [
            {
                "kind": "engineering",
                "enabled": True,
                "config": {"other_field": "value"},
            },
        ]
        result = inject_custom_table_prompts(self.BASE, tables)
        assert _TASK3_ENGINEERING_ANALYSIS.strip() in result

    def test_table_with_empty_prompt_skipped(self) -> None:
        """Table with empty/whitespace prompt is skipped."""
        tables: list[dict[str, Any]] = [
            {
                "kind": "engineering",
                "enabled": True,
                "config": {"prompt": "   "},
            },
        ]
        result = inject_custom_table_prompts(self.BASE, tables)
        assert _TASK3_ENGINEERING_ANALYSIS.strip() in result

    def test_table_config_not_a_dict(self) -> None:
        """Table with non-dict config is safely skipped."""
        tables: list[dict[str, Any]] = [
            {
                "kind": "engineering",
                "enabled": True,
                "config": "not_a_dict",
            },
        ]
        result = inject_custom_table_prompts(self.BASE, tables)
        assert _TASK3_ENGINEERING_ANALYSIS.strip() in result


# ──────────────────────────────────────────────
# AC3: Empty config → old Task 3 fallback
# ──────────────────────────────────────────────


class TestEmptyConfigFallback:
    """AC3: build_system_prompt 无启用表时不注入任何任务（engineering 默认关闭，无硬编码回退）。

    inject_custom_table_prompts 作为独立工具函数仍保留其自身 fallback（仅单元测试覆盖），
    但 build_system_prompt 已改走 YAML registry，不再无条件回退到硬编码 Task 3。"""

    BASE = "You are an expert analyst."

    def test_empty_list_fallback(self) -> None:
        """inject_custom_table_prompts 工具函数自身仍保留 fallback（独立单元测试）。"""
        result = inject_custom_table_prompts(self.BASE, [])
        assert _TASK3_ENGINEERING_ANALYSIS.strip() in result

    def test_none_custom_tables_no_injection(self) -> None:
        """None custom_tables → 不注入工程任务（默认关闭）。"""
        result = build_system_prompt(custom_tables=None)
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result
        assert "从聊天记录中识别技术工程问题" not in result

    def test_empty_dict_custom_tables_no_injection(self) -> None:
        """空 dict custom_tables → 不注入工程任务。"""
        result = build_system_prompt(custom_tables={})
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result

    def test_disabled_table_not_injected(self) -> None:
        """engineering 显式关闭 → 不注入工程任务，config.prompt 也忽略。"""
        result = build_system_prompt(
            custom_tables={
                "engineering": {"enabled": False, "config": {"prompt": "CUSTOM"}},
            }
        )
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result
        assert "CUSTOM" not in result  # disabled → 不注入
        assert "从聊天记录中识别技术工程问题" not in result

    def test_no_custom_tables_arg_no_injection(self) -> None:
        """build_system_prompt 不传 custom_tables → 不注入工程任务。"""
        result = build_system_prompt()
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result


# ──────────────────────────────────────────────
# AC4: build_system_prompt with custom_tables
# ──────────────────────────────────────────────


class TestBuildSystemPromptWithCustomTables:
    """AC4: build_system_prompt 启用 engineering 时注入 YAML registry 的技能 prompt."""

    def test_enabled_injects_registry_skill(self) -> None:
        """engineering 启用 → 注入 YAML 技能 prompt（非 config.prompt）。"""
        result = build_system_prompt(
            custom_tables={
                "engineering": {
                    "enabled": True,
                    "config": {"prompt": "SHOULD_BE_IGNORED"},
                }
            }
        )
        # YAML skill prompt content is injected
        assert "从聊天记录中识别技术工程问题" in result
        assert "### 任务 1 — 日报与议题分析" in result
        assert "### 任务 2 — 资源提取" in result
        # build_system_prompt sources prompts from the registry, not blob config.prompt
        assert "SHOULD_BE_IGNORED" not in result

    def test_group_cfg_with_custom_tables(self) -> None:
        """build_system_prompt works with both group_cfg and custom_tables."""
        result = build_system_prompt(
            group_cfg={"display_name": "测试群"},
            custom_tables={"engineering": {"enabled": True, "config": {}}},
        )
        assert "测试群" in result
        assert "从聊天记录中识别技术工程问题" in result

    def test_group_cfg_none_with_custom_tables(self) -> None:
        """build_system_prompt with group_cfg=None and custom_tables."""
        result = build_system_prompt(
            group_cfg=None,
            custom_tables={"engineering": {"enabled": True, "config": {}}},
        )
        assert "从聊天记录中识别技术工程问题" in result

    def test_task3_not_duplicated(self) -> None:
        """启用表注入 registry 片段，不再附加旧硬编码 Task 3。"""
        result = build_system_prompt(
            custom_tables={"engineering": {"enabled": True, "config": {}}}
        )
        assert "从聊天记录中识别技术工程问题" in result
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result


# ──────────────────────────────────────────────
# AC5: node_unified_reporter reads custom_tables from DB
# ──────────────────────────────────────────────


class TestGraphNodeReadsCustomTables:
    """AC5: node_unified_reporter 从数据库读取 custom_tables 配置并传入 build_system_prompt.

    P078: Uses real SQLite :memory: database (no mock DB).
    """

    @pytest.mark.asyncio
    async def test_node_passes_custom_tables_to_generate(self) -> None:
        """node_unified_reporter reads custom_tables and passes to generate_unified_report."""
        import os
        import tempfile

        import aiosqlite

        from z_winnow.graph.builder import node_unified_reporter

        custom_tables_data = {
            "engineering": {
                "enabled": True,
                "config": {"prompt": "DB_CUSTOM_PROMPT"},
            }
        }

        # P078: Real SQLite temp file database
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
        db_path = tmp.name
        tmp.close()
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """CREATE TABLE groups (
                        group_id TEXT PRIMARY KEY,
                        display_name TEXT,
                        custom_prompt_hints TEXT,
                        engineering_enabled INTEGER DEFAULT 1,
                        custom_tables TEXT,
                        feishu_tables TEXT
                    )"""
                )
                await db.execute(
                    """CREATE TABLE group_members (
                        group_id TEXT,
                        name TEXT,
                        role TEXT,
                        weight REAL,
                        is_active INTEGER DEFAULT 1
                    )"""
                )
                await db.execute(
                    "INSERT INTO groups (group_id, display_name, engineering_enabled, custom_tables) VALUES (?, ?, 1, ?)",
                    ("test_ct2_ac5", "测试群CT2", json.dumps(custom_tables_data)),
                )
                await db.commit()

            state: dict[str, Any] = {
                "date": "20260714",
                "messages": [
                    {
                        "svrid": "msg_001",
                        "sender": "张三",
                        "content": "测试消息",
                        "timestamp": "2026-07-14 10:00:00",
                    }
                ],
                "group_name": "测试群CT2",
                "group_id": "test_ct2_ac5",
                "chat_context_markdown": "### msg_001\n**张三** 10:00\n测试消息\n",
            }

            with (
                patch(
                    "z_winnow.config.settings.get_settings"
                ) as mock_settings,
                patch(
                    "z_winnow.subagents.unified_reporter.generate_unified_report"
                ) as mock_generate,
            ):
                mock_settings.return_value.use_mock_llm = False
                mock_settings.return_value.sqlite_db_path = db_path
                mock_settings.return_value.environment = "production"

                mock_result = MagicMock()
                mock_result.model_dump.return_value = {
                    "overview": "test",
                    "topics": [],
                    "resources": [],
                    "engineering_issues": [],
                    "group_summary": {},
                    "custom_tables": {},
                }
                mock_generate.return_value = mock_result

                await node_unified_reporter(state)

                call_kwargs = mock_generate.call_args.kwargs
                assert "custom_tables" in call_kwargs, (
                    f"custom_tables not in generate_unified_report kwargs: {list(call_kwargs.keys())}"
                )
                # The builder resolves a blob over ALL catalog kinds (engineering
                # + world_models + …). Assert the engineering entry flows through
                # correctly; other kinds (e.g. world_models disabled) may also be present.
                resolved = call_kwargs["custom_tables"]
                assert resolved["engineering"] == custom_tables_data["engineering"], (
                    f"engineering mismatch: expected {custom_tables_data['engineering']}, "
                    f"got {resolved.get('engineering')}"
                )
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_node_passes_none_when_column_null(self) -> None:
        """node_unified_reporter passes None when custom_tables column is NULL."""
        import os
        import tempfile

        import aiosqlite

        from z_winnow.graph.builder import node_unified_reporter

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
        db_path = tmp.name
        tmp.close()
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """CREATE TABLE groups (
                        group_id TEXT PRIMARY KEY,
                        display_name TEXT,
                        custom_prompt_hints TEXT,
                        engineering_enabled INTEGER DEFAULT 1,
                        custom_tables TEXT,
                        feishu_tables TEXT
                    )"""
                )
                await db.execute(
                    """CREATE TABLE group_members (
                        group_id TEXT,
                        name TEXT,
                        role TEXT,
                        weight REAL,
                        is_active INTEGER DEFAULT 1
                    )"""
                )
                # NULL custom_tables
                await db.execute(
                    "INSERT INTO groups (group_id, display_name, engineering_enabled, custom_tables) VALUES (?, ?, 1, NULL)",
                    ("test_ct2_ac5_null", "测试群NULL"),
                )
                await db.commit()

            state: dict[str, Any] = {
                "date": "20260714",
                "messages": [
                    {
                        "svrid": "msg_001",
                        "sender": "张三",
                        "content": "测试消息",
                        "timestamp": "2026-07-14 10:00:00",
                    }
                ],
                "group_name": "测试群NULL",
                "group_id": "test_ct2_ac5_null",
                "chat_context_markdown": "### msg_001\n**张三** 10:00\n测试消息\n",
            }

            with (
                patch(
                    "z_winnow.config.settings.get_settings"
                ) as mock_settings,
                patch(
                    "z_winnow.subagents.unified_reporter.generate_unified_report"
                ) as mock_generate,
            ):
                mock_settings.return_value.use_mock_llm = False
                mock_settings.return_value.sqlite_db_path = db_path
                mock_settings.return_value.environment = "production"

                mock_result = MagicMock()
                mock_result.model_dump.return_value = {
                    "overview": "test",
                    "topics": [],
                    "resources": [],
                    "engineering_issues": [],
                    "group_summary": {},
                    "custom_tables": {},
                }
                mock_generate.return_value = mock_result

                await node_unified_reporter(state)

                call_kwargs = mock_generate.call_args.kwargs
                # custom_tables column is NULL and feishu_tables is NULL → the node
                # resolves engineering via the deprecated column (=1) and passes a
                # resolved blob (engineering on), not None.
                ct = call_kwargs.get("custom_tables")
                assert ct is not None
                assert ct["engineering"]["enabled"] is True
        finally:
            os.unlink(db_path)


# ──────────────────────────────────────────────
# AC6: Error path — DB failure or JSON parse failure → fallback
# ──────────────────────────────────────────────


class TestErrorPathFallback:
    """AC6: 异常输入不崩溃，且不回退到硬编码 Task 3（registry 驱动，无 fallback）。"""

    def test_non_dict_custom_tables_safe(self) -> None:
        """非 dict 的 custom_tables（模拟 JSON 解析失败）→ 不崩溃、不注入工程任务。"""
        result = build_system_prompt(custom_tables="not_a_dict")  # type: ignore[arg-type]
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result
        assert "从聊天记录中识别技术工程问题" not in result

    def test_malformed_config_structure_safe(self) -> None:
        """畸形的 custom_tables 结构 → 跳过坏项、不注入、不崩溃。"""
        result = build_system_prompt(
            custom_tables={
                "engineering": None,  # Not a dict → skipped by registry
                "security": "string_instead_of_dict",  # not a registered table
                "ops": {"enabled": True},  # not a registered table
            }
        )
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result
        assert "从聊天记录中识别技术工程问题" not in result

    def test_none_config_value_safe(self) -> None:
        """engineering 启用但 config=None → registry 仍按 YAML 技能注入（忽略 config）。"""
        result = build_system_prompt(
            custom_tables={
                "engineering": {"enabled": True, "config": None},
            }
        )
        assert "从聊天记录中识别技术工程问题" in result
        assert _TASK3_ENGINEERING_ANALYSIS.strip() not in result

    @pytest.mark.asyncio
    async def test_db_row_missing_column(self) -> None:
        """When DB row exists but custom_tables column is absent → fallback."""
        import os
        import tempfile

        import aiosqlite

        from z_winnow.graph.builder import node_unified_reporter

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
        db_path = tmp.name
        tmp.close()
        try:
            async with aiosqlite.connect(db_path) as db:
                # Create table WITHOUT custom_tables column (simulating pre-migration)
                await db.execute(
                    """CREATE TABLE groups (
                        group_id TEXT PRIMARY KEY,
                        display_name TEXT,
                        custom_prompt_hints TEXT,
                        engineering_enabled INTEGER DEFAULT 1
                    )"""
                )
                await db.execute(
                    """CREATE TABLE group_members (
                        group_id TEXT,
                        name TEXT,
                        role TEXT,
                        weight REAL,
                        is_active INTEGER DEFAULT 1
                    )"""
                )
                await db.execute(
                    "INSERT INTO groups (group_id, display_name, engineering_enabled) VALUES (?, ?, 1)",
                    ("test_ct2_ac6", "测试群AC6"),
                )
                await db.commit()

            state: dict[str, Any] = {
                "date": "20260714",
                "messages": [
                    {
                        "svrid": "msg_001",
                        "sender": "张三",
                        "content": "测试消息",
                        "timestamp": "2026-07-14 10:00:00",
                    }
                ],
                "group_name": "测试群AC6",
                "group_id": "test_ct2_ac6",
                "chat_context_markdown": "### msg_001\n**张三** 10:00\n测试消息\n",
            }

            with (
                patch(
                    "z_winnow.config.settings.get_settings"
                ) as mock_settings,
                patch(
                    "z_winnow.subagents.unified_reporter.generate_unified_report"
                ) as mock_generate,
            ):
                mock_settings.return_value.use_mock_llm = False
                mock_settings.return_value.sqlite_db_path = db_path
                mock_settings.return_value.environment = "production"

                mock_result = MagicMock()
                mock_result.model_dump.return_value = {
                    "overview": "test",
                    "topics": [],
                    "resources": [],
                    "engineering_issues": [],
                    "group_summary": {},
                    "custom_tables": {},
                }
                mock_generate.return_value = mock_result

                # Should handle the missing column gracefully (DB error → exception caught)
                # The except block catches it and custom_tables stays None
                await node_unified_reporter(state)

                call_kwargs = mock_generate.call_args.kwargs
                # custom_tables should be None (fallback)
                assert call_kwargs.get("custom_tables") is None
        finally:
            os.unlink(db_path)
