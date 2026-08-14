"""CipherTalk API 异步客户端.

从 CipherTalk HTTP API (http://127.0.0.1:5031/v1) 获取聊天数据的异步客户端。

核心方法:
- fetch_messages(date) → List[Dict]   按日期拉取原始消息（不做解析）
- fetch_message_by_id(server_id) → Optional[Dict]  单条回溯
- find_group_session(group_name) → Optional[dict]  群聊查找
- get_group_members(chatroom_id) → dict  群成员获取（via contacts API）

特性:
- httpx.AsyncClient + 30s 超时
- 指数退避重试 (3 次)
- L1 层只做最小字段映射，所有解析由 L2 raw_message_parser 完成
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx

# Re-export from raw_message_parser for backward compatibility
# 其他模块可能从 cipher_talk_client 导入这些常量
from z_winnow.content_enrich.raw_message_parser import (  # noqa: F401
    MESSAGE_KIND_MAP,
    RECALL_CONTENT_PATTERNS,
    _map_message_kind,
    _strip_wxid_prefix,
)

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

CST = timezone(timedelta(hours=8))

DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3
BASE_BACKOFF = 1.0

# 模块级缓存 — 跨 client 实例存活，TTL 过期自动刷新
_GROUP_MEMBERS_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_GROUP_MEMBERS_CACHE_TTL: float = 3600.0  # 1 hour
_CONTACT_LOOKUP_CACHE: dict[str, str] = {}

# ============================================================
# CipherTalk API 客户端
# ============================================================


class CipherTalkClient:
    """CipherTalk HTTP API 异步客户端。

    与 legacy WeFlowClient 鸭子类型兼容，提供相同的 fetch_messages() 输出格式。
    下游代码 (ingest, data_fetch, builder) 无需修改。

    Usage:
        async with CipherTalkClient() as client:
            messages = await client.fetch_messages(group_name="...", date="20260428")
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5031",
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.token: str = token
        self.timeout: float = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers: dict[str, str] = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> CipherTalkClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ============================================================
    # HTTP 调用 + 重试
    # ============================================================

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        client = await self._get_client()
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                if method.upper() == "GET":
                    resp = await client.get(path, **kwargs)
                elif method.upper() == "POST":
                    resp = await client.post(path, **kwargs)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                if resp.status_code >= 500 or resp.status_code >= 400:
                    resp.raise_for_status()

                return resp

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    wait = BASE_BACKOFF * (2**attempt)
                    logger.warning(
                        "CipherTalk request attempt %d/%d failed: %s. Retrying in %.1fs...",
                        attempt + 1,
                        MAX_RETRIES + 1,
                        e,
                        wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "CipherTalk request failed after %d attempts: %s",
                        MAX_RETRIES + 1,
                        e,
                    )
                    raise

            except httpx.HTTPStatusError as e:
                logger.error(
                    "CipherTalk HTTP error: %s %s → %d", method, path, e.response.status_code
                )
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected: retry loop exited without exception")

    # ============================================================
    # API 方法
    # ============================================================

    async def health_check(self) -> bool:
        try:
            resp = await self._request_with_retry("GET", "/v1/health")
            data: dict[str, Any] = resp.json()
            return bool(data.get("success", False))
        except Exception:
            return False

    async def get_sessions(self) -> list[dict[str, Any]]:
        """GET /v1/sessions — 获取所有会话列表（分页拉全）.

        CipherTalk 默认每页 100 条且 hasMore=true；不分页会漏掉第 100 条之后的
        会话（含部分群聊）。这里按 limit/offset 翻页直到 hasMore=false。
        """
        sessions: list[dict[str, Any]] = []
        seen_usernames: set[str] = set()
        page_size = 200
        offset = 0
        pages_fetched = 0
        for _ in range(50):  # 安全上限 ~10000 会话，避免异常时死循环
            resp = await self._request_with_retry(
                "GET", "/v1/sessions", params={"limit": page_size, "offset": offset}
            )
            data: dict[str, Any] = resp.json()
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            batch = body.get("sessions", []) if isinstance(body, dict) else []
            if not batch:
                break
            # 去重保护：如果本页全部是已见过的 username，说明分页失效，停止
            new_in_batch = 0
            for s in batch:
                u = s.get("username", "")
                if u and u not in seen_usernames:
                    seen_usernames.add(u)
                    new_in_batch += 1
                    sessions.append(s)
            pages_fetched += 1
            if new_in_batch == 0:
                logger.warning(
                    "get_sessions: page %d returned %d items, all duplicates — stopping",
                    pages_fetched,
                    len(batch),
                )
                break
            # hasMore 可能在 data.data 内层，也可能在 data 顶层（兼容两种格式）
            has_more = False
            if isinstance(body, dict):
                has_more = bool(body.get("hasMore", False))
            if not has_more and isinstance(data, dict):
                has_more = bool(data.get("hasMore", False))
            if not has_more or len(batch) < page_size:
                break
            offset += page_size
        logger.info(
            "get_sessions: fetched %d pages, %d total sessions",
            pages_fetched,
            len(sessions),
        )
        return sessions

    async def find_session_by_room_id(self, room_id: str) -> dict[str, Any] | None:
        sessions = await self.get_sessions()
        for s in sessions:
            if s.get("username") == room_id:
                return dict(s)
        return None

    async def find_group_session(self, group_name: str) -> dict[str, Any] | None:
        """查找指定群聊的 session 信息."""
        sessions = await self.get_sessions()
        for s in sessions:
            if s.get("displayName") == group_name and "@chatroom" in s.get("username", ""):
                return dict(s)
        return None

    async def lookup_contact(self, wxid: str) -> dict[str, Any] | None:
        """GET /v1/contacts?q={wxid} — 按 wxid 查询单个联系人.

        q 参数匹配 username、displayName、remark、nickname 四个字段，
        用 wxid 搜可直接命中。
        返回联系人 dict 或 None。
        """
        # 检查模块级缓存
        if wxid in _CONTACT_LOOKUP_CACHE:
            return {"username": wxid, "displayName": _CONTACT_LOOKUP_CACHE[wxid]}

        try:
            resp = await self._request_with_retry(
                "GET",
                "/v1/contacts",
                params={"q": wxid, "limit": 1},
            )
            data = resp.json()
            contacts = data.get("data", {}).get("contacts", [])
            if contacts:
                return dict(contacts[0])
        except Exception as exc:
            logger.warning("lookup_contact failed for %s — %s", wxid, exc)
        return None

    async def build_member_map_from_messages(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, str]:
        """从消息列表中提取唯一 wxid，逐个反查联系人获取昵称.

        只查询消息中实际出现且昵称不可读的 wxid（跳过已有 display_name 的）。

        返回 {wxid: displayName} 映射。
        """
        wxids_to_lookup: set[str] = set()
        for msg in messages:
            account_name = msg.get("account_name", "")
            sender = msg.get("sender", "")
            if sender and (
                not account_name
                or account_name == sender
                or account_name.startswith("wxid_")
                or account_name.endswith("@openim")
            ):
                wxids_to_lookup.add(sender)

        if not wxids_to_lookup:
            return {}

        member_map: dict[str, str] = {}
        for wxid in wxids_to_lookup:
            if wxid in _CONTACT_LOOKUP_CACHE:
                member_map[wxid] = _CONTACT_LOOKUP_CACHE[wxid]
                continue
            contact = await self.lookup_contact(wxid)
            if contact:
                display = (
                    contact.get("displayName", "")
                    or contact.get("remark", "")
                    or contact.get("nickname", "")
                    or wxid
                )
                member_map[wxid] = display
                _CONTACT_LOOKUP_CACHE[wxid] = display
        return member_map

    async def get_group_members(self, chatroom_id: str) -> dict[str, Any]:
        """获取群成员名称映射.

        优先级:
        1. 模块级按群缓存（TTL 1 小时）
        2. /api/v1/group-members API（单次调用获取全部群成员）
        3. Fallback: 逐个 wxid 反查 /v1/contacts?q={wxid}

        返回格式: {"members": [{wxid, group_nickname, displayName, ...}, ...]}.
        P014: 永不抛出异常阻断管道.
        """
        import time

        # 检查缓存
        cached = _GROUP_MEMBERS_CACHE.get(chatroom_id)
        if cached and (time.monotonic() - cached[0]) < _GROUP_MEMBERS_CACHE_TTL:
            members_list = [{"wxid": k, "displayName": v} for k, v in cached[1].items()]
            return {"members": members_list}

        # 尝试 group-members API
        try:
            resp = await self._request_with_retry(
                "GET",
                "/api/v1/group-members",
                params={"chatroomId": chatroom_id},
            )
            data = resp.json()
            if data.get("success"):
                raw_members = data.get("members", [])
                cache_map: dict[str, str] = {}
                members: list[dict[str, Any]] = []
                for m in raw_members:
                    wxid = m.get("wxid", "")
                    display = (
                        m.get("groupNickname", "")
                        or m.get("displayName", "")
                        or m.get("remark", "")
                        or m.get("nickname", "")
                        or wxid
                    )
                    if wxid:
                        cache_map[wxid] = display
                        _CONTACT_LOOKUP_CACHE[wxid] = display
                        members.append(
                            {
                                "wxid": wxid,
                                "group_nickname": display,
                                "nickname": m.get("nickname", ""),
                                "remark": m.get("remark", ""),
                                "alias": m.get("alias", ""),
                                "displayName": display,
                            }
                        )
                _GROUP_MEMBERS_CACHE[chatroom_id] = (time.monotonic(), cache_map)
                logger.info(
                    "get_group_members: %d members via group-members API for %s",
                    len(members),
                    chatroom_id,
                )
                return {"members": members}
        except Exception as exc:
            logger.info(
                "group-members API not available for %s — %s, using per-wxid lookup",
                chatroom_id,
                exc,
            )

        # Fallback: 返回空，由调用方通过 build_member_map_from_messages 按需查询
        return {"members": []}

    async def get_messages(
        self,
        session_username: str,
        date: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
        offset: int = 0,
        *,
        room_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v1/messages — fetch messages for a chatroom.

        CipherTalk 使用 sessionId 参数 (vs legacy talker).
        返回 CipherTalk 原始格式消息 (含 parsedContent, messageKind, rawContent 等).
        """
        # Date → Unix seconds range
        if date and start_time is None and end_time is None:
            dt = datetime.strptime(date, "%Y%m%d").replace(tzinfo=CST)
            start_time = int(dt.timestamp())
            end_time = start_time + 86400

        params: dict[str, Any] = {
            "sessionId": session_username,
            "limit": limit,
            "offset": offset,
            "includeRaw": "true",
            "resolveMediaPath": "true",
            "resolveVoicePath": "true",
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        resp = await self._request_with_retry("GET", "/v1/messages", params=params)
        data: dict[str, Any] = resp.json()
        if data.get("success") and isinstance(data.get("data"), dict):
            return cast(list[dict[str, Any]], data["data"].get("messages", []))
        return []

    async def get_message_by_id(
        self,
        session_username: str,
        server_id: str,
    ) -> dict[str, Any] | None:
        """按 serverId 获取单条消息."""
        try:
            # CipherTalk messages API 不支持单条查询，需要过滤
            # 先尝试通过 get_messages 获取最近消息再过滤
            messages = await self.get_messages(
                session_username=session_username,
                limit=500,
            )
            for msg in messages:
                msg_sid = str(msg.get("serverId", ""))
                if msg_sid == server_id:
                    return msg
            return None
        except Exception:
            return None

    # ============================================================
    # 高层 API — 与 legacy WeFlowClient 输出格式兼容
    # ============================================================

    async def fetch_messages(
        self,
        group_name: str,
        date: str,
        *,
        group_id: str | None = None,
        chatroom_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch raw messages for a group and date.

        L1 层只做最轻量的字段映射，不做任何 XML 解析或内容清洗。
        所有解析由 L2 content_enrich 的 raw_message_parser 完成。

        输出字段:
            server_id, sender, account_name, group_nickname, timestamp,
            msg_type (原始 messageKind), content (原始 rawContent),
            original_content (空), media_url, media_local_path,
            reply_to (空), raw_content, raw_json (完整 API 响应)

        Args:
            group_name: 群聊显示名称（仅用于日志和兜底匹配）.
            date: 目标日期 YYYYMMDD
            group_id: 保留参数，不再内部使用.
            chatroom_id: session username (xxx@chatroom)，优先级最高.

        Returns:
            Raw message list (最小索引字段 + 完整原始 API 数据).
        """
        # 解析 chatroom_id
        if chatroom_id:
            session_username = chatroom_id
        elif "@chatroom" in group_name:
            session_username = group_name
        else:
            session = await self.find_group_session(group_name)
            if not session:
                raise ValueError(f"未找到群聊: {group_name}")
            session_username = session["username"]

        # Pagination: fetch all messages, up to max_total
        page_size = 1000
        max_total = 5000
        all_raw: list[dict[str, Any]] = []
        offset = 0

        while len(all_raw) < max_total:
            batch = await self.get_messages(
                session_username=session_username,
                date=date,
                limit=page_size,
                offset=offset,
            )
            all_raw.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        raw_messages = all_raw[:max_total]
        logger.info(
            "Pagination complete: fetched %d messages for %s on %s",
            len(raw_messages),
            session_username,
            date,
        )

        result: list[dict[str, Any]] = []
        for msg in raw_messages:
            metadata = msg.get("metadata", {})

            # 过滤系统消息
            if metadata.get("isSystem", False):
                continue

            # 仅提取最小索引字段，不做解析
            server_id = str(msg.get("serverId", ""))
            sender = str(msg.get("senderUsername", ""))
            message_kind = str(msg.get("messageKind", "text"))
            timestamp = (
                int(msg.get("sortSeq", 0))
                or int(msg.get("createTimeMs", 0))
                or int(msg.get("createTime", 0)) * 1000
            )

            # 媒体路径
            media_info = msg.get("media", {})
            media_url = str(
                media_info.get("imageCachePath", "")
                or media_info.get("emojiCachePath", "")
                or media_info.get("videoPath", "")
                or media_info.get("filePath", "")
                or media_info.get("voicePath", "")
            )

            raw_content_raw = str(msg.get("rawContent", ""))
            raw_json_str = json.dumps(msg, ensure_ascii=False)

            result.append(
                {
                    "server_id": server_id,
                    "sender": sender,
                    "account_name": sender,
                    "group_nickname": sender,
                    "timestamp": timestamp,
                    "msg_type": message_kind,  # 原始 messageKind，不映射
                    "content": raw_content_raw,  # 原始 rawContent
                    "original_content": "",
                    "media_url": media_url,
                    "media_local_path": media_url,
                    "reply_to": "",  # L2 解析填充
                    "raw_content": raw_content_raw,
                    "raw_json": raw_json_str,  # 完整原始 API 响应
                }
            )

        logger.info(
            "Fetched %d raw messages for group '%s' on %s (%d total, %d filtered system)",
            len(result),
            group_name,
            date,
            len(raw_messages),
            len(raw_messages) - len(result),
        )

        return result

    async def check_messages_count(
        self,
        chatroom_id: str,
        date: str,
        *,
        limit: int = 1,
    ) -> tuple[bool, int]:
        """从 CipherTalk API 直接查询指定群+日期是否有消息（不访问本地 DB）。

        limit=1 时仅检查存在性（单次请求，快速返回），batch_scheduler 使用。
        limit>1 时翻页统计真实消息总数，data_preview source-check 使用。

        Args:
            chatroom_id: session username (xxx@chatroom)。
            date: 日期 YYYYMMDD。
            limit: ≤1 = 快速存在性检查（不翻页）；>1 = 翻页统计真实总数，
                   page_size 取 min(limit, 1000)，硬上限 5000。

        Returns:
            (has_data, count) — has_data 表示是否有消息，count 为真实总数。
        """
        if limit <= 1:
            # 快速存在性检查，不翻页
            messages = await self.get_messages(
                session_username=chatroom_id,
                date=date,
                limit=1,
                offset=0,
            )
            has_data = len(messages) > 0
            logger.debug(
                "check_messages_count (exists): chatroom=%s date=%s has_data=%s",
                chatroom_id,
                date,
                has_data,
            )
            return (has_data, len(messages))

        # 翻页统计真实总数
        page_size = min(limit, 1000)
        max_total = 5000
        total = 0
        offset = 0

        while total < max_total:
            messages = await self.get_messages(
                session_username=chatroom_id,
                date=date,
                limit=page_size,
                offset=offset,
            )
            total += len(messages)
            if len(messages) < page_size:
                break
            offset += page_size

        has_data = total > 0
        logger.debug(
            "check_messages_count (paginated): chatroom=%s date=%s has_data=%s count=%d",
            chatroom_id,
            date,
            has_data,
            total,
        )
        return (has_data, total)

    async def fetch_message_by_id(
        self,
        group_name: str,
        server_id: str,
    ) -> dict[str, Any] | None:
        """按 serverID 回溯单条消息."""
        session = await self.find_group_session(group_name)
        if not session:
            return None

        return await self.get_message_by_id(
            session_username=session["username"],
            server_id=server_id,
        )


def create_data_client(
    base_url: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CipherTalkClient:
    """Create data source client per Settings.data_source.

    按 settings.data_source 分支:
    - 'weflow'     → WeFlowClient (legacy /api/v1/)
    - 'ciphertalk' → CipherTalkClient (默认, /v1/)

    base_url/token 显式传入且非空时优先; 否则用 settings.effective_data_*
    (按 data_source 选 weflow_*/ciphertalk_*). 返回 CipherTalkClient 标注 —
    WeFlowClient 是其子类, 鸭子类型兼容, 调用方无感.
    """
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    source = (settings.data_source or "ciphertalk").lower().strip()

    eff_base = base_url or settings.effective_data_base_url
    eff_token = token or settings.effective_data_token

    if source == "weflow":
        from z_winnow.pipeline.weflow_client import WeFlowClient

        return WeFlowClient(base_url=eff_base, token=eff_token or "", timeout=timeout)

    return CipherTalkClient(base_url=eff_base, token=eff_token or "", timeout=timeout)
