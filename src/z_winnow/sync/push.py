"""sync push：本地 L3 → ECS（阶段 2.1，checkpoint §3.3 L3 push 轨道）。

流程：
1. ``wal_checkpoint(TRUNCATE)`` 刷 WAL 进主库
2. ``sqlite3`` 整库 ``backup()`` → ``l3_snapshot.db``（一致性快照，非 WAL；
   产出 DELETE journal mode，ECS 端 ``mode=ro&immutable=1`` 可安全打开）
3. rsync ``l3_snapshot.db`` → ECS 临时文件 → ssh 原子 ``mv`` 替换
   （ECS MCP 靠 :func:`get_l3_db` 的 mtime 懒重连读新文件，零中断）
4. rsync ``data/processed/`` → ECS ``processed/``（L3 JSON，get_daily_report /
   list_resources 依赖），``--delete`` 保持镜像

MVP：整库 backup（含 L1/L2 等多余表，简单）。进阶可表级 dump 缩小传输。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from z_winnow.config.settings import Settings, get_settings

from . import transport
from .transport import CmdResult, check_config, rsync_e_arg, ssh_target

logger = logging.getLogger(__name__)

# 进度回调签名：(stage_id, label, pct)。前端 sync 面板按 stage_id 标记阶段清单状态。
ProgressCallback = Callable[[str, str, int], None]


def _backup_snapshot(src_db: str, dst_db: Path) -> int:
    """同步 sqlite3 backup：主库 WAL checkpoint → 整库复制到 dst。

    返回快照字节数。在 :func:`asyncio.to_thread` 中执行（CLI 手动触发，低频）。
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        dst_db.unlink()
    # 清可能的残留 -wal/-shm（backup 产非 WAL，但 dst 可能曾被 WAL 模式打开过）
    for suffix in ("-wal", "-shm"):
        side = dst_db.with_name(dst_db.name + suffix)
        if side.exists():
            side.unlink()

    src = sqlite3.connect(src_db)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)  # 阻塞复制；backup 期间 src 持读锁
        finally:
            dst.close()
    finally:
        src.close()
    return dst_db.stat().st_size


def _raise_if_failed(r: CmdResult, what: str) -> None:
    if not r.ok:
        raise RuntimeError(f"{what} 失败 (rc={r.returncode}):\n{r.combined}")


def _fire_progress(cb: ProgressCallback | None, stage: str, label: str, pct: int) -> None:
    """安全触发进度回调；cb 抛错仅 debug 日志，绝不阻断 push 主流程。

    stage 取值：snapshot / connect / upload_snapshot / upload_processed /
    upload_keys / done —— 前端 sync 面板按 stage 标记阶段清单状态。
    """
    if cb is None:
        return
    try:
        cb(stage, label, pct)
    except Exception:
        logger.debug("progress_cb raised (ignored)", exc_info=True)


