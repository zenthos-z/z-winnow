"""storage — 对象存储客户端（Cloudflare R2 / S3 兼容）.

附件上传到私有 R2 桶；resource 记 cloud_key；MCP serve 时按 cloud_key 生成
短期预签名 cloud_url（私有桶不公开暴露）。
"""

from z_winnow.object_storage.r2 import (
    is_r2_configured,
    presign_resource_urls,
    r2_key_for,
    reset_client_cache,
    upload_resources,
)

__all__ = [
    "is_r2_configured",
    "presign_resource_urls",
    "r2_key_for",
    "reset_client_cache",
    "upload_resources",
]
