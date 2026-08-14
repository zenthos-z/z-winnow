"""Tests for the Feishu Bitable upload pipeline.

Covers the lark-cli wrapper (mock dispatch), Layer-3 → row schema mappers, the
uploader (framework init + day upload), and the group-service init endpoint.
All tests run in ``LARK_CLI_MOCK=1`` — no real lark-cli subprocess or Feishu I/O.

The mock dispatcher mirrors the real lark-cli response envelopes
(``data.base.base_token`` / ``data.table.id``), so these tests exercise the same
extraction + mapping code paths that run against real Feishu.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from z_winnow.pipeline.feishu import lark_cli, schema, uploader

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_lark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable lark-cli mock mode and reset mock state between tests."""
    monkeypatch.setenv("LARK_CLI_MOCK", "1")
    lark_cli._MOCK_STATE.clear()


@pytest.fixture
async def temp_db():
    """An initialized temp SQLite DB with one registered group."""
    from z_winnow.pipeline.database import init_database_in_conn

    path = tempfile.mktemp(suffix=".db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, output_dir) VALUES (?, ?, ?, ?)",
        ("g_test", "测试群", "room@test", ""),
    )
    await db.commit()
    yield db
    await db.close()
    if os.path.exists(path):
        os.unlink(path)


# ============================================================
# Sample Layer-3 data
# ============================================================

SAMPLE_DAILY = {
    "date": "20260709",
    "overview": "今日围绕 CAD 效率讨论",
    "important_notice": "",
    "trend_summary": "2 个核心议题",
    "topics": [
        {
            "topic_name": "CAD 曲线转圆弧批量",
            "lifecycle": "emerging",
            "status": "active",
            "background": "背景",
            "process": "进展",
            "conclusion": "结论",
            "trend": "趋势",
            "participants": ["张蓓蕾", "阿Q"],
            "weight": 0.8,
        }
    ],
}
SAMPLE_RESOURCES = {
    "date": "20260709",
    "resources": [
        {
            "resource_title": "工具X",
            "resource_type": "repo",
            "summary": "简介",
            "content": "https://x",
            "shared_by": "阿Q",
        },
        {
            "resource_title": "世界模型综述论文",
            "resource_type": "paper",
            "summary": "一篇综述",
            "content": "https://arxiv.org/xxx",
            "shared_by": "李明",
        },
    ],
}
SAMPLE_ENGINEERING = {
    "date": "20260709",
    "engineering_issues": [
        {
            "datetime": "2026-07-09 11:08",
            "group": "开发与调试工具",
            "description": "只能单选",
            "solution": "待优化",
            "status": "⚠️",
            "status_desc": "待解决",
            "source_members": "张蓓蕾, 阿Q",
            "key_operations": "测试多选",
        }
    ],
}


# ============================================================
# Helpers
# ============================================================


def _write_sample_l3(root: Path, date: str = "20260709") -> Path:
    """Write SAMPLE_DAILY/RESOURCES/ENGINEERING as ``root/{date}/*.json``.

    Self-contained L3 fixture so push tests don't depend on runtime data that
    isn't checked into the repo. Returns ``root`` (the group's output_dir).
    """
    d = root / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "daily.json").write_text(json.dumps(SAMPLE_DAILY), encoding="utf-8")
    (d / "resources.json").write_text(json.dumps(SAMPLE_RESOURCES), encoding="utf-8")
    (d / "engineering.json").write_text(json.dumps(SAMPLE_ENGINEERING), encoding="utf-8")
    return root


# ============================================================
# Schema mappers
# ============================================================


