"""媒体文件下载落盘（content_enrich 阶段）.

在 content_enrich 节点 Phase A(parse_raw_messages) 与 Phase B(analyze_images_batch)
之间调用, 把群聊里直接发的图片/表情/文件/appmsg文件型 下载到本地
``{layer3_output_dir}/{group_id}/{date}/attachments/`` 目录。

下载成功后, 调用方把本地路径回写进 ``message["media_local_path"]`` —— Phase B 的
image_analyzer 因此改用本地路径（顺带修复 CipherTalk 远程部署时 open() 远程 URL 崩溃）,
后续资源附件上传也消费该路径。

数据源信任模型:
  - ``media_url`` 来自 CipherTalk/WeFlow 受控服务, 常是内网地址(127.0.0.1:5031 /
    0.0.0.0:5031)。**不做 SSRF 内网拦截**(否则把媒体服务全拒了), 仅靠
    scheme=http/https + 大小上限 + 超时 防护。与 link_fetcher 的 is_private_url 不同。

WeFlow 归一化后的两类坑(见 weflow_client._normalize_weflow_to_ciphertalk):
  1. **appmsg 文件型**: ``_MEDIA_FIELD_BY_KIND`` 不含 ``appmsg`` → media dict 空 →
     ``media_url=""``。下载 URL 只在 rawContent ``<appmsg><url>`` 里, 这里用
     ``card_parser.try_parse_appmsg_safe`` 提取。
  2. **mediaLocalPath**: WeFlow 扁平字段 ``mediaLocalPath`` 是客户端机器本地路径
     (如 ``C:\\cache\\x.jpg``), 归一化可能把它当 ``media_url``。scheme 判断会跳过。

用户决策: 落盘 image/emoji/file + appmsg文件型; **不落 video/voice**; 单文件 50MB 上限。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# 要落盘的 msg_type(字符串, raw_message_parser 归一化后)。用户决策: 不含 video/voice。
# appmsg 作为候选进入, _resolve_download_url 内部按子类型(file)过滤。
_DOWNLOADABLE_TYPES: frozenset[str] = frozenset({"image", "emoji", "file", "appmsg"})

_DEFAULT_TIMEOUT: float = 60.0
_DEFAULT_CONCURRENCY: int = 8
_DEFAULT_MAX_BYTES: int = 50 * 1024 * 1024  # 50MB — 用户决策
_USER_AGENT: str = "Winnow-MediaDownloader/1.0"

# 文件名清洗: 去掉路径分隔符/控制字符/非法字符
_UNSAFE_NAME_RE = re.compile(r'[\\/\x00-\x1f<>:"|?*]')
_MAX_NAME_LEN: int = 120


# ============================================================
# Helpers: classification + URL/filename resolution
# ============================================================


def _is_candidate(msg: dict[str, Any]) -> bool:
    """消息是否是下载候选(类型层面)。appmsg 统一候选, 子类型在 _resolve 时细分。"""
    mt = str(msg.get("msg_type", ""))
    return mt in _DOWNLOADABLE_TYPES


def _parse_appmsg(msg: dict[str, Any]) -> Any:
    """解析 appmsg raw_content。返回 AppMsgInfo 或 None。"""
    raw = msg.get("raw_content") or msg.get("raw_json") or ""
    if not raw or not isinstance(raw, str):
        return None
    try:
        from z_winnow.content_enrich.card_parser import try_parse_appmsg_safe

        return try_parse_appmsg_safe(raw, 49)
    except Exception as exc:  # 卡片解析全程容错
        logger.debug("media: appmsg parse failed: %s", exc)
        return None


def _extract_fileupload_title(msg: dict[str, Any]) -> str:
    """从 file 消息的 raw_content <fileupload><title> 提取原文件名。"""
    raw = msg.get("raw_content") or ""
    if not raw or not raw.strip().startswith("<"):
        return ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    fu = root.find("fileupload")
    if fu is None and root.tag == "fileupload":
        fu = root
    if fu is None:
        return ""
    el = fu.find("title")
    if el is not None and el.text:
        return el.text.strip()
    return ""


def _raw_json_filename(msg: dict[str, Any]) -> str:
    """从 raw_json 反查 WeFlow 原始 mediaFileName(归一化时已丢弃此字段)。"""
    rj = msg.get("raw_json")
    if not rj:
        return ""
    try:
        d = json.loads(rj) if isinstance(rj, str) else rj
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(d, dict):
        return str(d.get("mediaFileName") or d.get("fileName") or "")
    return ""


def _sanitize_filename(name: str) -> str:
    """清洗文件名: 去非法字符, 限长, 兜底 'media'。"""
    name = _UNSAFE_NAME_RE.sub("_", name or "").strip(" ._")
    if not name:
        return "media"
    if len(name) > _MAX_NAME_LEN:
        root, ext = os.path.splitext(name)
        name = root[: max(1, _MAX_NAME_LEN - len(ext))] + ext
    return name


def _rewrite_bind_host(url: str, base_url: str) -> str:
    """``0.0.0.0`` (any-interface 绑定哨兵) 永远不可达 → 用 base_url 的 host:port 替换。

    数据源服务端常绑定 ``0.0.0.0`` 并在媒体 URL 里广告它 (如 WeFlow ``imageCachePath``
    ``http://0.0.0.0:5031/...``), 客户端无法连接。这里把 host 为 ``0.0.0.0`` 的绝对
    URL 改写成配置的 ``base_url`` 的可达 host:port (含端口), 其余 host 原样返回 ——
    可能是合法的 CDN / 内网地址, 不能误伤。仅 ``0.0.0.0`` 这一确定不可达的哨兵才改。
    """
    if not url or not base_url:
        return url
    parsed = urlparse(url)
    if parsed.hostname != "0.0.0.0":
        return url
    base_parsed = urlparse(base_url)
    if not base_parsed.hostname:
        return url
    netloc = base_parsed.hostname
    if base_parsed.port:
        netloc = f"{base_parsed.hostname}:{base_parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _resolve_download_url(msg: dict[str, Any], base_url: str) -> tuple[str, str, str]:
    """解析消息的可下载 URL + 文件名提示。

    Returns:
        ``(url, filename_hint, kind)`` — url 为空表示不可下载(跳过)。
        kind 用于日志(image/emoji/file/appmsg-file)。
    """
    mt = str(msg.get("msg_type", ""))
    media_url = str(msg.get("media_url") or "")
    filename = ""

    # ① media_url: HTTP URL 直接用; 相对路径拼 base_url; 本地路径(无 http(s) scheme) 跳过
    url = ""
    if media_url:
        scheme = urlparse(media_url).scheme.lower()
        if scheme in ("http", "https"):
            url = media_url
        elif media_url.startswith("/") and base_url:
            url = base_url.rstrip("/") + media_url
        # else: 本地路径(如 C:\\...) 或无 scheme → url 保持空

    # ② appmsg: media_url 常空(归一化丢), 从 <appmsg><url> 取; 仅 file 子类型下载
    if mt == "appmsg":
        info = _parse_appmsg(msg)
        if info is None or info.msg_type != "file":
            return "", "", "appmsg-other"  # article/miniprogram/... 不下载
        if not url and info.url and urlparse(info.url).scheme.lower() in ("http", "https"):
            url = info.url
        if info.title:
            filename = info.title
        if not url:
            return "", "", "appmsg-file-no-url"
        return _rewrite_bind_host(url, base_url), filename, "appmsg-file"

    # 文件名 fallback 链
    if not filename:
        filename = _extract_fileupload_title(msg)  # file 的 <fileupload><title>
    if not filename:
        filename = _raw_json_filename(msg)  # WeFlow mediaFileName
    if not filename and url:
        filename = os.path.basename(urlparse(url).path)

    return _rewrite_bind_host(url, base_url), filename, mt


# ============================================================
# Single-file download (streaming + sha256 + size cap)
# ============================================================


def _rm(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


async def _download_one(
    client: httpx.AsyncClient,
    url: str,
    dest_dir: str,
    filename_hint: str,
    max_bytes: int,
    timeout: float,
) -> str | None:
    """流式下载单个 URL 到 dest_dir。成功返回最终绝对路径, 失败返回 None。

    边写临时文件边算 sha256 + 累计字节; 超 max_bytes 中止删除; 完成后按
    ``{sha256[:12]}_{文件名}`` 重命名(内容寻址去重)。临时文件清理全部走 finally。
    """
    tmp_path: str | None = None
    try:
        hasher = hashlib.sha256()
        oversize = False
        total = 0
        async with client.stream("GET", url, timeout=timeout) as resp:
            if resp.status_code >= 400:
                logger.debug("media download %s: HTTP %d", url, resp.status_code)
                return None
            fd, tmp_path = tempfile.mkstemp(prefix="md_dl_", dir=dest_dir)
            with os.fdopen(fd, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        oversize = True
                        break
                    hasher.update(chunk)
                    f.write(chunk)
        if oversize:
            logger.info("media download %s: exceeds %d bytes, aborted", url, max_bytes)
            return None
        digest = hasher.hexdigest()[:12]
        root, ext = os.path.splitext(filename_hint or "media")
        final_name = f"{digest}_{_sanitize_filename(root)}{ext or ''}"
        final_path = os.path.join(dest_dir, final_name)
        if os.path.exists(final_path):
            logger.debug("media download %s: dedup hit %s", url, final_name)
            return final_path  # tmp_path 由 finally 清理
        os.rename(tmp_path, final_path)
        tmp_path = None  # rename 成功, 临时文件已不存在
        logger.info("media download %s: saved %s (%d bytes)", url, final_name, total)
        return final_path
    except (TimeoutError, httpx.HTTPError, OSError) as exc:
        logger.warning("media download %s failed: %s", url, exc)
        return None
    finally:
        # 清理未完成的临时文件(rename 成功时已置 None; dedup/异常时仍指向临时文件)
        if tmp_path is not None:
            _rm(tmp_path)


# ============================================================
# Batch entry point
# ============================================================


async def download_media_batch(
    messages: list[dict[str, Any]],
    dest_dir: str,
    *,
    base_url: str = "",
    token: str = "",
    max_bytes: int = _DEFAULT_MAX_BYTES,
    timeout: float = _DEFAULT_TIMEOUT,
    concurrency: int = _DEFAULT_CONCURRENCY,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """批量下载媒体文件到 dest_dir。

    Args:
        messages: raw_message_parser 输出的消息 dict 列表(含 msg_type/media_url/raw_content)。
        dest_dir: 落盘目录(自动创建), 通常 ``{layer3_output_dir}/{group_id}/{date}/attachments``。
        base_url: media_url 为相对路径时拼接用(CipherTalk/WeFlow 服务地址);
            亦用于把媒体 URL 里的 ``0.0.0.0`` 绑定哨兵改写成可达 host(见 _rewrite_bind_host)。
        token: 数据源鉴权 token(WeFlow/CipherTalk), 非空时随请求带 ``Authorization: Bearer``。
            媒体端点与消息 API 共用同一 token, 缺它返回 401。
        max_bytes: 单文件大小上限, 超过则中止删除(默认 50MB)。
        timeout: 单请求超时秒数。
        concurrency: 并发下载数。

    Returns:
        ``{server_id: 本地绝对路径}`` — 仅成功下载的消息。失败/跳过项不出现。
        永不抛异常(单文件失败隔离)。
    """
    if not messages:
        return {}

    os.makedirs(dest_dir, exist_ok=True)

    # 预筛选 + URL 解析
    targets: list[tuple[str, str, str]] = []  # (server_id, url, filename_hint)
    skipped = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        sid = str(m.get("server_id") or "")
        if not sid or not _is_candidate(m):
            continue
        url, fname, _kind = _resolve_download_url(m, base_url)
        if not url:
            skipped += 1
            continue
        targets.append((sid, url, fname))

    if skipped:
        logger.debug("media download: %d messages skipped (no resolvable URL)", skipped)
    if not targets:
        return {}

    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client_kwargs: dict[str, Any] = {"headers": headers, "follow_redirects": True}
    if _transport is not None:
        client_kwargs["transport"] = _transport  # test injection

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def worker(t: tuple[str, str, str]) -> tuple[str, str] | None:
            sid, url, fname = t
            async with sem:
                path = await _download_one(client, url, dest_dir, fname, max_bytes, timeout)
                return (sid, path) if path else None

        results = await asyncio.gather(*(worker(t) for t in targets), return_exceptions=True)

    out: dict[str, str] = {}
    for r in results:
        if isinstance(r, tuple) and r[1]:
            out[r[0]] = r[1]
        elif isinstance(r, Exception):
            logger.warning("media download worker error: %s", r)

    logger.info(
        "media download: %d/%d files saved to %s",
        len(out),
        len(targets),
        dest_dir,
    )
    return out


__all__ = ["download_media_batch"]
