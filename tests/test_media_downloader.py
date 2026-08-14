"""Tests for content_enrich/media_downloader.py + output_composer local_path patch (#9.3).

Covers the resource-file landing pipeline:
  - download_media_batch: type filter, appmsg-file URL extraction, mediaLocalPath
    defense, relative-URL join, size cap, dedup, failure isolation, filename fallbacks
  - patch_resources_local_path: source_server_ids → resource["local_path"] 回填

All HTTP mocked via httpx.MockTransport injected through ``_transport`` (no network).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from z_winnow.content_enrich.media_downloader import download_media_batch

# ============================================================
# Helpers
# ============================================================


def _msg(
    sid: str,
    msg_type: str,
    *,
    media_url: str = "",
    raw_content: str = "",
    raw_json: dict | str | None = None,
) -> dict:
    m: dict = {"server_id": sid, "msg_type": msg_type, "media_url": media_url}
    if raw_content:
        m["raw_content"] = raw_content
    if raw_json is not None:
        m["raw_json"] = raw_json if isinstance(raw_json, str) else json.dumps(raw_json)
    return m


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ============================================================
# Type filter
# ============================================================


async def test_type_filter_downloads_image_emoji_file_skips_video_voice(tmp_path):
    """image/emoji/file 下载; video/voice 不在过滤集, 不下载。"""
    transport = _transport(lambda r: httpx.Response(200, content=b"data"))
    msgs = [
        _msg("s1", "image", media_url="http://srv/img.jpg"),
        _msg("s2", "emoji", media_url="http://srv/e.gif"),
        _msg("s3", "file", media_url="http://srv/doc.pdf"),
        _msg("s4", "video", media_url="http://srv/v.mp4"),
        _msg("s5", "voice", media_url="http://srv/a.amr"),
    ]
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert set(out) == {"s1", "s2", "s3"}


async def test_appmsg_non_file_subtype_skipped(tmp_path):
    """appmsg 但子类型是 article(非 file) → 不下载。"""
    xml = (
        "<msg><appmsg><title>文章</title><type>5</type>"
        "<url>http://srv/art.html</url></appmsg></msg>"
    )
    msgs = [_msg("s1", "appmsg", raw_content=xml)]
    transport = _transport(lambda r: httpx.Response(200, content=b"x"))
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert out == {}


# ============================================================
# appmsg file: URL extracted from XML
# ============================================================


async def test_appmsg_file_extracts_url_and_title_from_xml(tmp_path):
    """appmsg 文件型(type=6): media_url 空 → 从 <appmsg><url> 提取; title 作文件名。"""
    xml = (
        "<msg><appmsg><title>设计规范.pdf</title><type>6</type>"
        "<url>http://srv/doc.pdf</url></appmsg></msg>"
    )
    msgs = [_msg("s1", "appmsg", raw_content=xml)]  # media_url 空
    transport = _transport(lambda r: httpx.Response(200, content=b"%PDF-1.4"))
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert "s1" in out
    assert "设计规范" in os.path.basename(out["s1"])
    assert Path(out["s1"]).read_bytes() == b"%PDF-1.4"


# ============================================================
# mediaLocalPath defense (WeFlow)
# ============================================================


async def test_local_path_media_url_skipped(tmp_path):
    """media_url 是本地路径(C:\\...)无 http scheme → 跳过, 不抛。"""
    msgs = [_msg("s1", "image", media_url=r"C:\cache\x.jpg")]
    transport = _transport(lambda r: httpx.Response(200, content=b"x"))
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert out == {}


async def test_relative_url_joined_with_base(tmp_path):
    """media_url 相对路径(/api/...) + base_url → 拼接下载。"""
    msgs = [_msg("s1", "image", media_url="/api/v1/media/room/x.jpg")]

    def handler(r: httpx.Request) -> httpx.Response:
        assert str(r.url) == "http://srv:5031/api/v1/media/room/x.jpg"
        return httpx.Response(200, content=b"img")

    transport = _transport(handler)
    out = await download_media_batch(
        msgs, str(tmp_path), base_url="http://srv:5031", _transport=transport
    )
    assert "s1" in out


# ============================================================
# WeFlow 真实坑: 0.0.0.0 绑定哨兵 + 媒体端点鉴权
# ============================================================


async def test_bind_host_0000_rewritten_to_base(tmp_path):
    """WeFlow 媒体 URL 广告 0.0.0.0(绑定哨兵, 不可达) → 改写成 base_url 的 host:port。

    回归 #9.3 真实联调发现的 bug: imageCachePath 是 ``http://0.0.0.0:5031/...``,
    客户端连不上。测试用 RFC 5737 文档地址，非真实主机。
    """
    msgs = [_msg("s1", "image", media_url="http://0.0.0.0:5031/api/v1/media/room/x.jpg")]

    def handler(r: httpx.Request) -> httpx.Response:
        # 必须改写成 base_url 的 host, 不能是 0.0.0.0
        assert r.url.host == "192.0.2.10"
        assert r.url.port == 5031
        return httpx.Response(200, content=b"img")

    transport = _transport(handler)
    out = await download_media_batch(
        msgs, str(tmp_path), base_url="http://192.0.2.10:5031", _transport=transport
    )
    assert "s1" in out


async def test_non_bind_host_not_rewritten(tmp_path):
    """合法 host(CDN/内网) 不该被改写 —— 只动 0.0.0.0。"""
    msgs = [_msg("s1", "image", media_url="http://cdn.example.com/x.jpg")]

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.host == "cdn.example.com"
        return httpx.Response(200, content=b"img")

    transport = _transport(handler)
    out = await download_media_batch(
        msgs, str(tmp_path), base_url="http://192.0.2.10:5031", _transport=transport
    )
    assert "s1" in out


async def test_token_sent_as_bearer_header(tmp_path):
    """媒体端点要鉴权(401) → token 非空时带 Authorization: Bearer。"""
    msgs = [_msg("s1", "image", media_url="http://srv/img.jpg")]

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.headers.get("authorization") == "Bearer secret-token-xyz"
        return httpx.Response(200, content=b"img")

    transport = _transport(handler)
    out = await download_media_batch(
        msgs, str(tmp_path), token="secret-token-xyz", _transport=transport
    )
    assert "s1" in out


async def test_no_token_no_auth_header(tmp_path):
    """token 空 → 不带 Authorization 头(向后兼容, CipherTalk 可选鉴权)。"""
    msgs = [_msg("s1", "image", media_url="http://srv/img.jpg")]

    def handler(r: httpx.Request) -> httpx.Response:
        assert "authorization" not in {k.lower() for k in r.headers}
        return httpx.Response(200, content=b"img")

    transport = _transport(handler)
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert "s1" in out


# ============================================================
# Size cap + dedup + failure isolation
# ============================================================


async def test_size_cap_aborts_and_cleans_tmp(tmp_path):
    """超 max_bytes → 中止, 不留文件, 返回空。"""
    msgs = [_msg("s1", "image", media_url="http://srv/big")]
    transport = _transport(lambda r: httpx.Response(200, content=b"x" * 2048))
    out = await download_media_batch(msgs, str(tmp_path), max_bytes=1024, _transport=transport)
    assert out == {}
    # 临时文件必须清理干净
    assert list(tmp_path.iterdir()) == []


async def test_dedup_same_content_returns_same_path(tmp_path):
    """同内容 → 同 hash → 同路径; 第二次复用已存在文件。"""
    msgs = [_msg("s1", "image", media_url="http://srv/img.jpg")]
    transport = _transport(lambda r: httpx.Response(200, content=b"hello"))
    out1 = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    out2 = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert out1["s1"] == out2["s1"]
    assert Path(out1["s1"]).exists()


async def test_failure_isolation(tmp_path):
    """一个 URL 500, 其他仍正常下载。"""

    def handler(r: httpx.Request) -> httpx.Response:
        if "500" in r.url.path:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok")

    transport = _transport(handler)
    msgs = [
        _msg("s1", "image", media_url="http://srv/ok.jpg"),
        _msg("s2", "image", media_url="http://srv/500"),
    ]
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert "s1" in out and "s2" not in out


# ============================================================
# Filename fallbacks
# ============================================================


async def test_filename_from_fileupload_title(tmp_path):
    xml = (
        "<msg><fileupload><title>季度报告.pdf</title><length>100</length>"
        "<cdnattachurl>cdnkey</cdnattachurl></fileupload></msg>"
    )
    msgs = [_msg("s1", "file", media_url="http://srv/get", raw_content=xml)]
    transport = _transport(lambda r: httpx.Response(200, content=b"%PDF"))
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert "s1" in out
    assert "季度报告" in os.path.basename(out["s1"])


async def test_filename_from_raw_json_mediafilename(tmp_path):
    msgs = [
        _msg(
            "s1",
            "image",
            media_url="http://srv/path/",
            raw_json={"mediaFileName": "photo.jpg"},
        )
    ]
    transport = _transport(lambda r: httpx.Response(200, content=b"img"))
    out = await download_media_batch(msgs, str(tmp_path), _transport=transport)
    assert "s1" in out
    assert "photo" in os.path.basename(out["s1"])


# ============================================================
# output_composer.patch_resources_local_path
# ============================================================


def test_patch_resources_local_path_matches(tmp_path):
    from z_winnow.subagents.output_composer import patch_resources_local_path

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    messages = [{"server_id": "sid1", "media_local_path": str(f)}]
    resources_data = {
        "date": "20260709",
        "resources": [
            {"resource_title": "R1", "source_server_ids": ["sid1"]},
            {"resource_title": "R2", "source_server_ids": ["nope"]},
        ],
    }
    rpath = tmp_path / "resources.json"
    rpath.write_text(json.dumps(resources_data), encoding="utf-8")

    n = patch_resources_local_path(rpath, messages)
    assert n == 1
    out = json.loads(rpath.read_text(encoding="utf-8"))
    assert out["resources"][0]["local_path"] == str(f)
    assert "local_path" not in out["resources"][1]


def test_patch_resources_skips_when_file_missing_on_disk(tmp_path):
    """media_local_path 指向不存在的文件 → 不回填。"""
    from z_winnow.subagents.output_composer import patch_resources_local_path

    messages = [{"server_id": "sid1", "media_local_path": str(tmp_path / "no.pdf")}]
    resources_data = {"resources": [{"source_server_ids": ["sid1"]}]}
    rpath = tmp_path / "resources.json"
    rpath.write_text(json.dumps(resources_data), encoding="utf-8")
    assert patch_resources_local_path(rpath, messages) == 0


def test_patch_resources_missing_file_returns_zero(tmp_path):
    from z_winnow.subagents.output_composer import patch_resources_local_path

    assert patch_resources_local_path(tmp_path / "nope.json", []) == 0


# 防御: import 期检查 httpx.MockTransport 可用
def test_mock_transport_available() -> None:
    assert httpx.MockTransport is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