def test_topic_detail_rows_maps_labels_and_date(mock_lark: None) -> None:
    cols, rows = schema.topic_detail_rows(SAMPLE_DAILY, "20260709")
    assert cols == [
        "日期",
        "议题名称",
        "生命周期",
        "状态",
        "结论",
        "趋势",
        "参与人",
        "权重",
        "背景",
        "进展",
    ]
    assert rows[0][0] == "2026-07-09 00:00:00"  # datetime CellValue
    assert rows[0][1] == "CAD 曲线转圆弧批量"
    assert rows[0][2] == "新兴"  # lifecycle emerging → 新兴
    assert rows[0][3] == "进行中"  # status active → 进行中
    assert rows[0][4] == "结论"  # conclusion
    assert rows[0][6] == "张蓓蕾, 阿Q"  # participants joined
    assert rows[0][7] == 0.8  # weight as float
    assert rows[0][8] == "背景"  # 长文本列移到末尾
    assert rows[0][9] == "进展"


def test_render_feishu_daily_table_not_broken_by_details(mock_lark: None) -> None:
    """feishu_daily.j2: 议题表格必须连续——背景/进展/趋势不能以 blockquote 夹在表格行
    之间打断飞书解析（回归 #10）；详情移到表格下方。"""
    from z_winnow.templates import render_feishu_daily

    md = render_feishu_daily(SAMPLE_DAILY)
    lines = md.splitlines()

    # 从表头起，连续 `|` 行应 = 表头 + 分隔 + 议题数（中间无 blockquote 打断）。
    hdr = next(i for i, ln in enumerate(lines) if ln.startswith("| 议题"))
    pipe_run = 0
    for ln in lines[hdr:]:
        if ln.startswith("|"):
            pipe_run += 1
        else:
            break
    assert pipe_run == 2 + len(SAMPLE_DAILY["topics"])

    # 旧式 blockquote 详情已移除；背景/进展/趋势落到表格下方。
    assert "> 背景" not in md
    assert "**背景**：背景" in md
    assert "**进展**：进展" in md
    assert "**趋势**：趋势" in md


def test_daily_summary_one_row_per_day(mock_lark: None) -> None:
    cols, rows = schema.daily_summary_rows(SAMPLE_DAILY, "20260709")
    assert cols == ["日期", "概述", "重点提醒", "趋势总结", "议题数"]
    assert len(rows) == 1
    assert rows[0][0] == "2026-07-09 00:00:00"
    assert rows[0][4] == 1.0  # topic count


def test_resource_rows_multiselect_tags(mock_lark: None) -> None:
    cols, rows = schema.resource_rows(SAMPLE_RESOURCES, "20260709")
    assert cols == ["发布日期", "资源标题", "标签", "简介", "具体内容", "分享人"]
    # row[0]: repo → 工具
    assert rows[0][1] == "工具X"  # resource_title
    assert rows[0][2] == ["工具"]  # repo → 工具
    assert rows[0][5] == "阿Q"  # shared_by
    # row[1]: paper → 文档
    assert rows[1][1] == "世界模型综述论文"
    assert rows[1][2] == ["文档"]  # paper → 文档
    assert rows[1][5] == "李明"


def test_engineering_rows_status_mapping(mock_lark: None) -> None:
    cols, rows = schema.engineering_rows(SAMPLE_ENGINEERING, "20260709")
    assert "状态" in cols and "状态描述" in cols
    status_idx = cols.index("状态")
    assert rows[0][status_idx] == "待解决"  # status_desc → select label


# ============================================================
# Uploader: framework init
# ============================================================

# Per-group tables_config blobs: engineering is the only optional kind today.
ENG_ON = {"engineering": {"enabled": True}}
ENG_OFF = {"engineering": {"enabled": False}}


def _tid(fw: dict, kind: str) -> str:
    """Extract a kind's table_id from an ensure_framework result blob."""
    return (fw["tables_config"].get(kind) or {}).get("table_id", "")


async def test_ensure_framework_creates_base_and_four_tables(mock_lark: None) -> None:
    fw = await uploader.ensure_framework(base_name="测试群", tables_config=ENG_ON)
    assert fw["status"] == "ok"
    assert fw["created"] is True
    assert fw["base_token"]
    for kind in ("summary", "topics", "resources", "engineering"):
        assert _tid(fw, kind), f"missing table_id for {kind}"


