"""sync pull：ECS feedback inbox → 本地主库（阶段 2.1，checkpoint §3.3 feedback pull 轨道）。

流程：
0. ssh ``docker exec`` ECS 容器 ``PRAGMA wal_checkpoint(TRUNCATE)`` —— inbox 是 WAL 模式，
   rsync 只拉主库 ``.db``（不含 ``-wal``），未 checkpoint 时表/数据都在 ``-wal`` →
   必先合并到主库，否则拉到的库 ``no such table``
1. rsync ECS ``feedback_inbox.db`` → 本地临时文件
2. 本地主库 ``ATTACH`` 临时库 + ``INSERT OR IGNORE INTO feedback_events SELECT *``
   （按 ``feedback_id`` PK 去重）
3. merge 成功 → ssh ``docker exec`` ECS 容器跑 python3 ``DELETE FROM feedback_events``
   清 inbox（**保留文件 + schema**；merge 失败不清，下次重试，不丢反馈）

为何不直接替换 inbox 文件：ECS MCP 的 ``_inbox_conn`` 是 rw 单例、无 mtime 重连，
文件被替换后旧连接指向 unlinked inode，新反馈写到不可达文件 → 丢失。故用 SQL DELETE
保留同一文件 + 连接（WAL 多连接并发安全）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from z_winnow.config.settings import Settings, get_settings

from . import transport
from .transport import CmdResult, check_config, rsync_e_arg, ssh_target

logger = logging.getLogger(__name__)


def _raise_if_failed(r: CmdResult, what: str) -> None:
    if not r.ok:
        raise RuntimeError(f"{what} 失败 (rc={r.returncode}):\n{r.combined}")


def _is_no_such_file(output: str) -> bool:
    """rsync 远程文件不存在的特征（首次 inbox 未建）。"""
    low = output.lower()
    return (
        "no such file or directory" in low or "not a regular file" in low or "no such file" in low
    )


async def _merge_inbox(main_db: str, tmp_inbox: Path) -> int:
    """ATTACH tmp + INSERT OR IGNORE，返回实际插入行数（去重后）。

    schema 一致保证：inbox（ECS :func:`get_inbox_db`）与 main（本地）均由
    :func:`init_database_in_conn` 建 + 跑同一套 migrations，列顺序一致 → ``SELECT *`` 安全。
    """
    inserted = 0
    async with aiosqlite.connect(main_db) as main:
        await main.execute("PRAGMA busy_timeout=5000")
        await main.execute("ATTACH ? AS inbox_db", (str(tmp_inbox),))
        try:
            cur = await main.execute(
                "INSERT OR IGNORE INTO feedback_events SELECT * FROM inbox_db.feedback_events"
            )
            await main.commit()
            # rowcount: INSERT OR IGNORE 下为实际写入行数（冲突忽略不计）
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        finally:
            await main.execute("DETACH inbox_db")
    return inserted


def _clear_inbox_cmd(settings: Settings) -> str:
    """docker exec ECS 容器跑 python3 清 feedback_inbox（读容器内 env 跟随配置）。"""
    py = (
        "import os,sqlite3;"
        "p=os.environ.get('WINNOW_FEEDBACK_INBOX_PATH','/app/data/feedback_inbox.db');"
        "c=sqlite3.connect(p);"
        "n=c.execute('SELECT COUNT(*) FROM feedback_events').fetchone()[0];"
        "c.execute('DELETE FROM feedback_events');"
        "c.commit();c.close();"
        "print('CLEARED',n)"
    )
    return f'docker exec {settings.ecs_container_name} python3 -c "{py}"'


async def pull(settings: Settings | None = None, *, dry_run: bool = False) -> dict:
    """拉 ECS feedback inbox → merge 本地主库 → 清 ECS inbox。

    Args:
        settings: 配置（默认 :func:`get_settings`）。
        dry_run: 只 rsync + 报告 merge 行数，不清 ECS inbox。

    Returns:
        ``{pulled, cleared, dry_run, note?}``。``pulled`` = 实际新插入行数。
    """
    settings = check_config(settings or get_settings())
    remote_inbox = f"{settings.ecs_data_dir.rstrip('/')}/feedback_inbox.db"
    local_inbox_dir = Path(settings.feedback_inbox_path).parent
    local_inbox_dir.mkdir(parents=True, exist_ok=True)
    tmp_inbox = local_inbox_dir / "feedback_inbox.pull.tmp.db"

    # 0. checkpoint ECS inbox WAL → 主库（rsync 只拉主库 .db，不含 -wal；
    #    ECS inbox 是 WAL 模式，未 checkpoint 时表/数据在 -wal → 拉到的主库缺表）
    ckpt_py = (
        "import os,sqlite3,sys;"
        "p=os.environ.get('WINNOW_FEEDBACK_INBOX_PATH','/app/data/feedback_inbox.db');"
        "os.path.exists(p) or sys.exit('NO_INBOX');"  # inbox 未建（首次无反馈）→ 不 checkpoint，rsync 自然 no-such-file
        "c=sqlite3.connect(p);"
        "r=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone();"
        "c.close();print('CKPT',r)"
    )
    r0 = await transport.run_ssh(
        settings, f'docker exec {settings.ecs_container_name} python3 -c "{ckpt_py}"'
    )
    if r0.ok:
        logger.info("ECS inbox WAL checkpoint: %s", r0.output.strip().splitlines()[-1:])
    else:
        logger.warning("ECS inbox WAL checkpoint 失败（继续尝试 rsync）: %s", r0.combined)

    # 1. rsync ECS inbox → 本地 tmp
    r = await transport.run_rsync(
        [
            "-avz",
            "-e",
            rsync_e_arg(settings),
            f"{ssh_target(settings)}:{remote_inbox}",
            str(tmp_inbox),
        ]
    )
    if not r.ok:
        tmp_inbox.unlink(missing_ok=True)
        if _is_no_such_file(r.combined):
            return {
                "pulled": 0,
                "cleared": False,
                "dry_run": dry_run,
                "note": "ECS inbox not yet created (no feedback submitted on ECS)",
            }
        _raise_if_failed(r, "rsync feedback_inbox.db")

    # 2. merge 进本地主库
    try:
        inserted = await _merge_inbox(settings.db_path, tmp_inbox)
    finally:
        tmp_inbox.unlink(missing_ok=True)
    logger.info("merged %d feedback rows from ECS inbox", inserted)

    if dry_run:
        return {"pulled": inserted, "cleared": False, "dry_run": True}

    # 3. merge 成功（含 0 行）→ 清 ECS inbox（SQL DELETE 保留文件 + 连接）
    r2 = await transport.run_ssh(settings, _clear_inbox_cmd(settings))
    if not r2.ok:
        raise RuntimeError(
            f"inbox merge OK ({inserted} rows) but ECS clear failed — "
            f"下次 pull 会重复 merge（INSERT OR IGNORE 去重，安全）:\n{r2.combined}"
        )
    cleared = r2.output.strip().splitlines()[-1] if r2.output.strip() else "CLEARED ?"
    logger.info("ECS inbox cleared: %s", cleared)

    return {"pulled": inserted, "cleared": cleared, "dry_run": False}
