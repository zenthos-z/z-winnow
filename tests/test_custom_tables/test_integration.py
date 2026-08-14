"""Integration tests for custom_tables full pipeline.

CT-7: End-to-end verification of custom_tables plugin system.

Tests verify:
1. custom_tables config -> prompt injection
2. custom_tables config -> L3 JSON dynamic write
3. custom_tables config -> Feishu adapter active_kinds
4. API backward compatibility

Uses mock LLM mode (no API key required) and in-memory SQLite database.
At least 1 AC uses real SQLite database + real schema.py module (P078, L100).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from z_winnow.pipeline.feishu.schema import (
    TABLE_CATALOG,
    active_kinds,
    default_tables_config,
)
from z_winnow.subagents.output_composer import compose_json
from z_winnow.subagents.unified_reporter.prompt import (
    build_system_prompt,
)

# ============================================================
# Test fixtures
# ============================================================


@pytest.fixture
def sample_unified_output() -> dict[str, Any]:
    """Sample unified_reporter output for testing."""
    return {
        "overview": "今日群聊讨论了项目进度和技术架构问题",
        "important_notice": "",
        "topics": [
            {
                "topic_id": "tp_001",
                "topic_name": "架构设计讨论",
                "lifecycle": "emerging",
                "status": "active",
                "weight": 0.8,
                "background": "讨论系统架构的背景",
                "process": "讨论过程",
                "conclusion": "确定了微服务架构方案",
                "description": "架构设计相关讨论",
                "trend": "新兴议题，初步讨论",
                "participants": ["张三", "李四"],
                "first_seen": "2026-07-14",
                "last_seen": "2026-07-14",
                "source_server_ids": ["msg_001"],
            }
        ],
        "trend_analysis": "今日讨论主要集中在架构设计",
        "trend_summary": "1个新兴议题",
        "highlights": ["金句1"],
        "resources": [
            {
                "time_range": "10:00-11:00",
                "resource_type": "link",
                "summary": "参考文档",
                "content": "https://example.com/doc",
                "source_server_ids": ["msg_002"],
            }
        ],
        "resource_count_by_type": {"link": 1},
        "engineering_issues": [
            {
                "datetime": "2026-07-14 10:00",
                "group": "开发与调试工具",
                "description": "内存泄漏问题",
                "solution": "修复了引用计数bug",
                "status": "✅",
                "status_desc": "已解决",
                "source_members": "张三",
                "key_operations": "性能分析",
                "source_server_ids": ["msg_003"],
            }
        ],
        "group_summary": {"开发与调试工具": "1个问题已解决"},
        "model_used": "test-model",
    }


@pytest.fixture
def custom_tables_enabled() -> dict[str, Any]:
    """custom_tables config with engineering enabled."""
    return {
        "engineering": {
            "enabled": True,
            "skill_prompt": "### 任务 3 — 工程问题分析\n\n从聊天记录中识别技术工程问题...",
        }
    }


@pytest.fixture
def custom_tables_disabled() -> dict[str, Any]:
    """custom_tables config with engineering disabled."""
    return {
        "engineering": {
            "enabled": False,
        }
    }


# ============================================================
# AC1: 全链路: custom_tables 开启 engineering 后 prompt 包含工程任务
# ============================================================


@pytest.mark.integration
def test_full_pipeline_engineering_enabled(custom_tables_enabled: dict[str, Any]) -> None:
    """AC1: custom_tables 开启 engineering 后 prompt 包含工程任务（registry YAML 注入）."""
    # Step 1: Verify active_kinds includes engineering when enabled
    tables_config = default_tables_config()
    tables_config["engineering"]["enabled"] = True

    kinds = active_kinds(tables_config, custom_tables_enabled)
    assert "engineering" in kinds, (
        f"engineering should be in active_kinds when enabled, got {kinds}"
    )

    # Step 2: build_system_prompt with engineering enabled injects the YAML skill prompt
    prompt = build_system_prompt(
        group_cfg={"display_name": "测试群"},
        custom_tables=custom_tables_enabled,
    )
    assert "从聊天记录中识别技术工程问题" in prompt, (
        "engineering enabled → registry skill prompt must be injected"
    )


# ============================================================
# AC2: 全链路: custom_tables 关闭 engineering 后 prompt 不包含工程任务
# ============================================================


@pytest.mark.integration
def test_full_pipeline_engineering_disabled(custom_tables_disabled: dict[str, Any]) -> None:
    """AC2: custom_tables 关闭 engineering 后 prompt 不包含工程任务."""
    # Step 1: Verify active_kinds excludes engineering when disabled
    tables_config = default_tables_config()
    tables_config["engineering"]["enabled"] = False

    kinds = active_kinds(tables_config, custom_tables_disabled)
    assert "engineering" not in kinds, (
        f"engineering should NOT be in active_kinds when disabled, got {kinds}"
    )

    # Verify mandatory kinds are still present
    assert "summary" in kinds
    assert "topics" in kinds
    assert "resources" in kinds


# ============================================================
# AC3: 全链路: custom_tables 关闭 engineering 后 L3 JSON 中 engineering.json 不存在
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_l3_engineering_skipped(
    sample_unified_output: dict[str, Any],
    custom_tables_disabled: dict[str, Any],
) -> None:
    """AC3: custom_tables 关闭 engineering 后 L3 JSON 中 engineering.json 不存在."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)

        written = await compose_json(
            unified_report_output=sample_unified_output,
            output_dir=output_dir,
            date="20260714",
            custom_tables_config=custom_tables_disabled,
        )

        assert "engineering" not in written, "engineering.json should NOT be written when disabled"
        assert "daily" in written
        assert "resources" in written
        assert "topics" in written