async def test_ensure_framework_idempotent_skip(mock_lark: None) -> None:
    fw1 = await uploader.ensure_framework(base_name="X", tables_config=ENG_ON)
    fw2 = await uploader.ensure_framework(
        base_name="X",
        tables_config=fw1["tables_config"],
        base_token=fw1["base_token"],
    )
    assert fw2["status"] == "skipped"
    assert fw2["created"] is False
    assert fw2["base_token"] == fw1["base_token"]


async def test_ensure_framework_engineering_off_omits_table(mock_lark: None) -> None:
    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_OFF)
    assert fw["status"] == "ok"
    # engineering disabled → not created; mandatory 3 still present.
    assert not _tid(fw, "engineering")
    assert all(_tid(fw, k) for k in ("summary", "topics", "resources"))


# ============================================================
# Uploader: day upload
# ============================================================


async def test_upload_group_day_writes_all_tables(mock_lark: None) -> None:
    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_ON)
    l3 = {"daily": SAMPLE_DAILY, "resources": SAMPLE_RESOURCES, "engineering": SAMPLE_ENGINEERING}
    up = await uploader.upload_group_day(
        base_token=fw["base_token"],
        tables_config=fw["tables_config"],
        l3_data=l3,
        date="20260709",
    )
    assert up["status"] == "ok"
    assert up["counts"] == {"summary": 1, "topics": 1, "resources": 2, "engineering": 1}
    assert up["rows_total"] == 5
    assert up["errors"] == []


async def test_upload_group_day_skips_engineering_when_disabled(mock_lark: None) -> None:
    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_OFF)
    l3 = {"daily": SAMPLE_DAILY, "resources": SAMPLE_RESOURCES, "engineering": SAMPLE_ENGINEERING}
    up = await uploader.upload_group_day(
        base_token=fw["base_token"],
        tables_config=fw["tables_config"],
        l3_data=l3,
        date="20260709",
    )
    assert "engineering" not in up["counts"]
    assert up["rows_total"] == 4  # summary + topics + resources(2)


async def test_upload_attaches_daily_markdown_to_summary(
    mock_lark: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After creating the summary row, the rendered daily MD is attached to 日报文档."""
    captured: list[tuple[str, str, str]] = []

    async def spy(
        base_token: str, table_id: str, record_id: str, field: str, file_path: str, **kw: object
    ) -> dict[str, object]:
        captured.append((record_id, field, file_path))
        return {"ok": True}

    monkeypatch.setattr(lark_cli, "record_upload_attachment", spy)

    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_ON)
    await uploader.upload_group_day(
        base_token=fw["base_token"],
        tables_config=fw["tables_config"],
        l3_data={"daily": SAMPLE_DAILY},
        date="20260709",
    )
    assert len(captured) == 1
    record_id, field, path = captured[0]
    assert field == "日报文档"
    assert path.endswith(".md")
    assert record_id  # record_id from batch-create


async def test_upload_attaches_cover_image_when_enabled(
    mock_lark: None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#9.2: image_gen_enabled=True 且 cover.png 存在 → 挂「图片」字段。"""
    from z_winnow.config.settings import reset_settings

    group_id = "g_cover"
    date = "20260709"
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", str(tmp_path / "processed"))
    reset_settings()

    # 预置一张配图（模拟 gen-image 已生成）
    cover_dir = tmp_path / "processed" / group_id / date
    cover_dir.mkdir(parents=True)
    (cover_dir / "cover.png").write_bytes(b"\x89PNG fake cover")

    captured: list[str] = []

    async def spy(
        base_token: str, table_id: str, record_id: str, field: str, file_path: str, **kw: object
    ) -> dict[str, object]:
        captured.append(field)
        return {"ok": True}

    monkeypatch.setattr(lark_cli, "record_upload_attachment", spy)

    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_ON)
    await uploader.upload_group_day(
        base_token=fw["base_token"],
        tables_config=fw["tables_config"],
        l3_data={"daily": SAMPLE_DAILY},
        date=date,
        group_id=group_id,
    )
    assert "日报文档" in captured  # MD 始终挂
    assert "图片" in captured  # cover 也挂了


