"""MCP server tools — L3 read + feedback Inbox.

直接 await 工具函数测试（隔离 in-memory db，不经 MCP 协议）。
验证：LIKE 检索（FTS5 替代方案）、议题详情/时间线/反馈、日报版本解析、
反馈日期归一化 + 字段路由。

见 docs/mcp-platform-checkpoint.md §4.1。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import aiosqlite
import pytest
from fastmcp.exceptions import ToolError

from z_winnow.mcp_server import server
from z_winnow.mcp_server.mcp_keys import MemberInfo
from z_winnow.pipeline.database import init_database_in_conn


@pytest.fixture
async def mcp_db(monkeypatch):
    """隔离 in-memory db，注入 server.get_db() 单例。

    server.get_db() 检查模块级 ``_db_conn is None``；fixture 直接设为 in-memory 连接，
    跳过 get_db 的真实连接初始化，实现测试隔离。
    """
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await init_database_in_conn(db)
    monkeypatch.setattr(server, "_db_conn", db)
    yield db
    monkeypatch.setattr(server, "_db_conn", None)
    await db.close()


async def _insert_topic(db: aiosqlite.Connection, **overrides: object) -> None:
    """插入一行 topic_summaries（必填字段给默认值，可覆盖）。"""
    defaults: dict[str, object] = {
        "summary_id": "sum_x",
        "date": "20260719",
        "group_id": "g_test",
        "topic_name": "测试议题",
        "topic_id": "",
        "summary_text": "测试摘要内容",
        "context_ids": "[]",
        "source_server_ids": "[]",
    }
    defaults.update(overrides)
    cols = list(defaults.keys())
    placeholders = ",".join("?" * len(cols))
    await db.execute(
        f"INSERT OR REPLACE INTO topic_summaries ({','.join(cols)}) VALUES ({placeholders})",
        list(defaults.values()),
    )


# ============================================================
# list_groups
# ============================================================


async def test_list_groups_filters_inactive(mcp_db):
    await mcp_db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES "
        "('g1','测试群A','room_a@x'),('g2','测试群B','room_b@x')"
    )
    await mcp_db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id, is_active) VALUES "
        "('g3','停用群','room_c@x',0)"
    )
    await mcp_db.commit()
    result = await server.list_groups()
    assert {g["group_id"] for g in result} == {"g1", "g2"}


# ============================================================
# search_topics — LIKE 检索（FTS5 中文不可用的替代）
# ============================================================


async def test_search_topics_chinese_substring(mcp_db):
    """LIKE 命中中文子串 — FTS5 unicode61/trigram 做不到的核心场景。"""
    await _insert_topic(mcp_db, summary_id="s1", summary_text="讨论了因子选择方法")
    await _insert_topic(mcp_db, summary_id="s2", topic_name="Qlib 回测", summary_text="工程实践")
    await mcp_db.commit()

    r = await server.search_topics("因子")
    assert [x["summary_id"] for x in r] == ["s1"]
    r = await server.search_topics("回测")
    assert [x["summary_id"] for x in r] == ["s2"]


async def test_search_topics_english_case_insensitive(mcp_db):
    await _insert_topic(mcp_db, summary_id="s1", summary_text="Factor Zoo framework")
    await mcp_db.commit()
    r = await server.search_topics("factor")  # 小写查大写
    assert len(r) == 1


async def test_search_topics_filter_group_and_date(mcp_db):
    await _insert_topic(
        mcp_db, summary_id="s1", group_id="g_a", date="20260701", summary_text="x 因子"
    )
    await _insert_topic(
        mcp_db, summary_id="s2", group_id="g_b", date="20260719", summary_text="y 因子"
    )
    await mcp_db.commit()

    r = await server.search_topics("因子", group_id="g_a")
    assert {x["summary_id"] for x in r} == {"s1"}
    r = await server.search_topics("因子", date_from="20260715")
    assert {x["summary_id"] for x in r} == {"s2"}


# ============================================================
# get_topic — 详情 + 时间线 + 反馈
# ============================================================


async def test_get_topic_with_timeline_and_feedback(mcp_db):
    await _insert_topic(
        mcp_db,
        summary_id="s1",
        group_id="g1",
        topic_name="因子",
        date="20260719",
        participants="A,B",
        conclusion="可行",
    )
    await _insert_topic(
        mcp_db,
        summary_id="s2",
        group_id="g1",
        topic_name="因子",
        date="20260720",
        participants="A,C",  # timeline（同名跨天）
    )
    await mcp_db.execute(
        "INSERT INTO feedback_events (feedback_id, group_id, date, target_type, signal, "
        "target_topic_id) VALUES ('f1','g1','20260719','topic','correction','s1')"
    )
    await mcp_db.commit()

    r = await server.get_topic("s1")
    assert r["detail"]["summary_id"] == "s1"
    assert [t["summary_id"] for t in r["timeline"]] == ["s2"]
    assert [f["feedback_id"] for f in r["feedback"]] == ["f1"]


async def test_get_topic_not_found(mcp_db):
    r = await server.get_topic("nonexistent")
    assert "error" in r


# ============================================================
# get_daily_report — 版本解析
# ============================================================


async def test_get_daily_report_resolves_active_version(mcp_db, monkeypatch, tmp_path):
    await mcp_db.execute(
        "INSERT INTO report_versions (version_id, report_id, group_id, date, "
        "version_number, source, is_active) VALUES "
        "('g1-20260719-v1','g1-20260719','g1','20260719',1,'daily_run',0),"
        "('g1-20260719-v2','g1-20260719','g1','20260719',2,'daily_run',1)"
    )
    await mcp_db.commit()
    # active = v2，造 v2 JSON
    l3_dir = tmp_path / "g1" / "20260719" / "v2"
    l3_dir.mkdir(parents=True)
    (l3_dir / "daily.json").write_text(
        '{"date":"20260719","overview":"v2 active"}', encoding="utf-8"
    )
    from z_winnow.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "layer3_output_dir", str(tmp_path))

    r = await server.get_daily_report("g1", "20260719")
    assert "error" not in r
    assert r["version"] == 2  # 取 active 版本而非 v1
    assert r["content"]["overview"] == "v2 active"


async def test_get_daily_report_not_found(mcp_db):
    r = await server.get_daily_report("g1", "20260719")
    assert "error" in r


# ============================================================
# submit_feedback — 日期归一化 + 字段路由
# ============================================================


async def _seed_group(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES ('g1','测试','r@x')"
    )
    await db.commit()


async def test_submit_feedback_correction_routes_to_corrected_text(mcp_db):
    await _seed_group(mcp_db)
    r = await server.submit_feedback(
        group_id="g1",
        date="20260719",  # YYYYMMDD
        target_type="topic",
        signal="correction",
        content="结论应为 Factor Zoo 可行",
        target_topic_id="s1",
    )
    assert r["accepted"] is True

    cur = await mcp_db.execute(
        "SELECT date, signal, corrected_text, correction_note, correction_mode, target_topic_id "
        "FROM feedback_events WHERE feedback_id = ?",
        (r["feedback_id"],),
    )
    row = await cur.fetchone()
    assert row["date"] == "2026-07-19"  # 归一化 YYYYMMDD → YYYY-MM-DD
    assert row["corrected_text"] == "结论应为 Factor Zoo 可行"
    assert row["correction_note"] is None
    assert row["correction_mode"] == "free_text"
    assert row["target_topic_id"] == "s1"


async def test_submit_feedback_stale_routes_to_note(mcp_db):
    await _seed_group(mcp_db)
    r = await server.submit_feedback(
        group_id="g1",
        date="2026-07-20",  # 已是 YYYY-MM-DD
        target_type="topic",
        signal="stale",
        content="此结论已过时",
    )
    cur = await mcp_db.execute(
        "SELECT corrected_text, correction_note, correction_mode FROM feedback_events "
        "WHERE feedback_id = ?",
        (r["feedback_id"],),
    )
    row = await cur.fetchone()
    assert row["corrected_text"] is None
    assert row["correction_note"] == "此结论已过时"
    assert row["correction_mode"] is None


# ============================================================
# ECS 双库路由（阶段 2.3）— l3_snapshot (ro) + feedback_inbox (rw)
# ============================================================


async def _seed_l3_file(l3_path: Path, seed_fn) -> None:
    """用 rw 连接重建 l3_snapshot.db 内容，checkpoint 后关闭。

    模拟 sync push 的整库 ``.backup()`` 替换：先关旧 ro 连接 → 删旧文件（连同
    -wal/-shm）→ 新建库 + init schema + seed → ``wal_checkpoint(TRUNCATE)`` 把数据
    落进主 .db 文件。get_l3_db 用 ``mode=ro&immutable=1`` 打开时忽略 -wal，故必须
    checkpoint 确保 immutable ro 读得到。
    """
    # 先关可能持有旧文件的 ro 连接（避免删文件时 fd 仍开）
    if server._l3_conn is not None:
        await server._l3_conn.close()
        server._l3_conn = None
    server._l3_mtime = 0.0
    if l3_path.exists():
        l3_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = l3_path.with_name(l3_path.name + suffix)
        if side.exists():
            side.unlink()
    conn = await aiosqlite.connect(str(l3_path))
    conn.row_factory = aiosqlite.Row
    await init_database_in_conn(conn)
    await seed_fn(conn)
    await conn.commit()
    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await conn.close()


@pytest.fixture
async def mcp_ecs(monkeypatch, tmp_path):
    """ECS 双库模式：deployment_target=ecs + l3/inbox 指向 tmp 文件。

    l3_snapshot.db 预建空 schema（数据由测试经 _seed_l3_file 注入）；
    feedback_inbox.db 不预建 — 验 get_inbox_db 首次自动 init。
    """
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    l3_path = tmp_path / "l3_snapshot.db"
    inbox_path = tmp_path / "feedback_inbox.db"
    monkeypatch.setattr(settings, "deployment_target", "ecs")
    monkeypatch.setattr(settings, "l3_snapshot_path", str(l3_path))
    monkeypatch.setattr(settings, "feedback_inbox_path", str(inbox_path))

    # 预建空 l3 schema（get_l3_db 首次 ro 打开需要文件存在）；
    # _seed_l3_file 服务测试中的数据注入（契约：async seed_fn），预建不经过它。
    pre = await aiosqlite.connect(str(l3_path))
    pre.row_factory = aiosqlite.Row
    await init_database_in_conn(pre)
    await pre.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await pre.close()
    monkeypatch.setattr(server, "_l3_conn", None)
    monkeypatch.setattr(server, "_inbox_conn", None)
    monkeypatch.setattr(server, "_l3_mtime", 0.0)

    yield {"l3": l3_path, "inbox": inbox_path}

    if server._l3_conn is not None:
        await server._l3_conn.close()
        monkeypatch.setattr(server, "_l3_conn", None)
    if server._inbox_conn is not None:
        await server._inbox_conn.close()
        monkeypatch.setattr(server, "_inbox_conn", None)


async def test_ecs_read_uses_l3_snapshot(mcp_ecs):
    """ECS 读工具查 l3_snapshot（非 inbox）— seed l3 有数据即命中。"""

    async def seed(db):
        await db.execute(
            "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES ('g1','群','r@x')"
        )
        await _insert_topic(db, summary_id="s1", group_id="g1", summary_text="因子讨论")

    await _seed_l3_file(mcp_ecs["l3"], seed)

    groups = await server.list_groups()
    assert [g["group_id"] for g in groups] == ["g1"]
    r = await server.search_topics("因子")
    assert [x["summary_id"] for x in r] == ["s1"]


async def test_ecs_submit_feedback_writes_inbox_not_l3(mcp_ecs):
    """ECS submit_feedback 写 feedback_inbox.db；l3_snapshot.feedback_events 不受影响。"""

    async def seed(db):
        await db.execute(
            "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES ('g1','群','r@x')"
        )

    await _seed_l3_file(mcp_ecs["l3"], seed)

    r = await server.submit_feedback(
        group_id="g1",
        date="20260719",
        target_type="topic",
        signal="correction",
        content="应改为 Factor Zoo",
        target_topic_id="s1",
    )
    assert r["accepted"] is True

    inbox = await server.get_inbox_db()
    cur = await inbox.execute(
        "SELECT corrected_text FROM feedback_events WHERE feedback_id = ?",
        (r["feedback_id"],),
    )
    assert (await cur.fetchone())["corrected_text"] == "应改为 Factor Zoo"

    # l3_snapshot 是 ro 快照，ECS 写不污染它
    l3 = await server.get_l3_db()
    cur = await l3.execute(
        "SELECT COUNT(*) AS n FROM feedback_events WHERE feedback_id = ?",
        (r["feedback_id"],),
    )
    assert (await cur.fetchone())["n"] == 0


async def test_ecs_l3_full_replace_via_seed(mcp_ecs):
    """sync push = 整库替换（非增量）：v1 数据在 v2 push 后消失。"""

    async def seed_v1(db):
        await _insert_topic(db, summary_id="v1", summary_text="第一版数据")

    await _seed_l3_file(mcp_ecs["l3"], seed_v1)
    assert [x["summary_id"] for x in await server.search_topics("第一版")] == ["v1"]

    async def seed_v2(db):
        await _insert_topic(db, summary_id="v2", summary_text="第二版数据")

    await _seed_l3_file(mcp_ecs["l3"], seed_v2)  # 整库替换

    assert [x["summary_id"] for x in await server.search_topics("第二版")] == ["v2"]
    assert await server.search_topics("第一版") == []  # v1 已被替换掉


async def test_ecs_l3_reconnect_on_mtime_change(mcp_ecs):
    """生产关键：push 原子替换文件（mtime 变）→ get_l3_db 懒重连，无需重启容器。"""

    async def seed(db):
        await _insert_topic(db, summary_id="s1", summary_text="数据")

    await _seed_l3_file(mcp_ecs["l3"], seed)
    await server.search_topics("数据")  # 触发首次 ro 打开

    old_conn = server._l3_conn
    assert old_conn is not None

    # 模拟 sync push 原子替换：touch 把 mtime 推到未来（文件内容不变，单验重连触发）
    future = time.time() + 100
    os.utime(mcp_ecs["l3"], (future, future))

    await server.search_topics("数据")  # mtime 变 → 懒重连

    assert server._l3_mtime == future  # mtime 已更新
    assert server._l3_conn is not old_conn  # 连接被重开


async def test_ecs_inbox_auto_init_schema(mcp_ecs):
    """inbox 文件首次不存在 → get_inbox_db 自动建库 + feedback_events 表。"""
    assert not mcp_ecs["inbox"].exists()

    db = await server.get_inbox_db()

    assert mcp_ecs["inbox"].exists()
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_events'"
    )
    assert (await cur.fetchone()) is not None


async def test_ecs_l3_not_found_raises(monkeypatch, tmp_path):
    """ECS 模式 + l3 文件未 push → get_l3_db 抛 ToolError（提示 sync push）。"""
    from fastmcp.exceptions import ToolError

    from z_winnow.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "deployment_target", "ecs")
    monkeypatch.setattr(settings, "l3_snapshot_path", str(tmp_path / "nope.db"))
    monkeypatch.setattr(server, "_l3_conn", None)

    with pytest.raises(ToolError, match="sync push"):
        await server.get_l3_db()


# ============================================================
# key-based 权限过滤（contextvars 注入 MemberInfo）
# ============================================================


@pytest.fixture
def member_ctx():
    """注入非 admin MemberInfo 到 _current_member（模拟 http member key 调用）。

    allowed_groups={"g1"}：只能访问 g1；g2 越权。
    """
    member = MemberInfo("zhang_san", "张三", is_admin=False, allowed_groups={"g1"})
    token = server._current_member.set(member)
    yield member
    server._current_member.reset(token)


async def test_list_groups_member_filtered(mcp_db, member_ctx):
    """member key 只看到 allowed_groups 内的群。"""
    await mcp_db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES "
        "('g1','群A','r1'),('g2','群B','r2')"
    )
    await mcp_db.commit()
    result = await server.list_groups()
    assert {g["group_id"] for g in result} == {"g1"}  # 只 g1（白名单）


async def test_list_groups_admin_all(mcp_db):
    """无 member_ctx（_current_member=None）→ admin 兜底全权（所有群）。"""
    await mcp_db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES "
        "('g1','群A','r1'),('g2','群B','r2')"
    )
    await mcp_db.commit()
    result = await server.list_groups()
    assert {g["group_id"] for g in result} == {"g1", "g2"}  # 全权


async def test_search_topics_member_denied_group(mcp_db, member_ctx):
    """member 越权指定 group_id → ToolError。"""
    with pytest.raises(ToolError, match="无权"):
        await server.search_topics("x", group_id="g2")


async def test_search_topics_member_implicit_filter(mcp_db, member_ctx):
    """member 不传 group_id → 只搜 allowed 群的议题。"""
    await _insert_topic(mcp_db, summary_id="s1", group_id="g1", summary_text="因子")
    await _insert_topic(mcp_db, summary_id="s2", group_id="g2", summary_text="因子")
    await mcp_db.commit()
    r = await server.search_topics("因子")
    assert {x["summary_id"] for x in r} == {"s1"}  # 只 g1


async def test_get_topic_member_denied(mcp_db, member_ctx):
    """member get_topic 越权群 → ToolError。"""
    await _insert_topic(mcp_db, summary_id="s1", group_id="g2", summary_text="x")
    await mcp_db.commit()
    with pytest.raises(ToolError, match="无权"):
        await server.get_topic("s1")


async def test_get_daily_report_member_denied(mcp_db, member_ctx):
    with pytest.raises(ToolError, match="无权"):
        await server.get_daily_report("g2", "20260719")


async def test_submit_feedback_reporter_is_member_id(mcp_db, member_ctx):
    """member 提反馈 → reporter = member_id（g1 在 allowed 内）。"""
    await mcp_db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES ('g1','群','r@x')"
    )
    await mcp_db.commit()
    r = await server.submit_feedback(
        group_id="g1",
        date="20260719",
        target_type="topic",
        signal="correction",
        content="改",
        target_topic_id="s1",
    )
    assert r["accepted"] is True
    cur = await mcp_db.execute(
        "SELECT reporter FROM feedback_events WHERE feedback_id = ?", (r["feedback_id"],)
    )
    assert (await cur.fetchone())["reporter"] == "zhang_san"


async def test_submit_feedback_member_denied(mcp_db, member_ctx):
    """member 对越权群提反馈 → ToolError。"""
    with pytest.raises(ToolError, match="无权"):
        await server.submit_feedback(
            group_id="g2",
            date="20260719",
            target_type="topic",
            signal="correction",
            content="x",
        )
