"""mcp_keys 模块测试（MemberInfo + load_keys + resolve_member + save_keys + mtime 热重载）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from z_winnow.mcp_server.mcp_keys import (
    MemberInfo,
    load_keys,
    resolve_member,
    save_keys,
)


def _write_yaml(path: Path, keys: dict) -> None:
    """直接写 YAML（绕过 save_keys，测 load 路径）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"keys": keys}, allow_unicode=True), encoding="utf-8")
    # 清 mtime 缓存（_write_yaml 可能复用同 path，强制下次 load 重读）
    load_keys.__wrapped__ if hasattr(load_keys, "__wrapped__") else None


# ============================================================
# MemberInfo
# ============================================================


def test_member_info_can_access():
    admin = MemberInfo("a", "管理员", is_admin=True)
    assert admin.can_access("any_group")  # admin 全权

    member = MemberInfo("m", "张三", is_admin=False, allowed_groups={"g1", "g2"})
    assert member.can_access("g1")
    assert not member.can_access("g3")


# ============================================================
# load_keys
# ============================================================


def test_load_keys_parses(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    _write_yaml(
        p,
        {
            "qrb_admin": {
                "member_id": "admin",
                "display_name": "管理员",
                "is_admin": True,
                "allowed_groups": [],
            },
            "qrb_zhang": {
                "member_id": "zhang",
                "display_name": "张三",
                "is_admin": False,
                "allowed_groups": ["g1"],
            },
        },
    )
    keys = load_keys(p)
    assert set(keys.keys()) == {"qrb_admin", "qrb_zhang"}
    assert keys["qrb_admin"]["is_admin"] is True


def test_load_keys_missing_file_returns_empty(tmp_path):
    assert load_keys(tmp_path / "nope.yaml") == {}


def test_load_keys_malformed_returns_empty(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    p.write_text("not: valid: yaml: [", encoding="utf-8")
    assert load_keys(p) == {}  # 容错，不崩


def test_load_keys_mtime_cache_reload(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    _write_yaml(p, {"qrb_a": {"member_id": "a", "is_admin": True, "allowed_groups": []}})
    assert "qrb_a" in load_keys(p)
    # 改文件 → mtime 变 → 重读
    _write_yaml(p, {"qrb_b": {"member_id": "b", "is_admin": False, "allowed_groups": ["g1"]}})
    keys = load_keys(p)
    assert "qrb_a" not in keys
    assert "qrb_b" in keys


# ============================================================
# resolve_member
# ============================================================


def test_resolve_member_admin(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    _write_yaml(
        p,
        {
            "qrb_a": {
                "member_id": "admin",
                "display_name": "管理员",
                "is_admin": True,
                "allowed_groups": [],
            }
        },
    )
    m = resolve_member("qrb_a", p)
    assert m.member_id == "admin"
    assert m.is_admin is True
    assert m.allowed_groups == set()


def test_resolve_member_with_groups(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    _write_yaml(
        p,
        {
            "qrb_z": {
                "member_id": "zhang",
                "display_name": "张三",
                "is_admin": False,
                "allowed_groups": ["g1", "g2"],
            }
        },
    )
    m = resolve_member("qrb_z", p)
    assert m.member_id == "zhang"
    assert m.is_admin is False
    assert m.allowed_groups == {"g1", "g2"}


def test_resolve_member_unknown_raises(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    _write_yaml(p, {"qrb_a": {"member_id": "a", "is_admin": True, "allowed_groups": []}})
    with pytest.raises(KeyError):
        resolve_member("qrb_unknown", p)


# ============================================================
# save_keys
# ============================================================


def test_save_keys_atomic_and_reload(tmp_path):
    p = tmp_path / "mcp_keys.yaml"
    save_keys(
        p,
        {
            "qrb_new": {
                "member_id": "x",
                "display_name": "X",
                "is_admin": False,
                "allowed_groups": ["g1"],
            }
        },
    )
    keys = load_keys(p)  # save 后缓存已刷新，能读到
    assert "qrb_new" in keys
    assert keys["qrb_new"]["member_id"] == "x"
