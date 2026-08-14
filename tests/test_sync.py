"""sync 模块测试（阶段 2.1）。

mock transport（run_rsync/run_ssh）不真连 ECS；验真实 backup 一致性 / merge 去重 /
清 inbox 时机 / no-such-file 容忍 / dry-run / status 比对逻辑。

push.py / pull.py / status.py 都 ``from .transport import run_rsync, run_ssh`` —— 各
模块持有自己的引用，故 monkeypatch 对应模块的引用即可（不需 patch transport 本身）。
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from z_winnow.config.settings import get_settings
from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.sync import transport
from z_winnow.sync.transport import CmdResult, SyncConfigError, check_config

# 子模块名与 __init__ 导出的函数同名（push/pull/status），``import ... as`` 拿到的会是
# 函数而非模块；用 importlib.import_module 取 sys.modules 的模块对象绕过属性重绑。
push_mod = importlib.import_module("z_winnow.sync.push")
pull_mod = importlib.import_module("z_winnow.sync.pull")
status_mod = importlib.import_module("z_winnow.sync.status")


@pytest.fixture
async def sync_settings(monkeypatch, tmp_path):
    """配置 settings 指向 tmp + ECS 假地址（满足 check_config）。"""
    s = get_settings()
    monkeypatch.setattr(s, "db_path", str(tmp_path / "main.db"))
    monkeypatch.setattr(s, "l3_snapshot_path", str(tmp_path / "l3_snapshot.db"))
    monkeypatch.setattr(s, "feedback_inbox_path", str(tmp_path / "feedback_inbox.db"))
    proc = tmp_path / "processed"
    proc.mkdir()
    monkeypatch.setattr(s, "layer3_output_dir", str(proc))
    monkeypatch.setattr(s, "ecs_ssh_host", "fake-host")
    monkeypatch.setattr(s, "ecs_ssh_user", "root")
    monkeypatch.setattr(s, "ecs_ssh_key", "/fake/key")
    monkeypatch.setattr(s, "ecs_data_dir", str(tmp_path / "ecs_data"))
    monkeypatch.setattr(s, "ecs_container_name", "fake-ctr")
    monkeypatch.setattr(s, "mcp_keys_path", str(tmp_path / "mcp_keys.yaml"))
    return s


def _ok(output: str = "") -> CmdResult:
    return CmdResult(returncode=0, output=output)


async def _build_main(settings) -> None:
    """建本地主库 schema + seed 一行 group/topic/feedback。"""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await init_database_in_conn(db)
        await db.execute(
            "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES ('g1','群','r@x')"
        )
        await db.execute(
            "INSERT INTO topic_summaries (summary_id, date, group_id, topic_name, "
            "topic_id, summary_text, context_ids, source_server_ids) "
            "VALUES ('s1','20260719','g1','议题','','内容','[]','[]')"
        )
        await db.execute(
            "INSERT INTO feedback_events (feedback_id, group_id, date, target_type, signal) "
            "VALUES ('fb-local','g1','20260719','topic','approval')"
        )
        await db.commit()


async def _make_inbox_db(path: Path, feedback_ids: list[str]) -> None:
    """造 inbox db（同 init schema + 指定 feedback 行，模拟 ECS rsync 来源）。"""
    async with aiosqlite.connect(str(path)) as db:
        await init_database_in_conn(db)
        for fid in feedback_ids:
            await db.execute(
                "INSERT INTO feedback_events (feedback_id, group_id, date, target_type, signal) "
                f"VALUES ('{fid}','g1','20260719','topic','correction')"
            )
        await db.commit()


# ============================================================
# transport — check_config
# ============================================================


async def test_check_config_raises_when_unconfigured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ecs_ssh_host", "")
    monkeypatch.setattr(s, "ecs_ssh_key", "")
    with pytest.raises(SyncConfigError, match="未配置"):
        check_config(s)


# ============================================================
# push
# ============================================================


async def test_push_backup_matches_main(sync_settings, monkeypatch):
    """push 生成的 l3_snapshot 是主库一致快照（表行数一致）+ 触发 rsync/mv。"""
    await _build_main(sync_settings)
    calls: list[tuple] = []

    async def fake_rsync(args):
        calls.append(("rsync", args))
        return _ok()

    async def fake_ssh(settings, cmd):
        calls.append(("ssh", cmd))
        return _ok()

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    r = await push_mod.push(include_processed=False)

    snapshot = Path(sync_settings.l3_snapshot_path)
    assert snapshot.exists()
    snap = sqlite3.connect(snapshot)
    assert snap.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1
    assert snap.execute("SELECT COUNT(*) FROM topic_summaries").fetchone()[0] == 1
    assert snap.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 1
    snap.close()
    assert r["processed_synced"] is False
    # rsync 传 snapshot + ssh 做了 mv（原子替换）
    assert any("l3_snapshot.db" in str(c) for c in calls)
    assert any(c[0] == "ssh" and "mv -f" in c[1] for c in calls)


async def test_push_dry_run_skips_transport(sync_settings, monkeypatch):
    await _build_main(sync_settings)

    async def fail(*a, **k):
        raise AssertionError("dry-run 不应调 transport")

    monkeypatch.setattr(transport, "run_rsync", fail)
    monkeypatch.setattr(transport, "run_ssh", fail)

    r = await push_mod.push(dry_run=True)

    assert r["dry_run"] is True
    assert Path(sync_settings.l3_snapshot_path).exists()  # snapshot 仍本地生成


async def test_push_raises_on_rsync_failure(sync_settings, monkeypatch):
    await _build_main(sync_settings)

    async def fail_rsync(args):
        return CmdResult(returncode=23, output="rsync error")

    async def ok_ssh(settings, cmd):
        return _ok()

    monkeypatch.setattr(transport, "run_rsync", fail_rsync)
    monkeypatch.setattr(transport, "run_ssh", ok_ssh)

    with pytest.raises(RuntimeError, match="rsync l3_snapshot"):
        await push_mod.push(include_processed=False)


async def test_push_syncs_processed_with_delete(sync_settings, monkeypatch):
    """include_processed=True 时 processed/ 走 rsync --delete 镜像。"""
    await _build_main(sync_settings)
    rsync_calls: list[list] = []

    async def fake_rsync(args):
        rsync_calls.append(args)
        return _ok()

    async def fake_ssh(settings, cmd):
        return _ok()

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    await push_mod.push(include_processed=True)

    # 两次 rsync：snapshot + processed（processed 含 --delete）
    assert any("--delete" in args for args in rsync_calls)


# ============================================================
# pull
# ============================================================


async def test_pull_merges_dedup_and_clears(sync_settings, monkeypatch):
    """merge：新 feedback 入库 + 重复 feedback_id 去重；成功后清 ECS inbox。"""
    await _build_main(sync_settings)  # 主库已有 fb-local

    ecs_inbox = Path(sync_settings.feedback_inbox_path).parent / "ecs_src.db"
    await _make_inbox_db(ecs_inbox, ["fb-local", "fb-new1", "fb-new2"])  # fb-local 重复

    async def fake_rsync(args):
        shutil.copy(str(ecs_inbox), args[-1])  # 模拟 rsync 拷到 pull 的 tmp dest
        return _ok()

    ssh_calls: list[str] = []

    async def fake_ssh(settings, cmd):
        ssh_calls.append(cmd)
        return _ok("CLEARED 3")

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    r = await pull_mod.pull()

    assert r["pulled"] == 2  # fb-new1 + fb-new2（fb-local 去重）
    async with aiosqlite.connect(sync_settings.db_path) as db:
        n = (await (await db.execute("SELECT COUNT(*) FROM feedback_events")).fetchone())[0]
    assert n == 3
    assert any("DELETE FROM feedback_events" in c for c in ssh_calls)


async def test_pull_no_inbox_returns_empty(sync_settings, monkeypatch):
    """ECS inbox 不存在（首次无反馈）→ pulled=0 + note，不报错、不清。"""
    await _build_main(sync_settings)

    async def fake_rsync(args):
        return CmdResult(returncode=23, output="rsync: link_stat: No such file or directory")

    async def fake_ssh(settings, cmd):
        if "wal_checkpoint" in cmd:
            return CmdResult(returncode=1, output="NO_INBOX")  # inbox 不存在
        raise AssertionError("inbox 不存在不应清")

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    r = await pull_mod.pull()
    assert r["pulled"] == 0
    assert "not yet created" in r.get("note", "")


async def test_pull_dry_run_merges_no_clear(sync_settings, monkeypatch):
    await _build_main(sync_settings)
    ecs_inbox = Path(sync_settings.feedback_inbox_path).parent / "ecs_src.db"
    await _make_inbox_db(ecs_inbox, ["fb-dry1"])

    async def fake_rsync(args):
        shutil.copy(str(ecs_inbox), args[-1])
        return _ok()

    async def fake_ssh(settings, cmd):
        if "wal_checkpoint" in cmd:
            return _ok("CKPT (0, 0, 0)")
        raise AssertionError("dry-run 不应清 inbox")

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    r = await pull_mod.pull(dry_run=True)
    assert r["pulled"] == 1
    assert r["dry_run"] is True


async def test_pull_clear_failure_raises(sync_settings, monkeypatch):
    """merge OK 但清 inbox 失败 → raise（下次重试，INSERT OR IGNORE 安全）。"""
    await _build_main(sync_settings)
    ecs_inbox = Path(sync_settings.feedback_inbox_path).parent / "ecs_src.db"
    await _make_inbox_db(ecs_inbox, ["fb-x"])

    async def fake_rsync(args):
        shutil.copy(str(ecs_inbox), args[-1])
        return _ok()

    async def fake_ssh(settings, cmd):
        return CmdResult(returncode=1, output="docker exec failed")

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    with pytest.raises(RuntimeError, match="clear failed"):
        await pull_mod.pull()


# ============================================================
# status
# ============================================================


async def test_status_compares_counts(sync_settings, monkeypatch):
    await _build_main(sync_settings)

    async def fake_ssh(settings, cmd):
        if "WINNOW_L3_SNAPSHOT_PATH" in cmd:
            return _ok("groups 1\ntopic_summaries 1\nreport_versions 0\nfeedback_events 0\n")
        return _ok("groups 0\ntopic_summaries 0\nreport_versions 0\nfeedback_events 5\n")

    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    r = await status_mod.status()
    assert r["local"]["groups"] == 1
    assert r["ecs_l3"]["groups"] == "1"
    assert r["inbox_pending_pull"] == 5


async def test_status_l3_not_pushed(sync_settings, monkeypatch):
    await _build_main(sync_settings)

    async def fake_ssh(settings, cmd):
        return _ok("NOT_EXISTS")

    monkeypatch.setattr(transport, "run_ssh", fake_ssh)

    r = await status_mod.status()
    assert r["ecs_l3"] == "NOT_EXISTS"
    assert r["inbox_pending_pull"] == 0
