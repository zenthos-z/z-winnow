"""storage/r2.py — Cloudflare R2（S3 兼容）对象存储客户端.

附件（图片/PDF/文件）上传到**私有** R2 桶，resource 记 ``cloud_key``；
MCP serve 时按 ``cloud_key`` 生成短期预签名 ``cloud_url`` —— 私有桶不公开暴露，
泄漏的 URL 在 ``r2_presigned_expiry``（默认 1h）后失效，且需 MCP key 鉴权才能拿到。

设计要点：
- boto3 同步 → 全程 ``asyncio.to_thread``，137MB PDF 不阻塞 graph 事件循环
- HEAD 命中跳过 PUT（同 gid/date 重跑不重传）
- best-effort：单文件失败 log 跳过，不抛异常（与 content_enrich 降级风格一致）

Public API:
    - is_r2_configured: 凭证是否齐全（上传 + 预签名均需此为真）
    - r2_key_for: 算 R2 对象 key = attachments/{gid}/{date}/{filename}
    - upload_resources: 异步上传入口（pipeline hook + CLI 回填共用）
    - presign_resource_urls: MCP serve 时按 cloud_key 生成 cloud_url（in-place）
    - reset_client_cache: 测试用，清 client 缓存
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from z_winnow.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# boto3 S3 client 单例（进程级；凭证不变无需重建）
_client_cache: Any = None


def is_r2_configured(settings: Settings | None = None) -> bool:
    """R2 凭证是否齐全（endpoint + access_key + secret + bucket）。

    上传与预签名均需此为真。``r2_upload_enabled`` 是独立的「是否上传」开关
    （ECS 只读预签名场景下 upload_enabled=false 但凭证齐全 → 仍可 presign）。
    """
    s = settings or get_settings()
    return bool(
        (s.r2_endpoint or "").strip()
        and (s.r2_access_key_id or "").strip()
        and (s.r2_secret_access_key or "").strip()
        and (s.r2_bucket or "").strip()
    )


def _get_client() -> Any:
    """懒建/缓存 boto3 S3 client。未配置返 None。"""
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    s = get_settings()
    if not is_r2_configured(s):
        return None
    import boto3
    from botocore.config import Config

    config_kwargs: dict[str, Any] = {
        "signature_version": "s3v4",
        # 显式超时 + 重试上限：boto3 默认重试退避可达数分钟，best-effort 场景需快速失败
        "connect_timeout": 10,
        "read_timeout": 60,
        "retries": {"max_attempts": 3},
    }
    proxy = (s.r2_https_proxy or "").strip()
    if proxy:
        # 国内直连 R2 签名读常卡死（curl 裸 GET 通但 SDK 读超时），走代理后 0.9s 通
        config_kwargs["proxies"] = {"http": proxy, "https": proxy}
    _client_cache = boto3.client(
        "s3",
        endpoint_url=s.r2_endpoint.strip(),
        aws_access_key_id=s.r2_access_key_id.strip(),
        aws_secret_access_key=s.r2_secret_access_key.strip(),
        region_name="auto",
        config=Config(**config_kwargs),
    )
    return _client_cache


def reset_client_cache() -> None:
    """清 client 缓存（测试 monkeypatch settings 后重建）。"""
    global _client_cache
    _client_cache = None


def r2_key_for(group_id: str, date: str, filename: str) -> str:
    """R2 对象 key：``attachments/{group_id}/{date}/{filename}``（镜像本地 attachments 布局）。"""
    return f"attachments/{group_id}/{date}/{filename}"


def _safe_read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _safe_write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("r2: write json failed %s — %s", path, exc)


def _object_exists(client: Any, bucket: str, key: str) -> bool:
    """HEAD key；存在返 True，404/异常返 False（异常不抛）。"""
    from botocore.exceptions import ClientError

    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        logger.warning("r2: HEAD %s failed — %s", key, exc)
        return False
    except Exception as exc:
        logger.warning("r2: HEAD %s failed — %s", key, exc)
        return False


def _upload_object(client: Any, bucket: str, key: str, local_path: Path) -> None:
    ctype, _ = mimetypes.guess_type(local_path.name)
    extra_args: dict[str, Any] = {}
    if ctype:
        extra_args["ContentType"] = ctype
    client.upload_file(str(local_path), bucket, key, ExtraArgs=extra_args)


def _upload_resources_sync(
    resources_path: Path,
    group_id: str,
    date: str,
    *,
    dry_run: bool = False,
) -> int:
    """同步核心：读 resources.json → 上传 valid local_path 文件 → 回填 cloud_key → 写回。

    - 跳过：无 local_path / 文件不存在 / R2 已存在该 key
    - dry_run：只计待传数，不传不写
    - 返回：本次上传（或 dry_run 待传）的资源数
    """
    s = get_settings()
    if not s.r2_upload_enabled:
        return 0
    client = _get_client()
    if client is None:
        logger.warning("r2: not configured (endpoint/key/secret/bucket missing), skip upload")
        return 0
    if not resources_path.exists():
        return 0
    bucket = s.r2_bucket.strip()

    data = _safe_read_json(resources_path)
    resources = data.get("resources")
    if not isinstance(resources, list):
        return 0

    uploaded = 0
    changed = False
    for r in resources:
        if not isinstance(r, dict):
            continue
        local_path_str = str(r.get("local_path") or "")
        if not local_path_str:
            continue
        local_path = Path(local_path_str)
        if not local_path.is_file():
            continue
        key = r2_key_for(group_id, date, local_path.name)

        # 已记 cloud_key 且 R2 上确实存在 → 跳过（幂等）
        if r.get("cloud_key") == key and _object_exists(client, bucket, key):
            continue

        if dry_run:
            uploaded += 1
            continue

        try:
            if not _object_exists(client, bucket, key):
                _upload_object(client, bucket, key, local_path)
                logger.info("r2: uploaded %s → %s", local_path.name, key)
            r["cloud_key"] = key
            uploaded += 1
            changed = True
        except Exception as exc:
            logger.warning("r2: upload %s failed — %s", local_path.name, exc)

    if changed:
        _safe_write_json(resources_path, data)
    return uploaded


async def upload_resources(
    resources_path: Path,
    group_id: str,
    date: str,
    *,
    dry_run: bool = False,
) -> int:
    """异步入口（pipeline hook + CLI 共用）：to_thread 包同步实现，不阻塞事件循环。"""
    return await asyncio.to_thread(
        _upload_resources_sync, resources_path, group_id, date, dry_run=dry_run
    )


def presign_resource_urls(resources: list[Any]) -> None:
    """MCP serve 时调用：按每个 ``resource.cloud_key`` 生成短期预签名 ``cloud_url``（in-place）。

    - 凭证未配 / 无 cloud_key → 跳过（本地 web 仍走 local_url）
    - 纯本地计算（boto3 generate_presigned_url 不发网络请求）
    - ECS 场景：upload_enabled 可为 false，只要凭证齐就 presign
    """
    if not is_r2_configured():
        return
    client = _get_client()
    if client is None:
        return
    s = get_settings()
    bucket = s.r2_bucket.strip()
    ttl = max(60, int(s.r2_presigned_expiry or 3600))
    for r in resources:
        if not isinstance(r, dict):
            continue
        key = str(r.get("cloud_key") or "")
        if not key:
            continue
        try:
            r["cloud_url"] = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=ttl,
            )
        except Exception as exc:
            logger.debug("r2: presign %s failed — %s", key, exc)