async def test_upload_skips_cover_image_when_absent(
    mock_lark: None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#9.2: image_gen_enabled=True 但无 cover.png → 跳过「图片」（MD 仍挂）。"""
    from z_winnow.config.settings import reset_settings

    monkeypatch.setenv("WINNOW_IMAGE_GEN_ENABLED", "true")
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", str(tmp_path / "processed"))
    reset_settings()

    captured: list[str] = []

    async def spy(
        base_token: str, table_id: str, record_id: str, field: str, file_path: str, **kw: object
    ) -> dict[str, object]:
        captured.append(field)
        return {"ok": True}

    monkeypatch.setattr(lark_cli, "record_upload_attachment", spy)

    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_ON)
    await uploader.upload_group_day(
        base_token=fw["base_token"],
        tables_config=fw["tables_config"],
        l3_data={"daily": SAMPLE_DAILY},
        date="20260709",
        group_id="g_nocover",  # 无 cover.png
    )
    assert "日报文档" in captured
    assert "图片" not in captured  # 无图，跳过


# ============================================================
# Group service: init endpoint
# ============================================================


async def test_init_group_feishu_framework_persists(
    mock_lark: None, temp_db: aiosqlite.Connection
) -> None:
    from z_winnow.web.services.group_service import init_group_feishu_framework

    g = await init_group_feishu_framework(
        temp_db, "g_test", base_target=""
    )
    assert g.feishu_base_token
    assert g.feishu_framework_initialized == 1
    assert g.feishu_engineering_enabled == 1
    assert all(
        getattr(g, f"feishu_table_{k}") for k in ("summary", "topics", "resources", "engineering")
    )


async def test_init_group_feishu_framework_unknown_group_raises(
    mock_lark: None, temp_db: aiosqlite.Connection
) -> None:
    from z_winnow.web.services.group_service import init_group_feishu_framework

    with pytest.raises(RuntimeError, match="not found"):
        await init_group_feishu_framework(temp_db, "nope", base_target="")


# ============================================================
# Full push coroutine (report_service → uploader)
# ============================================================


