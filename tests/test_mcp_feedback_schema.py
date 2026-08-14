"""MCP submit_feedback schema 校验 —— 入参格式守门测试。

验证：不符合 schema 的请求在写库前被拒（raise ToolError），且 feedback_events 无新行；
合法 payload（含自定义表 target_type）仍正常入库。drift-guard 守护服务端 schema 与
客户端独立校验脚本 (scripts/validate_feedback.py) 的合法取值集一致。

见 plans/vast-bubbling-swan.md。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import aiosqlite
import pytest
from fastmcp.exceptions import ToolError

from z_winnow.mcp_server import server
from z_winnow.mcp_server.feedback_schema import (
    BASE_TARGET_TYPES,
    FeedbackSignal,
    allowed_target_types,
    validate_feedback_payload,
)
from z_winnow.pipeline.database import init_database_in_conn

_SKILL_SCRIPT = (
    Path(__file__).resolve().parents[1] / ".claude/skills/winnow-mcp/scripts/validate_feedback.py"
)


# ============================================================
# fixture —— 隔离 in-memory db（与 test_mcp_server.py 同模式）
# ============================================================


@pytest.fixture
async def mcp_db(monkeypatch):
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await init_database_in_conn(db)
    monkeypatch.setattr(server, "_db_conn", db)
    yield db
    monkeypatch.setattr(server, "_db_conn", None)
    await db.close()


async def _seed_group(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES ('g1','测试','r@x')"
    )
    await db.commit()


async def _count_feedback(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM feedback_events")
    return (await cur.fetchone())[0]


def _valid_kwargs(**overrides) -> dict:
    base = {
        "group_id": "g1",
        "date": "20260719",
        "target_type": "topic",
        "signal": "correction",
        "content": "结论应为 X",
    }
    base.update(overrides)
    return base


# ============================================================
# validate_feedback_payload —— 单元层
# ============================================================


def test_validate_accepts_valid_payload():
    sub = validate_feedback_payload(**_valid_kwargs())
    assert sub.signal is FeedbackSignal.CORRECTION
    assert sub.date == "2026-07-19"  # YYYYMMDD → 归一 YYYY-MM-DD
    assert sub.target_type == "topic"


def test_validate_normalizes_iso_date():
    sub = validate_feedback_payload(**_valid_kwargs(date="2026-07-19"))
    assert sub.date == "2026-07-19"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("signal", "fix"),
        ("target_type", "asdfg"),
        ("date", "2026-13-40"),  # 形态对但非法日历日期
        ("date", "notadate"),
        ("content", ""),
        ("group_id", ""),
        ("target_type", ""),
    ],
)
def test_validate_rejects_bad_field(field: str, bad_value: str):
    with pytest.raises(ToolError) as exc_info:
        validate_feedback_payload(**_valid_kwargs(**{field: bad_value}))
    msg = str(exc_info.value)
    assert "格式校验失败" in msg
    # 错误消息里点名出错的字段（content/group_id 空串时 pydantic 报 min_length，
    # loc 仍含字段名；signal/target_type/date 的自定义消息含字段语义）
    assert field in msg or "min_length" in msg or "at least 1 character" in msg


def test_validate_error_message_lists_allowed_values():
    with pytest.raises(ToolError) as exc_info:
        validate_feedback_payload(**_valid_kwargs(signal="fix", target_type="asdfg"))
    msg = str(exc_info.value)
    for s in ("correction", "supplement", "approval", "stale", "quality"):
        assert s in msg
    for t in BASE_TARGET_TYPES:
        assert t in msg


def test_validate_aggregates_multiple_violations():
    with pytest.raises(ToolError) as exc_info:
        validate_feedback_payload(
            group_id="",
            date="2026-13-40",
            target_type="asdfg",
            signal="fix",
            content="",
        )
    msg = str(exc_info.value)
    # 一条消息里同时点到多个字段，而不是只报第一个
    assert msg.count("•") >= 3


def test_allowed_target_types_includes_base_and_custom():
    allowed = allowed_target_types()
    assert allowed >= BASE_TARGET_TYPES
    # 内置自定义表（engineering / world_models）应被 registry 动态并入
    custom = allowed - BASE_TARGET_TYPES
    assert "engineering" in custom or "world_models" in custom, (
        f"expected built-in custom tables registered, got custom={custom}"
    )


def test_validate_accepts_registered_custom_table_target_type():
    """registry 已注册的自定义表 id 应通过 target_type 校验。"""
    custom = allowed_target_types() - BASE_TARGET_TYPES
    assert custom, "no custom tables registered — registry auto_register_builtin 未跑？"
    ttype = next(iter(custom))
    sub = validate_feedback_payload(**_valid_kwargs(target_type=ttype, signal="approval"))
    assert sub.target_type == ttype


# ============================================================
# server.submit_feedback —— 端到端（写库 / 不写库）
# ============================================================


async def test_submit_valid_payload_writes_row(mcp_db):
    await _seed_group(mcp_db)
    r = await server.submit_feedback(**_valid_kwargs())
    assert r["accepted"] is True

    cur = await mcp_db.execute(
        "SELECT date, signal, corrected_text, correction_note FROM feedback_events "
        "WHERE feedback_id = ?",
        (r["feedback_id"],),
    )
    row = await cur.fetchone()
    assert row["date"] == "2026-07-19"
    assert row["signal"] == "correction"
    assert row["corrected_text"] == "结论应为 X"
    assert row["correction_note"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"signal": "fix"},
        {"target_type": "asdfg"},
        {"date": "2026-13-40"},
        {"date": "notadate"},
        {"content": ""},
        {"group_id": ""},
    ],
)
async def test_submit_invalid_payload_rejected_no_write(mcp_db, overrides: dict):
    await _seed_group(mcp_db)
    before = await _count_feedback(mcp_db)
    with pytest.raises(ToolError):
        await server.submit_feedback(**_valid_kwargs(**overrides))
    after = await _count_feedback(mcp_db)
    assert after == before, "非法 payload 不应写入 feedback_events"


async def test_submit_custom_table_target_type_accepted(mcp_db):
    await _seed_group(mcp_db)
    custom = allowed_target_types() - BASE_TARGET_TYPES
    if not custom:
        pytest.skip("no custom tables registered")
    ttype = next(iter(custom))
    r = await server.submit_feedback(
        **_valid_kwargs(target_type=ttype, signal="approval", content="赞")
    )
    assert r["accepted"] is True


# ============================================================
# drift-guard —— 客户端独立脚本与服务端 schema 取值集一致
# ============================================================


def _load_skill_script():
    """把独立校验脚本作为模块加载（纯标准库，import 无副作用）。"""
    spec = importlib.util.spec_from_file_location("validate_feedback", _SKILL_SCRIPT)
    assert spec and spec.loader, f"无法加载 {_SKILL_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_drift_guard_signal_set_matches():
    mod = _load_skill_script()
    script_signals = set(mod.ALLOWED_SIGNALS)
    server_signals = {s.value for s in FeedbackSignal}
    assert script_signals == server_signals, (
        f"signal 集 drift：脚本={sorted(script_signals)} 服务端={sorted(server_signals)}"
    )


def test_drift_guard_base_target_types_match():
    mod = _load_skill_script()
    script_targets = set(mod.ALLOWED_TARGET_TYPES)
    assert script_targets == set(BASE_TARGET_TYPES), (
        f"target_type 基础集 drift：脚本={sorted(script_targets)} "
        f"服务端={sorted(BASE_TARGET_TYPES)}"
    )
