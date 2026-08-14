"""WeFlow API 异步客户端 (legacy /api/v1/ 数据源).

WeFlowClient 继承 CipherTalkClient, 重写端点相关方法 (health_check /
get_sessions / get_messages), 在 get_messages() 里把 weflow 原始响应规范化为
CipherTalk API 格式 dict, 让 L2 content_enrich.raw_message_parser.parse_raw_message
无感复用 (L2 是 CipherTalk 格式专用).

weflow vs CipherTalk 差异:
- 端点:   /api/v1/*           vs  /v1/*
- 取消息: talker/start/end    vs  sessionId/startTime/endTime
- 响应:   data["messages"]    vs  data["data"]["messages"]
- 类型:   localType (数字)    vs  messageKind (字符串)
- 认证:   Bearer token (同 CipherTalk)

实测 (2026-07-09): weflow 消息字段 serverId/senderUsername/parsedContent/
rawContent/sortSeq(ms)/createTime(s) 与 CipherTalk 同名, 规范化大部分直接复制.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from z_winnow.content_enrich.raw_message_parser import _strip_wxid_prefix
from z_winnow.pipeline.cipher_talk_client import CipherTalkClient

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# ============================================================
# WeFlow 消息类型映射 (从 legacy weflow_client.py @ 2d76790^ 搬迁)
# ============================================================

# 标准数值类型 → 内部类型字符串 (用于 type/msgType 字段直接映射)
MSG_TYPE_MAP: dict[int, str] = {
    0: "text",
    1: "image",
    2: "voice",
    3: "video",
    4: "file",
    5: "emoji",
    7: "link",
    8: "location",
    20: "redpacket",
    21: "transfer",
    22: "poke",
    23: "call",
    24: "share",
    25: "reply",
    26: "forward",
    27: "contact",
    49: "appmsg",
    81: "recall",
    99: "other",
}

# Real API localType → 标准类型 (实测: 1=text, 3=image, 47=emoji, 43=video).
# 扩展 43:video — 避免 _detect_real_api_type 的 <msg> 兜底把视频归为 appmsg.
LOCAL_TYPE_MAP: dict[int, str] = {
    1: "text",
    3: "image",
    43: "video",
    47: "emoji",
}


def _detect_real_api_type(msg: dict[str, Any]) -> tuple[str, int]:
    """从 weflow 真实 API 响应检测消息类型.

    weflow 用 localType (非标准), 部分是复合大数 (如 244813135921=引用,
    8594229559345=红包). 检测策略:
      1. 标准 type/msgType → MSG_TYPE_MAP
      2. 已知 localType → LOCAL_TYPE_MAP
      3. 未知大数 localType → rawContent XML 结构检测
      4. content 文本回退 ([图片]/[视频] 等)

    Returns:
        (msg_type_str, type_code_int) — msg_type_str 直接作为 CipherTalk messageKind.
    """
    # Strategy 1: 标准 type/msgType 字段 (向后兼容)
    type_code = msg.get("type", msg.get("msgType"))
    if type_code is not None and isinstance(type_code, int | float):
        tc = int(type_code)
        if tc in MSG_TYPE_MAP:
            return MSG_TYPE_MAP[tc], tc

    # Strategy 2: 已知 localType 直接映射 (小数值)
    local_type = msg.get("localType")
    if local_type is not None and isinstance(local_type, int | float):
        lt = int(local_type)
        if lt in LOCAL_TYPE_MAP:
            return LOCAL_TYPE_MAP[lt], lt

    # Strategy 3: 未知大数 localType → rawContent XML 检测
    raw = _strip_wxid_prefix(str(msg.get("rawContent", "")))
    if raw.strip().startswith("<"):
        raw_lower = raw.lower()
        # 顺序敏感: refermsg 优先于 appmsg (引用 XML 用 <appmsg> 包裹 <refermsg>)
        # 返回 "quote" (CipherTalk messageKind) — L2 _map_message_kind("quote")="reply"
        # 走 reply 解析分支; 若返回 "reply" 则 MAP 无此 key → msg_type="other" 不走 reply 分支
        if "<refermsg" in raw_lower:
            return "quote", 25
        if "<appmsg" in raw_lower:
            # 小程序: <type>33</type> 或 <type>36</type>
            if "<type>33</type>" in raw_lower or "<type>36</type>" in raw_lower:
                return "weapp", 33
            return "appmsg", 49
        if "<fileupload" in raw_lower:
            return "file", 4
        if "<img" in raw_lower or "<image" in raw_lower:
            return "image", 1
        if "<msg>" in raw_lower:
            return "appmsg", 49

    # Strategy 4: content 文本回退
    content = str(msg.get("content", "")).strip()
    if content == "[图片]":
        return "image", 1
    if content in ("[动画表情]", "[表情]"):
        return "emoji", 5
    if content == "[文件]":
        return "file", 4
    if content.startswith("[语音]"):
        return "voice", 2
    if content.startswith("[视频]"):
        return "video", 3

    return "text", 0


# ============================================================
# 规范化: weflow 原始消息 → CipherTalk API 格式
# ============================================================

# messageKind → CipherTalk media dict 的键
_MEDIA_FIELD_BY_KIND: dict[str, str] = {
    "image": "imageCachePath",
    "emoji": "emojiCachePath",
    "video": "videoPath",
    "voice": "voicePath",
    "file": "filePath",
}


def _normalize_weflow_to_ciphertalk(msg: dict[str, Any]) -> dict[str, Any]:
    """weflow 原始消息 → CipherTalk API 格式 dict.

    让 L2 parse_raw_message 无感复用. 规范化 dict **不含下划线 account_name**
    (否则 raw_message_parser._is_already_parsed 误判为已清洗而跳过 L2, 但 L1
    输出非完整清洗格式会出错). 昵称靠 build_member_map enrich (和 CipherTalk 一致).

    weflow 字段与 CipherTalk 高度同名, 大部分直接复制; 仅需:
      - localType (数字) → messageKind (字符串, via _detect_real_api_type)
      - 扁平 mediaUrl/mediaLocalPath → 嵌套 media dict
      - 构造 metadata.isSystem (localType=10000 → True, 系统消息由 L1/L2 过滤)
    """
    msg_kind, _ = _detect_real_api_type(msg)
    is_system = msg.get("localType") == 10000

    # 媒体: weflow 扁平 → CipherTalk media dict (按 kind 选字段)
    media: dict[str, Any] = {}
    media_val = str(msg.get("mediaUrl", "") or msg.get("mediaPath", "") or "") or str(
        msg.get("mediaLocalPath", "") or ""
    )
    if media_val:
        media_field = _MEDIA_FIELD_BY_KIND.get(msg_kind)
        if media_field:
            media[media_field] = media_val

    # timestamp: sortSeq(ms) 优先, 兜底 createTime(s)*1000
    sort_seq = int(msg.get("sortSeq", 0) or 0)
    create_time = int(msg.get("createTime", 0) or 0)
    if not sort_seq and create_time:
        sort_seq = create_time * 1000

    return {
        "serverId": str(
            msg.get("serverId", "") or msg.get("platformMessageId", "") or msg.get("localId", "")
        ),
        "messageKind": msg_kind,
        "senderUsername": str(msg.get("senderUsername", "") or msg.get("sender", "") or ""),
        "parsedContent": str(msg.get("parsedContent", "") or msg.get("content", "")),
        "rawContent": str(msg.get("rawContent", "")),
        "sortSeq": sort_seq,
        "createTime": create_time,
        "media": media,
        "metadata": {"isSystem": is_system},
        "replyToMessageId": str(msg.get("replyToMessageId", "") or ""),
        # 驼峰 accountName (非下划线) — 仅供 raw_json 完整性, L2 不读它
        "accountName": str(msg.get("accountName", "") or msg.get("groupNickname", "") or ""),
    }


# ============================================================
# WeFlowClient
# ============================================================


class WeFlowClient(CipherTalkClient):
    """WeFlow HTTP API 异步客户端 (legacy /api/v1/).

    与 CipherTalkClient 鸭子类型兼容: fetch_messages / fetch_message_by_id /
    get_group_members / find_group_session / find_session_by_room_id /
    build_member_map_from_messages / lookup_contact 全部继承父类.
    只重写端点相关方法 (health_check / get_sessions / get_messages), 在
    get_messages 里把 weflow 响应规范化为 CipherTalk API 格式.

    Usage:
        async with WeFlowClient(base_url=..., token=...) as client:
            messages = await client.fetch_messages(group_name="...", date="20260709")
    """

    async def health_check(self) -> bool:
        """GET /api/v1/health — weflow 返回 {"status":"ok"}, 200 即健康."""
        try:
            resp = await self._request_with_retry("GET", "/api/v1/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def get_sessions(self) -> list[dict[str, Any]]:
        """GET /api/v1/sessions — weflow 分页拉全.

        weflow 支持 limit 但不支持 offset（offset 被忽略，返回相同数据）。
        策略：先用大 limit 一次拉全；若 count > 实际返回数，则说明服务端有上限，
        此时用 limit 尽可能拉最大窗口。
        """
        all_sessions: list[dict[str, Any]] = []
        seen_usernames: set[str] = set()
        # 先用大 limit 尝试一次拉全
        limit = 5000
        pages_fetched = 0
        total_count: int | None = None
        for _ in range(5):  # 最多 5 次尝试（不同 limit 值）
            try:
                resp = await self._request_with_retry(
                    "GET", "/api/v1/sessions",
                    params={"limit": limit, "offset": 0},
                )
                data: Any = resp.json()
            except Exception:
                logger.warning(
                    "WeFlow get_sessions: request failed (limit=%d)", limit,
                    exc_info=True,
                )
                break
            batch: list[dict[str, Any]] = []
            if isinstance(data, dict):
                if total_count is None:
                    total_count = data.get("count") if isinstance(data.get("count"), int) else None
                batch = data.get("sessions", [])
                if not isinstance(batch, list):
                    batch = []
                if not batch:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        b2 = inner.get("sessions", [])
                        if isinstance(b2, list):
                            batch = b2
            elif isinstance(data, list):
                batch = data
            if not batch:
                break
            new_in_batch = 0
            for s in batch:
                u = s.get("username", "")
                if u and u not in seen_usernames:
                    seen_usernames.add(u)
                    new_in_batch += 1
                    all_sessions.append(s)
            pages_fetched += 1
            # 如果 count <= 返回数，说明已经拉全
            if total_count is not None and total_count <= len(batch):
                break
            # 如果返回数 < limit，说明服务端已经到了上限
            if len(batch) < limit:
                break
            # 理论上不会到这里（offset=0 每次相同），但保留安全检查
            if new_in_batch == 0:
                break
        chatroom_count = sum(
            1 for s in all_sessions if (s.get("sessionType") or "") == "group"
            or "@chatroom" in (s.get("username") or "")
        )
        logger.info(
            "WeFlow get_sessions: %d pages, %d total (count=%s, limit=%d), %d groups",
            pages_fetched, len(all_sessions), total_count, limit, chatroom_count,
        )
        return all_sessions

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
        """GET /api/v1/messages — weflow 原始响应规范化为 CipherTalk 格式.

        weflow 参数: talker/start/end/media/image/emoji/room_id
        响应: data["messages"] 顶层 (vs CipherTalk data["data"]["messages"])
        返回: 规范化后的 CipherTalk API 格式 dict 列表 (供父类 fetch_messages 消费).
        """
        # Date → Unix 秒范围 (同父类逻辑)
        if date and start_time is None and end_time is None:
            dt = datetime.strptime(date, "%Y%m%d").replace(tzinfo=CST)
            start_time = int(dt.timestamp())
            end_time = start_time + 86400

        params: dict[str, Any] = {
            "talker": session_username,
            "limit": limit,
            "offset": offset,
            "media": "1",
            "image": "1",
            "emoji": "1",
        }
        if room_id:
            params["room_id"] = room_id
        if start_time is not None:
            params["start"] = start_time
        if end_time is not None:
            params["end"] = end_time

        resp = await self._request_with_retry("GET", "/api/v1/messages", params=params)
        data: dict[str, Any] = resp.json()
        if isinstance(data, dict):
            raw = data.get("messages", [])
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        return [_normalize_weflow_to_ciphertalk(m) for m in raw if isinstance(m, dict)]