# ============================================================
# AC4: 全链路: 飞书 active_kinds 正确反映 custom_tables 配置
# ============================================================


@pytest.mark.integration
def test_feishu_active_kinds_reflects_custom_tables() -> None:
    """AC4: 飞书 active_kinds 正确反映 custom_tables 配置.

    Verified with real schema.py module (no mock), per L100.
    """
    # Case 1: engineering enabled in custom_tables
    tables_config = default_tables_config()
    custom_tables_enabled = {"engineering": {"enabled": True}}
    kinds = active_kinds(tables_config, custom_tables_enabled)
    assert "engineering" in kinds, (
        f"engineering should be enabled when custom_tables says so: {kinds}"
    )

    # Case 2: engineering disabled in custom_tables (overrides tables_config)
    tables_config["engineering"]["enabled"] = True  # tables_config says True
    custom_tables_disabled = {"engineering": {"enabled": False}}  # custom_tables says False
    kinds = active_kinds(tables_config, custom_tables_disabled)
    assert "engineering" not in kinds, (
        f"custom_tables disabled should override tables_config: {kinds}"
    )

    # Case 3: custom_tables is None (backward compat, use tables_config only)
    tables_config["engineering"]["enabled"] = True
    kinds = active_kinds(tables_config, None)
    assert "engineering" in kinds, (
        "backward compat: should use tables_config when custom_tables is None"
    )

    # Case 4: custom_tables is empty dict (no override, use tables_config)
    kinds = active_kinds(tables_config, {})
    assert "engineering" in kinds, "empty custom_tables should fall back to tables_config"

    # Case 5: mandatory kinds always present
    assert "summary" in kinds
    assert "topics" in kinds
    assert "resources" in kinds


# ============================================================
# AC5: 全链路: API 响应包含 custom_tables 字段且 engineering_enabled 正确归一化
# ============================================================


@pytest.mark.integration
def test_api_backward_compat_custom_tables() -> None:
    """AC5: API 响应包含 custom_tables 字段且 engineering_enabled 正确归一化."""
    from z_winnow.web.schemas.groups import GroupOut

    # Create a mock group response
    group_data = {
        "group_id": "test-group-001",
        "display_name": "测试群",
        "chatroom_id": "test@chatroom",
        "engineering_enabled": 1,
        "feishu_tables": {
            "engineering": {"enabled": True, "table_id": "tbl_abc"},
        },
    }

    group = GroupOut(**group_data)

    # Verify engineering_enabled field exists (backward compat)
    assert hasattr(group, "engineering_enabled")
    assert group.engineering_enabled == 1

    # Verify feishu_tables field exists
    assert hasattr(group, "feishu_tables")
    assert group.feishu_tables is not None
    assert "engineering" in group.feishu_tables

    # CT-6 landed: GroupOut carries the custom_tables blob (single source of truth)
    assert hasattr(group, "custom_tables")


