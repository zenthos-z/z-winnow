"""CLI mcp-key 子命令测试（add/list/revoke/allow 写 YAML）。"""

from __future__ import annotations

from z_winnow import cli
from z_winnow.config.settings import get_settings
from z_winnow.mcp_server.mcp_keys import load_keys


def _parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def _settings_yaml(monkeypatch, tmp_path):
    """patch settings.mcp_keys_path 到 tmp（隔离，不碰真实 config/mcp_keys.yaml）。"""
    p = tmp_path / "mcp_keys.yaml"
    monkeypatch.setattr(get_settings(), "mcp_keys_path", str(p))
    return p


# ============================================================
# add
# ============================================================


async def test_mcp_key_add_writes_yaml(monkeypatch, tmp_path):
    p = _settings_yaml(monkeypatch, tmp_path)
    rc = await cli._cmd_mcp_key_add(
        _parse("mcp-key", "add", "--member", "zhang", "--name", "张三", "--groups", "g1,g2")
    )
    assert rc == 0
    keys = load_keys(p)
    assert len(keys) == 1
    key = next(iter(keys.keys()))
    entry = keys[key]
    assert key.startswith("qrb_")
    assert entry["member_id"] == "zhang"
    assert entry["display_name"] == "张三"
    assert entry["is_admin"] is False
    assert entry["allowed_groups"] == ["g1", "g2"]


async def test_mcp_key_add_admin_ignores_groups(monkeypatch, tmp_path):
    p = _settings_yaml(monkeypatch, tmp_path)
    await cli._cmd_mcp_key_add(
        _parse("mcp-key", "add", "--member", "admin", "--admin", "--groups", "g1")
    )
    entry = next(iter(load_keys(p).values()))
    assert entry["is_admin"] is True
    assert entry["allowed_groups"] == []  # admin 忽略 groups


async def test_mcp_key_add_default_name_is_member(monkeypatch, tmp_path):
    p = _settings_yaml(monkeypatch, tmp_path)
    await cli._cmd_mcp_key_add(_parse("mcp-key", "add", "--member", "zhang"))
    entry = next(iter(load_keys(p).values()))
    assert entry["display_name"] == "zhang"  # 默认 = member


# ============================================================
# list
# ============================================================


async def test_mcp_key_list_empty(monkeypatch, tmp_path, capsys):
    _settings_yaml(monkeypatch, tmp_path)
    rc = await cli._cmd_mcp_key_list(_parse("mcp-key", "list"))
    assert rc == 0
    assert "无注册 key" in capsys.readouterr().out


async def test_mcp_key_list_shows_entries(monkeypatch, tmp_path, capsys):
    _settings_yaml(monkeypatch, tmp_path)
    await cli._cmd_mcp_key_add(_parse("mcp-key", "add", "--member", "zhang", "--groups", "g1"))
    await cli._cmd_mcp_key_list(_parse("mcp-key", "list"))
    out = capsys.readouterr().out
    assert "member=zhang" in out
    assert "g1" in out


# ============================================================
# revoke
# ============================================================


async def test_mcp_key_revoke(monkeypatch, tmp_path):
    p = _settings_yaml(monkeypatch, tmp_path)
    await cli._cmd_mcp_key_add(_parse("mcp-key", "add", "--member", "zhang"))
    key = next(iter(load_keys(p).keys()))
    rc = await cli._cmd_mcp_key_revoke(_parse("mcp-key", "revoke", "--key", key))
    assert rc == 0
    assert load_keys(p) == {}


async def test_mcp_key_revoke_unknown_exit1(monkeypatch, tmp_path):
    _settings_yaml(monkeypatch, tmp_path)
    rc = await cli._cmd_mcp_key_revoke(_parse("mcp-key", "revoke", "--key", "qrb_nope"))
    assert rc == 1


# ============================================================
# allow
# ============================================================


async def test_mcp_key_allow_appends(monkeypatch, tmp_path):
    p = _settings_yaml(monkeypatch, tmp_path)
    await cli._cmd_mcp_key_add(_parse("mcp-key", "add", "--member", "zhang", "--groups", "g1"))
    key = next(iter(load_keys(p).keys()))
    await cli._cmd_mcp_key_allow(_parse("mcp-key", "allow", "--key", key, "--groups", "g2,g3"))
    entry = load_keys(p)[key]
    assert entry["allowed_groups"] == ["g1", "g2", "g3"]


async def test_mcp_key_allow_admin_noop(monkeypatch, tmp_path, capsys):
    p = _settings_yaml(monkeypatch, tmp_path)
    await cli._cmd_mcp_key_add(_parse("mcp-key", "add", "--member", "admin", "--admin"))
    key = next(iter(load_keys(p).keys()))
    rc = await cli._cmd_mcp_key_allow(_parse("mcp-key", "allow", "--key", key, "--groups", "g1"))
    assert rc == 0
    assert "管理员" in capsys.readouterr().out  # admin 无需限定群