async def test_feishu_push_coro_end_to_end(
    mock_lark: None, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """White-box: _feishu_push_coro builds framework + uploads L3 from disk."""
    from z_winnow.web.services.report_service import _feishu_push_coro

    # Point output_dir at a self-contained sample L3 written under tmp_path.
    l3_dir = str(_write_sample_l3(tmp_path / "l3"))

    # Stand up a temp DB + group with feishu enabled, pointing at the sample L3.
    from z_winnow.pipeline.database import init_database_in_conn

    path = tempfile.mktemp(suffix=".db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, feishu_enabled, output_dir) "
        "VALUES (?, ?, ?, 1, ?)",
        ("g_real", "真实群", "r@x", l3_dir),
    )
    await db.commit()
    await db.close()

    try:
        result = await _feishu_push_coro(
            report_id="rep1", group_id="g_real", date="20260709", db_path=path
        )
    finally:
        if os.path.exists(path):
            os.unlink(path)

    assert result["status"] == "uploaded"
    assert result["rows_count"] >= 1


async def test_upload_attaches_resource_files(
    mock_lark: None, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """#9.3: resources 带 local_path 的行上传「附件」字段; 纯链接不上传。"""
    captured: list[tuple[str, str, str]] = []  # (record_id, field, file_name)

    async def spy(
        base_token: str,
        table_id: str,
        record_id: str,
        field: str,
        file_name: str,
        **kw: object,
    ) -> dict[str, object]:
        captured.append((record_id, field, file_name))
        return {"ok": True}

    monkeypatch.setattr(lark_cli, "record_upload_attachment", spy)

    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    fw = await uploader.ensure_framework(base_name="X", tables_config=ENG_ON)
    l3 = {
        "resources": {
            "date": "20260709",
            "resources": [
                {"title": "链接资源", "tags": ["链接"], "content": "https://x", "sharer": "阿Q"},
                {
                    "title": "文件资源",
                    "tags": ["文档"],
                    "content": "",
                    "sharer": "张三",
                    "local_path": str(pdf),
                },
            ],
        }
    }
    await uploader.upload_group_day(
        base_token=fw["base_token"],
        tables_config=fw["tables_config"],
        l3_data=l3,
        date="20260709",
    )
    # 只有第2条(有 local_path 且文件存在)触发附件上传; 第1条纯链接不上传
    assert len(captured) == 1
    assert captured[0][1] == "附件"
    assert captured[0][2] == "spec.pdf"


# ============================================================
# auto_push_after_run — run-completion auto-upload gate
# ============================================================


async def test_auto_push_after_run_skips_when_feishu_disabled(
    mock_lark: None, tmp_path
) -> None:
    """feishu_enabled=0 → 返回 None，不排队任何后台上传任务。"""
    from z_winnow.pipeline.database import init_database_in_conn
    from z_winnow.web.services.report_service import auto_push_after_run

    path = str(tmp_path / "t.db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, feishu_enabled) "
        "VALUES (?, ?, ?, 0)",
        ("g_test", "测试群", "room@test"),
    )
    await db.commit()
    await db.close()

    task_id = await auto_push_after_run("g_test", "20260709", db_path=path)
    assert task_id is None

    # 确认没排队上传任务
    async with aiosqlite.connect(path) as chk:
        cur = await chk.execute("SELECT COUNT(*) FROM async_tasks")
        n = (await cur.fetchone())[0]
    assert n == 0


async def test_auto_push_after_run_normalizes_hyphenated_date(
    mock_lark: None, tmp_path
) -> None:
    """调用方传 YYYY-MM-DD，内部归一化到 YYYYMMDD，不报错（disabled 群返回 None）。"""
    from z_winnow.pipeline.database import init_database_in_conn
    from z_winnow.web.services.report_service import auto_push_after_run

    path = str(tmp_path / "t.db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, feishu_enabled) "
        "VALUES (?, ?, ?, 0)",
        ("g_test", "测试群", "room@test"),
    )
    await db.commit()
    await db.close()

    assert await auto_push_after_run("g_test", "2026-07-09", db_path=path) is None
    assert await auto_push_after_run("g_test", "20260709", db_path=path) is None


async def test_auto_push_after_run_unknown_group_no_raise(
    mock_lark: None, tmp_path
) -> None:
    """未知 group_id → 返回 None，绝不抛（保护管线调用方）。"""
    from z_winnow.pipeline.database import init_database_in_conn
    from z_winnow.web.services.report_service import auto_push_after_run

    path = str(tmp_path / "t.db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.commit()
    await db.close()

    assert await auto_push_after_run("nonexistent", "2026-07-09", db_path=path) is None


async def test_auto_push_after_run_enabled_schedules_and_persists(
    mock_lark: None, tmp_path
) -> None:
    """feishu_enabled=1 → 排队后台上传任务，成功后写 report_versions.feishu_pushed_at。"""
    from z_winnow.pipeline.database import init_database_in_conn
    from z_winnow.web.services.report_service import auto_push_after_run
    from z_winnow.web.services.task_queue import get_task_status

    l3_dir = str(_write_sample_l3(tmp_path / "l3"))
    path = str(tmp_path / "t.db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, feishu_enabled, output_dir) "
        "VALUES (?, ?, ?, 1, ?)",
        ("g_real", "真实群", "r@x", l3_dir),
    )
    await db.execute(
        "INSERT INTO report_versions(version_id, report_id, group_id, date, "
        "version_number, source) VALUES (?, ?, ?, ?, ?, ?)",
        ("g_real-20260709-v1", "g_real-20260709", "g_real", "20260709", 1, "daily_run"),
    )
    await db.commit()
    await db.close()

    # 带连字符日期：验证内部归一化到 20260709 后能匹配到 report_versions 行
    task_id = await auto_push_after_run("g_real", "2026-07-09", db_path=path)
    assert task_id, "feishu 开启时应返回 task_id"

    # 轮询后台任务至终态（fire-and-forget 任务跑在同一事件循环上）
    status = None
    st = None
    for _ in range(50):  # ~5s 上限
        st = await get_task_status(task_id, db_path=path)
        if st and st["status"] in ("done", "failed"):
            status = st["status"]
            break
        await asyncio.sleep(0.1)
    assert status == "done", f"后台飞书推送未成功完成: {st}"

    async with aiosqlite.connect(path) as chk:
        cur = await chk.execute(
            "SELECT feishu_pushed_at FROM report_versions "
            "WHERE group_id='g_real' AND date='20260709'"
        )
        row = await cur.fetchone()
    assert row and row[0], "成功推送后 feishu_pushed_at 应被写入"


async def test_overview_surfaces_feishu_pushed_at(tmp_path) -> None:
    """overview 应返回最新日报的 feishu_pushed_at（供 index.html 按钮反映状态）。"""
    from z_winnow.pipeline.database import init_database_in_conn
    from z_winnow.web.services.overview_service import get_dashboard_summary

    path = str(tmp_path / "t.db")
    db = await aiosqlite.connect(path)
    await init_database_in_conn(db)
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, is_active) "
        "VALUES (?, ?, ?, 1)",
        ("g_ov", "总览群", "room@ov"),
    )
    await db.execute(
        "INSERT INTO report_versions(version_id, report_id, group_id, date, "
        "version_number, source, feishu_pushed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "g_ov-20260709-v1",
            "g_ov-20260709",
            "g_ov",
            "20260709",
            1,
            "daily_run",
            "2026-07-09T10:00:00Z",
        ),
    )
    await db.commit()

    summary = await get_dashboard_summary(db)
    await db.close()

    match = [g for g in summary.groups if g.group_id == "g_ov"]
    assert match, "g_ov 应出现在 overview groups 里"
    assert match[0].feishu_pushed_at == "2026-07-09T10:00:00Z"