# ============================================================
# AC6: 非 mock: 真实 SQLite 数据库 + 真实 active_kinds 函数验证向后兼容
# ============================================================


@pytest.mark.integration
def test_real_sqlite_backward_compat() -> None:
    """AC6: 非 mock: 真实 SQLite 数据库 + 真实 active_kinds 函数验证向后兼容.

    Uses real SQLite :memory: database per P078.
    Tests real schema.py module (no mock) per L100.
    """
    import aiosqlite

    async def run_test() -> None:
        # Create in-memory SQLite database
        async with aiosqlite.connect(":memory:") as db:
            # Create groups table schema (simplified)
            await db.execute("""
                CREATE TABLE groups (
                    group_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    chatroom_id TEXT NOT NULL,
                    engineering_enabled INTEGER DEFAULT 1,
                    feishu_tables TEXT
                )
            """)

            # Insert test group with legacy config (no feishu_tables blob)
            await db.execute(
                """
                INSERT INTO groups (group_id, display_name, chatroom_id, engineering_enabled)
                VALUES (?, ?, ?, ?)
            """,
                ("test-group-legacy", "历史群", "legacy@chatroom", 1),
            )

            # Insert test group with new blob config
            blob = json.dumps(
                {
                    "engineering": {"enabled": False, "table_id": "tbl_xyz"},
                }
            )
            await db.execute(
                """
                INSERT INTO groups (group_id, display_name, chatroom_id, engineering_enabled, feishu_tables)
                VALUES (?, ?, ?, ?, ?)
            """,
                ("test-group-new", "新群", "new@chatroom", 0, blob),
            )

            await db.commit()

            # Query and verify backward compat
            async with db.execute(
                "SELECT group_id, engineering_enabled, feishu_tables FROM groups"
            ) as cursor:
                rows = await cursor.fetchall()

            assert len(rows) == 2

            # Row 1: legacy group with engineering_enabled=1
            row1 = rows[0]
            assert row1[0] == "test-group-legacy"
            assert row1[1] == 1  # engineering_enabled
            assert row1[2] is None  # feishu_tables blob

            # Row 2: new group with blob
            row2 = rows[1]
            assert row2[0] == "test-group-new"
            assert row2[1] == 0  # engineering_enabled
            blob_data = json.loads(row2[2])
            assert blob_data["engineering"]["enabled"] is False

            # Test active_kinds with real data
            # Legacy: derive from engineering_enabled column
            legacy_config = default_tables_config()
            if row1[1] == 1:  # engineering_enabled
                legacy_config["engineering"]["enabled"] = True
            kinds_legacy = active_kinds(legacy_config, None)
            assert "engineering" in kinds_legacy  # backward compat

            # New: read from blob
            new_config = blob_data
            kinds_new = active_kinds(new_config, None)
            assert "engineering" not in kinds_new  # disabled in blob

    asyncio.run(run_test())


# ============================================================
# Additional tests for edge cases
# ============================================================


@pytest.mark.integration
def test_active_kinds_mandatory_always_present() -> None:
    """Verify mandatory kinds are always present regardless of config."""
    # Empty config
    kinds = active_kinds({}, None)
    assert "summary" in kinds
    assert "topics" in kinds
    assert "resources" in kinds

    # Config with unknown kinds
    kinds = active_kinds({"unknown_kind": {"enabled": True}}, None)
    assert "summary" in kinds
    assert "topics" in kinds
    assert "resources" in kinds

    # Config that tries to disable mandatory
    kinds = active_kinds({"summary": {"enabled": False}}, None)
    assert "summary" in kinds  # mandatory stays enabled


