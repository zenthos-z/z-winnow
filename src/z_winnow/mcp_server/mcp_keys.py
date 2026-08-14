"""MCP API key 注册表（key → 成员 + 群组权限白名单）。

YAML 配置（默认 ``config/mcp_keys.yaml``，gitignored）。mtime 缓存热重载 ——
加/撤 key 无需重启 server。``sync push`` 推到 ECS。

YAML schema::

    keys:
      qrb_<random>:           # API key（secrets.token_urlsafe 生成）
        member_id: admin      # 稳定成员标识（写入 feedback_events.reporter）
        display_name: 管理员  # 可读名
        is_admin: true        # true = 全权（所有群）；allowed_groups 忽略
        allowed_groups: []    # is_admin=false 时可访问的 group_id 列表

阶段：MCP key-based 鉴权深化（docs/mcp-platform-checkpoint.md §4.1 安全模型）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class MemberInfo:
    """当前调用者的身份与权限（middleware 从 key 解析注入 contextvar）。"""

    member_id: str
    display_name: str
    is_admin: bool
    allowed_groups: set[str] = field(default_factory=set)

    def can_access(self, group_id: str) -> bool:
        """是否有权访问某群（admin 全权）。"""
        return self.is_admin or group_id in self.allowed_groups


# mtime 缓存：{path_str: (mtime, keys_dict)}。YAML 改动 → 下次 load 重读。
_keys_cache: dict[str, tuple[float, dict[str, dict]]] = {}


def _read_yaml(path: Path) -> dict[str, dict]:
    """读 YAML 原始结构 → ``{key: entry_dict}``。文件不存在/空/格式错 → ``{}``。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        logger.warning("mcp_keys.yaml 格式错误，按空注册表处理: %s", path)
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        return {}
    # 只保留 dict 型 entry（容错：跳过畸形条目）
    return {k: v for k, v in keys.items() if isinstance(v, dict)}


def load_keys(path: str | Path) -> dict[str, dict]:
    """加载 key 注册表（mtime 缓存热重载）。

    Returns:
        ``{api_key: {member_id, display_name, is_admin, allowed_groups}}``
    """
    p = Path(path)
    cache_key = str(p.resolve())
    try:
        mtime = p.stat().st_mtime if p.exists() else 0.0
    except OSError:
        mtime = 0.0
    cached = _keys_cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    data = _read_yaml(p)
    _keys_cache[cache_key] = (mtime, data)
    return data


def save_keys(path: str | Path, keys: dict[str, dict]) -> None:
    """原子写 YAML（``.tmp`` → ``replace``），并刷新缓存。CLI add/revoke 用。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"keys": keys}, f, allow_unicode=True, sort_keys=False)
    tmp.replace(p)  # 原子替换
    _keys_cache.pop(str(p.resolve()), None)  # 强制下次 load 重读
    logger.info("mcp_keys.yaml saved: %d keys", len(keys))


def resolve_member(api_key: str, path: str | Path) -> MemberInfo:
    """API key → :class:`MemberInfo`。

    Raises:
        KeyError: key 未在注册表（middleware 转 ``ToolError`` 拒绝）。
    """
    keys = load_keys(path)
    entry = keys.get(api_key)
    if entry is None:
        raise KeyError("API key not registered")
    allowed = entry.get("allowed_groups") or []
    return MemberInfo(
        member_id=str(entry.get("member_id") or ""),
        display_name=str(entry.get("display_name") or ""),
        is_admin=bool(entry.get("is_admin", False)),
        allowed_groups=({str(g) for g in allowed} if isinstance(allowed, (list, tuple)) else set()),
    )
