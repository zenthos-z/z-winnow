"""sync push 的 progress_cb + sync_service（Web 一键推送）测试。

mock transport 不真连 ECS；验：
- push 的 6 阶段进度回调按序触发、dry-run 只发 snapshot+done、cb=None 兼容
- sync_service.start_sync 的并发守卫（409）、配置缺失（400）、成功后进度流转到 done +
  last_sync 从 async_tasks 解析、失败标记 failed
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from z_winnow.config.settings import get_settings
from z_winnow.sync import transport
from z_winnow.sync.transport import CmdResult, SyncConfigError
from z_winnow.web.services import sync_service

push_mod = importlib.import_module("z_winnow.sync.push")


# ============================================================
# fixtures
# ============================================================


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


@pytest.fixture(autouse=True)
def _reset_live():
    """每个测试前清掉 sync_service 的内存单槽态（避免跨用例污染）。"""
    sync_service._LIVE = None
    yield
    sync_service._LIVE = None


def _ok(output: str = "") -> CmdResult:
    return CmdResult(returncode=0, output=output)


def _patch_transport_ok(monkeypatch):
    async def fake_rsync(args):
        return _ok()

    async def fake_ssh(settings, cmd):
        return _ok()

    monkeypatch.setattr(transport, "run_rsync", fake_rsync)
    monkeypatch.setattr(transport, "run_ssh", fake_ssh)


# ============================================================
# push — progress_cb
# ============================================================


async def test_push_progress_cb_fires_all_stages(sync_settings, monkeypatch):
    """progress_cb 按 6 阶段顺序触发，pct 递增。"""
    _patch_transport_ok(monkeypatch)
    stages: list[tuple[str, str, int]] = []

    await push_mod.push(progress_cb=lambda s, lab, p: stages.append((s, lab, p)))

    ids = [s[0] for s in stages]
    assert ids == [
        "snapshot",
        "connect",
        "upload_snapshot",
        "upload_processed",
        "upload_keys",
        "done",
    ]
    pcts = [s[2] for s in stages]
    assert pcts == sorted(pcts) and pcts[-1] == 100  # 单调递增、收尾 100


async def test_push_progress_cb_none_is_noop(sync_settings, monkeypatch):
    """progress_cb=None 时 push 行为不变（CLI 回归）。"""
    _patch_transport_ok(monkeypatch)
    r = await push_mod.push(progress_cb=None)
    assert r["dry_run"] is False
    assert r["processed_synced"] is True


async def test_push_dry_run_progress_only_snapshot_and_done(sync_settings, monkeypatch):
    """dry-run 不传输，只发 snapshot + done。"""
    _patch_transport_ok(monkeypatch)
    stages: list[tuple[str, str, int]] = []

    r = await push_mod.push(dry_run=True, progress_cb=lambda s, lab, p: stages.append((s, lab, p)))

    assert [s[0] for s in stages] == ["snapshot", "done"]
    assert r["dry_run"] is True


async def test_push_progress_cb_exception_swallowed(sync_settings, monkeypatch):
    """回调抛错被吞掉，不影响 push 主流程。"""
    _patch_transport_ok(monkeypatch)

    def bad_cb(s, lab, p):
        raise ValueError("boom")

    r = await push_mod.push(progress_cb=bad_cb)  # 不应抛
    assert r["dry_run"] is False


# ============================================================
# sync_service — start_sync / get_progress
# ============================================================


async def test_start_sync_unconfigured_raises(monkeypatch):
    """ECS 未配置 → SyncConfigError（路由层映射 400）。"""
    s = get_settings()
    monkeypatch.setattr(s, "ecs_ssh_host", "")
    monkeypatch.setattr(s, "ecs_ssh_key", "")
    with pytest.raises(SyncConfigError, match="未配置"):
        await sync_service.start_sync()


async def test_start_sync_concurrency_guard(sync_settings):
    """已有同步在跑 → SyncInProgressError（路由层映射 409）。"""
    sync_service._set_live(state="syncing", pct=10)
    with pytest.raises(sync_service.SyncInProgressError):
        await sync_service.start_sync()


async def test_start_sync_success_progress_to_done(sync_settings, monkeypatch):
    """成功路径：start_sync 立即 syncing → 后台跑完 → done + last_sync 摘要。"""
    fake_summary = {
        "snapshot_bytes": 9999,
        "processed_synced": True,
        "keys_synced": False,
        "remote_snapshot_path": "/app/data/l3_snapshot.db",
        "dry_run": False,
    }

    async def fake_push(*, progress_cb=None, **_kw):
        for stage, label, pct in [
            ("snapshot", "生成 L3 快照", 15),
            ("connect", "连接 ECS", 30),
            ("upload_snapshot", "上传 L3 快照", 55),
            ("upload_processed", "同步 processed JSON", 85),
            ("upload_keys", "同步鉴权配置", 95),
            ("done", "完成", 100),
        ]:
            if progress_cb:
                progress_cb(stage, label, pct)
            await asyncio.sleep(0)  # 让出循环，便于观测 syncing 中间态
        return fake_summary

    monkeypatch.setattr(sync_service, "sync_push", fake_push)

    res = await sync_service.start_sync()
    assert res["state"] == "syncing"

    # 立即查应是 syncing（start_sync 同步置态，不依赖后台任务调度）
    p = await sync_service.get_progress()
    assert p["state"] == "syncing"

    # 轮询等后台收尾
    for _ in range(100):
        p = await sync_service.get_progress()
        if p["state"] != "syncing":
            break
        await asyncio.sleep(0.02)

    assert p["state"] == "done"
    assert p["pct"] == 100
    assert p["last_sync"] is not None
    assert p["last_sync"].snapshot_bytes == 9999
    assert p["last_sync"].processed_synced is True


async def test_start_sync_failure_marks_failed(sync_settings, monkeypatch):
    """push 抛错 → 后台标记 failed + 错误信息透传。"""

    async def fake_push(*, progress_cb=None, **_kw):
        if progress_cb:
            progress_cb("snapshot", "生成 L3 快照", 15)
        raise RuntimeError("boom-from-push")

    monkeypatch.setattr(sync_service, "sync_push", fake_push)

    await sync_service.start_sync()

    for _ in range(100):
        p = await sync_service.get_progress()
        if p["state"] not in ("syncing",):
            break
        await asyncio.sleep(0.02)

    assert p["state"] == "failed"
    assert "boom-from-push" in (p["error"] or "")


async def test_get_last_sync_none_when_no_history(sync_settings):
    """无历史同步 → get_progress 的 last_sync 为 None。"""
    p = await sync_service.get_progress()
    assert p["state"] == "idle"
    assert p["last_sync"] is None