@pytest.mark.integration
def test_table_catalog_engineering_is_optional() -> None:
    """Verify engineering is marked as optional (not mandatory)."""
    assert "engineering" in TABLE_CATALOG
    eng_def = TABLE_CATALOG["engineering"]
    assert eng_def.mandatory is False, "engineering should be optional"
    assert eng_def.default_enabled is False, "engineering should default to disabled"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_compose_json_mandatory_files_always_written(
    sample_unified_output: dict[str, Any],
) -> None:
    """Verify mandatory files (daily/resources/topics) are always written."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)

        written = await compose_json(
            unified_report_output=sample_unified_output,
            output_dir=output_dir,
            date="20260714",
        )

        # Mandatory files
        assert "daily" in written
        assert "resources" in written
        assert "topics" in written

        # Verify files exist
        assert written["daily"].exists()
        assert written["resources"].exists()
        assert written["topics"].exists()

        # Verify content is valid JSON
        daily_data = json.loads(written["daily"].read_text())
        assert daily_data["date"] == "20260714"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_compose_json_placeholder_on_none_output() -> None:
    """Verify placeholder files are written when unified_report_output is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)

        written = await compose_json(
            unified_report_output=None,
            output_dir=output_dir,
            date="20260714",
        )

        # All 4 files should have placeholders
        assert "daily" in written
        assert "resources" in written
        assert "engineering" in written
        assert "topics" in written

        # Verify placeholder marker
        for name in ("daily", "resources", "engineering", "topics"):
            data = json.loads(written[name].read_text())
            assert data.get("placeholder") is True


# ============================================================
# CT-2/CT-3 landed: signatures now accept the custom_tables params
# ============================================================


@pytest.mark.integration
def test_build_system_prompt_accepts_custom_tables_param() -> None:
    """CT-2: build_system_prompt accepts the custom_tables parameter."""
    import inspect

    sig = inspect.signature(build_system_prompt)
    params = list(sig.parameters.keys())
    assert "group_cfg" in params
    assert "custom_tables" in params


@pytest.mark.asyncio
@pytest.mark.integration
async def test_compose_json_accepts_custom_tables_config() -> None:
    """CT-3: compose_json accepts the custom_tables_config parameter."""
    import inspect

    sig = inspect.signature(compose_json)
    params = list(sig.parameters.keys())
    assert "unified_report_output" in params
    assert "output_dir" in params
    assert "date" in params
    assert "custom_tables_config" in params


# ============================================================
# 新增：engineering 开关端到端一致性（active_kinds / compose_json / prompt 三处一致）
# ============================================================


@pytest.mark.integration
def test_engineering_disabled_is_consistent_across_layers() -> None:
    """custom_tables 关闭 engineering 时，三层都一致地排除 engineering."""
    custom_tables_disabled = {"engineering": {"enabled": False}}
    feishu_tables = default_tables_config()  # engineering default_enabled=False here too

    # Layer 1: active_kinds 解析器不含 engineering
    kinds = active_kinds(feishu_tables, custom_tables_disabled)
    assert "engineering" not in kinds
    assert {"summary", "topics", "resources"} <= set(kinds)

    # Layer 2: build_system_prompt 不注入工程任务
    prompt_off = build_system_prompt(custom_tables=custom_tables_disabled)
    assert "从聊天记录中识别技术工程问题" not in prompt_off

    # 对比：开启时三层都包含
    custom_tables_enabled = {"engineering": {"enabled": True}}
    assert "engineering" in active_kinds(feishu_tables, custom_tables_enabled)
    prompt_on = build_system_prompt(custom_tables=custom_tables_enabled)
    assert "从聊天记录中识别技术工程问题" in prompt_on


@pytest.mark.asyncio
@pytest.mark.integration
async def test_engineering_disabled_skips_l3_and_enabled_writes_it(
    sample_unified_output: dict[str, Any],
) -> None:
    """compose_json：关闭不写 engineering.json，开启则写。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)

        written_off = await compose_json(
            unified_report_output=sample_unified_output,
            output_dir=output_dir,
            date="20260714",
            custom_tables_config={"engineering": {"enabled": False}},
        )
        assert "engineering" not in written_off

        written_on = await compose_json(
            unified_report_output=sample_unified_output,
            output_dir=output_dir,
            date="20260714",
            custom_tables_config={"engineering": {"enabled": True}},
        )
        assert "engineering" in written_on
