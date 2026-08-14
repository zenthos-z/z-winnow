"""sync 模块：本地 ↔ ECS 数据同步（阶段 2.1）。

分轨单向（checkpoint §3.3）——写入职责分离，零双向冲突：
- :func:`push` — 本地 L3 → ECS（l3_snapshot.db 整库快照 + processed JSON）
- :func:`pull` — ECS feedback inbox → 本地主库（INSERT OR IGNORE 去重 + 清 inbox）
- :func:`status` — 本地 vs ECS 行数比对 + 待 pull 计数

设计原则：MVP 手动触发（``winnow sync push|pull|status``），自动化 cron 推迟到 2.5+。
"""

from __future__ import annotations

from .pull import pull
from .push import push
from .status import status

__all__ = ["pull", "push", "status"]
