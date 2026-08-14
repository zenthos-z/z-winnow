"""CLI sync 子命令测试（阶段 2.2）。

验 ``build_parser`` 解析 + ``_cmd_sync_dispatch`` 路由 + ``_cmd_sync_*`` 输出格式。
sync 核心逻辑（backup/merge/status）由 :mod:`tests.test_sync` 覆盖，这里只测 CLI 薄包装。

mock ``z_winnow.sync.push/pull/status``（``__init__`` 导出的函数引用），
``_cmd_sync_*`` 内惰性 ``from z_winnow.sync import push`` 每次调用重新绑定 →
monkeypatch 后下次调用拿到 fake。
"""

from __future__ import annotations

import argparse

from z_winnow import cli


def _parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


# ============================================================
# 参数解析
# ============================================================


def test_parse_sync_push_flags():
    a = _parse("sync", "push", "--dry-run", "--no-processed")
    assert a.command == "sync"
    assert a.sync_action == "push"
    assert a.dry_run is True
    assert a.no_processed is True


def test_parse_sync_push_defaults():
    a = _parse("sync", "push")
    assert a.dry_run is False
    assert a.no_processed is False  # 默认含 processed


def test_parse_sync_pull_dry_run():
    a = _parse("sync", "pull", "--dry-run")
    assert a.sync_action == "pull"
    assert a.dry_run is True


def test_parse_sync_status():
    a = _parse("sync", "status")
    assert a.sync_action == "status"


# ============================================================
# _cmd_sync_push
# ============================================================


async def test_cmd_sync_push_output(capsys, monkeypatch):
    async def fake_push(*, dry_run=False, include_processed=True):
        return {
            "snapshot_bytes": 2048,
            "processed_synced": True,
            "remote_snapshot_path": "/data/l3_snapshot.db",
            "dry_run": False,
        }

    monkeypatch.setattr("z_winnow.sync.push", fake_push)
    rc = await cli._cmd_sync_push(_parse("sync", "push"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2.0 KB" in out  # 2048/1024
    assert "/data/l3_snapshot.db" in out
    assert "processed JSON 同步: 是" in out


async def test_cmd_sync_push_failure_exit1(capsys, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("ssh timeout")

    monkeypatch.setattr("z_winnow.sync.push", boom)
    rc = await cli._cmd_sync_push(_parse("sync", "push"))
    assert rc == 1
    assert "push 失败" in capsys.readouterr().err


# ============================================================
# _cmd_sync_pull
# ============================================================


async def test_cmd_sync_pull_output(capsys, monkeypatch):
    async def fake_pull(*, dry_run=False):
        return {"pulled": 3, "cleared": "CLEARED 3", "dry_run": False}

    monkeypatch.setattr("z_winnow.sync.pull", fake_pull)
    rc = await cli._cmd_sync_pull(_parse("sync", "pull"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 条反馈" in out


async def test_cmd_sync_pull_dry_run_output(capsys, monkeypatch):
    async def fake_pull(*, dry_run=False):
        return {"pulled": 1, "cleared": False, "dry_run": True}

    monkeypatch.setattr("z_winnow.sync.pull", fake_pull)
    rc = await cli._cmd_sync_pull(_parse("sync", "pull", "--dry-run"))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out


# ============================================================
# _cmd_sync_status
# ============================================================


async def test_cmd_sync_status_output(capsys, monkeypatch):
    async def fake_status():
        return {
            "local": {
                "groups": 2,
                "topic_summaries": 10,
                "report_versions": 5,
                "feedback_events": 1,
            },
            "ecs_l3": {
                "groups": "2",
                "topic_summaries": "10",
                "report_versions": "5",
                "feedback_events": "0",
            },
            "ecs_inbox": {"feedback_events": "4"},
            "inbox_pending_pull": 4,
        }

    monkeypatch.setattr("z_winnow.sync.status", fake_status)
    rc = await cli._cmd_sync_status(_parse("sync", "status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "本地" in out and "ECS" in out
    assert "待 pull: 4" in out
    assert "winnow sync pull" in out  # 提示运行 pull


async def test_cmd_sync_status_l3_not_pushed(capsys, monkeypatch):
    async def fake_status():
        return {
            "local": {
                "groups": 1,
                "topic_summaries": 0,
                "report_versions": 0,
                "feedback_events": 0,
            },
            "ecs_l3": "NOT_EXISTS",
            "ecs_inbox": "NOT_EXISTS",
            "inbox_pending_pull": 0,
        }

    monkeypatch.setattr("z_winnow.sync.status", fake_status)
    rc = await cli._cmd_sync_status(_parse("sync", "status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "尚未 push" in out


# ============================================================
# _cmd_sync_dispatch 路由
# ============================================================


async def test_dispatch_push_routes_with_flags(monkeypatch):
    called: dict = {}

    async def fake_push(*, dry_run=False, include_processed=True):
        called["kwargs"] = {"dry_run": dry_run, "include_processed": include_processed}
        return {
            "snapshot_bytes": 0,
            "processed_synced": False,
            "remote_snapshot_path": "",
            "dry_run": dry_run,
        }

    monkeypatch.setattr("z_winnow.sync.push", fake_push)
    await cli._cmd_sync_dispatch(_parse("sync", "push", "--dry-run", "--no-processed"))
    assert called["kwargs"] == {"dry_run": True, "include_processed": False}
