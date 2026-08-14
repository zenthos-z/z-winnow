"""Tests for object_storage.r2 — Cloudflare R2 upload + presign（mock boto3，无真实 key）.

私有桶模型：upload_resources 上传文件 + 回填 cloud_key；presign_resource_urls
按 cloud_key 生成短期 cloud_url。boto3 client 用 MagicMock 注入，HEAD 404 用
botocore ClientError 模拟。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from z_winnow.object_storage import r2 as r2mod
from z_winnow.object_storage.r2 import (
    is_r2_configured,
    presign_resource_urls,
    r2_key_for,
    upload_resources,
)


def _fake_settings(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "r2_upload_enabled": True,
        "r2_endpoint": "https://acct.r2.cloudflarestorage.com",
        "r2_bucket": "winnow-mcp",
        "r2_access_key_id": "AKIATEST",
        "r2_secret_access_key": "secrettest",
        "r2_presigned_expiry": 3600,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _not_found_error(**_kw: object) -> None:
    raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


@pytest.fixture(autouse=True)
def _reset_r2_cache() -> None:
    """每个测试前清 R2 client 缓存，避免跨测试污染。"""
    r2mod.reset_client_cache()


# ----------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------


def test_r2_key_for_layout() -> None:
    assert r2_key_for("g_abc", "20260624", "doc_12345678.pdf") == (
        "attachments/g_abc/20260624/doc_12345678.pdf"
    )


def test_is_r2_configured_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings())
    assert is_r2_configured() is True


@pytest.mark.parametrize(
    "missing", ["r2_endpoint", "r2_access_key_id", "r2_secret_access_key", "r2_bucket"]
)
def test_is_r2_configured_false_on_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings(**{missing: ""}))
    assert is_r2_configured() is False


# ----------------------------------------------------------------
# upload_resources
# ----------------------------------------------------------------


def _write_resources(path, resources: list) -> None:
    path.write_text(json.dumps({"date": "20260624", "resources": resources}), encoding="utf-8")


async def test_upload_disabled_returns_zero(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """r2_upload_enabled=False → 直接 return 0，不碰 client。"""
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings(r2_upload_enabled=False))
    client = MagicMock()
    monkeypatch.setattr(r2mod, "_get_client", lambda: client)
    res_path = tmp_path / "resources.json"
    _write_resources(res_path, [{"local_path": str(tmp_path / "x.pdf")}])
    assert await upload_resources(res_path, "g1", "20260624") == 0
    client.head_object.assert_not_called()
    client.upload_file.assert_not_called()


async def test_upload_not_configured_returns_zero(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭证未配齐 → _get_client 返 None → return 0（不 monkeypatch _get_client，走真实判定）。"""
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings(r2_access_key_id=""))
    res_path = tmp_path / "resources.json"
    _write_resources(res_path, [])
    assert await upload_resources(res_path, "g1", "20260624") == 0


async def test_upload_uploads_and_writes_cloud_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 valid local_path + R2 不存在 → PUT + 回填 cloud_key。"""
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings())
    client = MagicMock()
    client.head_object.side_effect = _not_found_error  # R2 上不存在
    monkeypatch.setattr(r2mod, "_get_client", lambda: client)

    pdf = tmp_path / "doc_abc12345.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    missing = tmp_path / "missing.jpg"  # 本地不存在的 local_path → 跳过
    res_path = tmp_path / "resources.json"
    _write_resources(
        res_path,
        [
            {"resource_title": "doc.pdf", "local_path": str(pdf)},
            {"resource_title": "no-file", "local_path": str(missing)},
            {"resource_title": "no-path"},
        ],
    )

    n = await upload_resources(res_path, "g_abc", "20260624")
    assert n == 1
    client.upload_file.assert_called_once()
    call = client.upload_file.call_args
    assert call.args[1] == "winnow-mcp"  # bucket
    assert call.args[2] == "attachments/g_abc/20260624/doc_abc12345.pdf"  # key

    data = json.loads(res_path.read_text(encoding="utf-8"))
    assert data["resources"][0]["cloud_key"] == "attachments/g_abc/20260624/doc_abc12345.pdf"
    # 未上传的两个不应冒出 cloud_key
    assert "cloud_key" not in data["resources"][1]
    assert "cloud_key" not in data["resources"][2]


async def test_upload_skips_when_already_uploaded(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cloud_key 已记 + R2 存在 → 幂等跳过，不 PUT、不写盘。"""
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings())
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 10}  # 存在
    monkeypatch.setattr(r2mod, "_get_client", lambda: client)

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"xxxxxxxxxx")
    key = "attachments/g1/20260624/a.pdf"
    res_path = tmp_path / "resources.json"
    _write_resources(res_path, [{"local_path": str(pdf), "cloud_key": key}])
    mtime_before = res_path.stat().st_mtime

    n = await upload_resources(res_path, "g1", "20260624")
    assert n == 0  # 幂等跳过，不计入
    client.upload_file.assert_not_called()
    assert res_path.stat().st_mtime == mtime_before  # 未写盘


async def test_upload_dry_run_counts_without_writing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run：HEAD 判定待传 + 计数，但不 PUT、不写 cloud_key。"""
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings())
    client = MagicMock()
    client.head_object.side_effect = _not_found_error
    monkeypatch.setattr(r2mod, "_get_client", lambda: client)

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    res_path = tmp_path / "resources.json"
    _write_resources(res_path, [{"local_path": str(pdf)}])

    n = await upload_resources(res_path, "g1", "20260624", dry_run=True)
    assert n == 1
    client.upload_file.assert_not_called()
    data = json.loads(res_path.read_text(encoding="utf-8"))
    assert "cloud_key" not in data["resources"][0]  # 未回填


# ----------------------------------------------------------------
# presign_resource_urls
# ----------------------------------------------------------------


def test_presign_generates_cloud_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings())
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://acct.r2.cloudflarestorage.com/signed"
    monkeypatch.setattr(r2mod, "_get_client", lambda: client)

    resources = [
        {"cloud_key": "attachments/g1/20260624/a.pdf"},
        {"cloud_key": ""},  # 无 key → 跳过
    ]
    presign_resource_urls(resources)
    assert resources[0]["cloud_url"] == "https://acct.r2.cloudflarestorage.com/signed"
    assert "cloud_url" not in resources[1]
    client.generate_presigned_url.assert_called_once()
    kw = client.generate_presigned_url.call_args
    assert kw.args[0] == "get_object"
    assert kw.kwargs["Params"]["Key"] == "attachments/g1/20260624/a.pdf"
    assert kw.kwargs["ExpiresIn"] == 3600


def test_presign_noop_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r2mod, "get_settings", lambda: _fake_settings(r2_secret_access_key=""))
    resources = [{"cloud_key": "attachments/g1/d/a.pdf"}]
    presign_resource_urls(resources)
    assert "cloud_url" not in resources[0]


# ----------------------------------------------------------------
# 真实上传（默认 skip；配齐 .env 后 -m integration 跑）
# ----------------------------------------------------------------


@pytest.mark.integration
async def test_real_upload_roundtrip(tmp_path) -> None:
    """需要真实 R2 凭证 + 公网。验证上传 + HEAD 存在 + presign 可下。"""
    if not is_r2_configured():
        pytest.skip("R2 not configured (set WINNOW_R2_* env to run)")
    pdf = tmp_path / "real_test.pdf"
    pdf.write_bytes(b"%PDF-1.4 integration test payload")
    res_path = tmp_path / "resources.json"
    _write_resources(res_path, [{"local_path": str(pdf)}])
    n = await upload_resources(res_path, "integration_test", "20260101")
    assert n == 1
