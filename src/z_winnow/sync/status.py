"""sync status：本地 vs ECS 数据比对 + 待 pull 计数（阶段 2.1）。

本地主库行数 + ECS l3_snapshot 副本行数 + ECS feedback inbox 待 pull 数，
供 ``winnow sync status`` 判断是否需要 push/pull。

ECS 端查询走 ``docker exec`` 容器内 python3（容器内有 sqlite3，宿主不一定有）。
"""

from __future__ import annotations

import logging

import aiosqlite

from z_winnow.config.settings import Settings, get_settings

from . import transport
from .transport import check_config

logger = logging.getLogger(__name__)

# 比对的关键表（读工具实际查的 + feedback）
_COMPARE_TABLES = ("groups", "topic_summaries", "report_versions", "feedback_events")


async def _count_local(main_db: str) -> dict[str, int]:
    """本地主库各表行数（表缺失记 -1，不阻断）。"""
    out: dict[str, int] = {}
    async with aiosqlite.connect(main_db) as db:
        for t in _COMPARE_TABLES:
            try:
                cur = await db.execute(f"SELECT COUNT(*) FROM {t}")
                row = await cur.fetchone()
                out[t] = int(row[0]) if row else 0
            except aiosqlite.OperationalError as e:
                out[t] = -1
                logger.warning("local count %s failed: %s", t, e)
    return out


def _remote_count_cmd(settings: Settings, db_env: str, default_path: str) -> str:
    """``docker exec`` 容器内 python3 查指定库各表行数（库不存在打印 NOT_EXISTS）。"""
    tables_repr = repr(tuple(_COMPARE_TABLES))
    # 多行 script（shell 双引号内换行合法；script 内只用单引号字面量，免转义）
    script = (
        "import os, sqlite3\n"
        f"p = os.environ.get('{db_env}', '{default_path}')\n"
        "if not os.path.exists(p):\n"
        "    print('NOT_EXISTS')\n"
        "else:\n"
        "    c = sqlite3.connect(p)\n"
        f"    for t in {tables_repr}:\n"
        "        try:\n"
        "            print(t, c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0])\n"
        "        except Exception as e:\n"
        "            print(t, 'ERR', str(e))\n"
        "    c.close()"
    )
    return f'docker exec {settings.ecs_container_name} python3 -c "{script}"'


async def _count_remote_db(
    settings: Settings, db_env: str, default_path: str
) -> dict[str, str] | str:
    """返回 ``{table: count_str}``，或 ``"NOT_EXISTS"``（库未建 / 未 push）。"""
    r = await transport.run_ssh(settings, _remote_count_cmd(settings, db_env, default_path))
    if not r.ok:
        return f"query failed (rc={r.returncode}): {r.output.strip()}"
    lines = [ln.strip() for ln in r.output.splitlines() if ln.strip()]
    if lines and lines[0] == "NOT_EXISTS":
        return "NOT_EXISTS"
    out: dict[str, str] = {}
    for ln in lines:
        parts = ln.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


async def status(settings: Settings | None = None) -> dict:
    """比对本地与 ECS 数据状态。

    Returns:
        ``{local, ecs_l3, ecs_inbox, inbox_pending_pull}``。
        ``ecs_l3`` / ``ecs_inbox`` 可能是 dict、``"NOT_EXISTS"`` 或错误串。
    """
    settings = check_config(settings or get_settings())
    local = await _count_local(settings.db_path)
    ecs_l3 = await _count_remote_db(settings, "WINNOW_L3_SNAPSHOT_PATH", "/app/data/l3_snapshot.db")
    ecs_inbox = await _count_remote_db(
        settings, "WINNOW_FEEDBACK_INBOX_PATH", "/app/data/feedback_inbox.db"
    )

    inbox_pending = 0
    if isinstance(ecs_inbox, dict):
        val = ecs_inbox.get("feedback_events", "0")
        inbox_pending = int(val) if val.lstrip("-").isdigit() else 0

    return {
        "local": local,
        "ecs_l3": ecs_l3,
        "ecs_inbox": ecs_inbox,
        "inbox_pending_pull": inbox_pending,
    }