async def push(
    settings: Settings | None = None,
    *,
    dry_run: bool = False,
    include_processed: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> dict:
    """推 L3 快照 + processed JSON 到 ECS。

    Args:
        settings: 配置（默认 :func:`get_settings`）。
        dry_run: 只生成本地快照并报告，不实际传输。
        include_processed: 是否同步 ``data/processed/``（首次必传；后续可关）。
        progress_cb: 可选进度回调 ``(stage_id, label, pct)``，在 6 个阶段边界触发
            （snapshot / connect / upload_snapshot / upload_processed / upload_keys / done）。
            回调抛错被吞掉，不影响推送。CLI 不传，行为不变。

    Returns:
        ``{snapshot_bytes, processed_synced, keys_synced, remote_snapshot_path, dry_run}``
    """
    settings = check_config(settings or get_settings())
    local_snapshot = Path(settings.l3_snapshot_path)

    # 1+2. 本地生成一致性快照
    snapshot_bytes = await asyncio.to_thread(_backup_snapshot, settings.db_path, local_snapshot)
    logger.info("L3 snapshot generated: %s (%d bytes)", local_snapshot, snapshot_bytes)
    _fire_progress(progress_cb, "snapshot", "生成 L3 快照", 15)

    remote_dir = settings.ecs_data_dir.rstrip("/")
    final_remote = f"{remote_dir}/l3_snapshot.db"

    if dry_run:
        logger.info("[dry-run] skip rsync; would push to %s", final_remote)
        _fire_progress(progress_cb, "done", "完成（dry-run）", 100)
        return {
            "snapshot_bytes": snapshot_bytes,
            "processed_synced": False,
            "keys_synced": False,
            "remote_snapshot_path": final_remote,
            "dry_run": True,
        }

    e = rsync_e_arg(settings)
    target = ssh_target(settings)
    tmp_remote = f"{remote_dir}/l3_snapshot.db.tmp"

    # 确保远程目录存在（幂等）
    _raise_if_failed(
        await transport.run_ssh(settings, f"mkdir -p {remote_dir}"), "mkdir remote data_dir"
    )
    _fire_progress(progress_cb, "connect", "连接 ECS", 30)

    # 3. rsync 快照 → 临时文件 → 原子 mv（ECS MCP mtime 懒重连读新文件）
    _raise_if_failed(
        await transport.run_rsync(["-avz", "-e", e, str(local_snapshot), f"{target}:{tmp_remote}"]),
        "rsync l3_snapshot.db",
    )
    _raise_if_failed(
        await transport.run_ssh(settings, f"mv -f {tmp_remote} {final_remote}"),
        "atomic mv l3_snapshot.db",
    )
    logger.info("L3 snapshot pushed → %s", final_remote)
    _fire_progress(progress_cb, "upload_snapshot", "上传 L3 快照", 55)

    # 4. processed/ JSON（get_daily_report / list_resources 依赖）
    processed_synced = False
    if include_processed:
        local_proc = Path(settings.layer3_output_dir)
        if local_proc.exists():
            remote_proc = f"{remote_dir}/processed"
            _raise_if_failed(
                await transport.run_ssh(settings, f"mkdir -p {remote_proc}"),
                "mkdir remote processed",
            )
            # 尾斜杠：src 内容镜像到 dest/；--delete 保持一致（本地删则 ECS 删）
            # 排除 attachments/：附件二进制已上传 R2（私有桶预签名），ECS 不再需要
            # 本地副本（MCP serve 走 cloud_url；省 ~265MB/次的 rsync payload + ECS 磁盘）
            _raise_if_failed(
                await transport.run_rsync(
                    [
                        "-avz",
                        "--delete",
                        "--exclude=attachments/",
                        "-e",
                        e,
                        f"{local_proc}/",
                        f"{target}:{remote_proc}/",
                    ]
                ),
                "rsync processed/",
            )
            processed_synced = True
            logger.info("processed/ synced → %s", remote_proc)
            _fire_progress(progress_cb, "upload_processed", "同步 processed JSON", 85)
        else:
            logger.warning("processed dir not found, skip: %s", local_proc)
            _fire_progress(
                progress_cb, "upload_processed", "同步 processed JSON（跳过，目录不存在）", 85
            )
    else:
        _fire_progress(progress_cb, "upload_processed", "同步 processed JSON（跳过）", 85)

    # 5. mcp_keys.yaml（MCP 鉴权注册表，ECS http 模式必需 —— 无则 http 调用全被拒）
    keys_synced = False
    local_keys = Path(settings.mcp_keys_path)
    if local_keys.exists():
        remote_keys = f"{remote_dir}/mcp_keys.yaml"
        _raise_if_failed(
            await transport.run_rsync(
                ["-avz", "-e", e, str(local_keys), f"{target}:{remote_keys}"]
            ),
            "rsync mcp_keys.yaml",
        )
        keys_synced = True
        logger.info("mcp_keys.yaml synced → %s", remote_keys)
        _fire_progress(progress_cb, "upload_keys", "同步鉴权配置", 95)
    else:
        logger.warning(
            "mcp_keys.yaml not found at %s — ECS http 调用将被拒绝（先 winnow mcp-key add）",
            local_keys,
        )
        _fire_progress(progress_cb, "upload_keys", "同步鉴权配置（跳过，文件不存在）", 95)

    _fire_progress(progress_cb, "done", "完成", 100)
    return {
        "snapshot_bytes": snapshot_bytes,
        "processed_synced": processed_synced,
        "keys_synced": keys_synced,
        "remote_snapshot_path": final_remote,
        "dry_run": False,
    }