def test_attachment_upload_timeout_scales_with_size(tmp_path) -> None:
    """大附件按文件大小放宽 lark-cli 超时（修复 100MB+ PDF 在 90s 被杀、永远推不上去）。

    文件 >20MB 走 lark-cli 分片上传，慢链路下 131MB 要好几分钟；run() 默认 90s
    会中途杀掉进程。helper 按大小（保守 ~200KB/s）放宽，小文件走 floor，超大走 cap。
    """
    from z_winnow.pipeline.feishu.uploader import (
        _ATTACHMENT_TIMEOUT_CAP_S,
        _ATTACHMENT_TIMEOUT_FLOOR_S,
        _attachment_upload_timeout,
    )

    # 小文件 → floor
    small = tmp_path / "small.pdf"
    small.write_bytes(b"%PDF-1.4 tiny")
    assert _attachment_upload_timeout(small) == _ATTACHMENT_TIMEOUT_FLOOR_S

    # 131MB 书（sparse，不占真实磁盘）→ 介于 floor 与 cap 之间，且明显 > 默认 90s
    big = tmp_path / "book.pdf"
    with big.open("wb") as f:
        f.truncate(131 * 1024 * 1024)
    t = _attachment_upload_timeout(big)
    assert _ATTACHMENT_TIMEOUT_FLOOR_S < t < _ATTACHMENT_TIMEOUT_CAP_S
    assert t > 600  # 131MB 应给到 10 分钟以上

    # 超大文件 → 触顶
    huge = tmp_path / "huge.pdf"
    with huge.open("wb") as f:
        f.truncate(5 * 1024 * 1024 * 1024)
    assert _attachment_upload_timeout(huge) == _ATTACHMENT_TIMEOUT_CAP_S

    # 文件不存在 → 不崩，回退 floor
    assert _attachment_upload_timeout(tmp_path / "nope.pdf") == _ATTACHMENT_TIMEOUT_FLOOR_S

