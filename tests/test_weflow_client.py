"""WeFlowClient + 规范化函数测试.

验证:
1. _normalize_weflow_to_ciphertalk 把 weflow 原始消息正确转为 CipherTalk API 格式
2. 规范化 dict 不含下划线 account_name (否则 _is_already_parsed 误判跳过 L2)
3. 规范化 dict 喂 L2 parse_raw_message 能正确解析 (端到端兼容)
4. WeFlowClient 的 health_check/get_sessions/get_messages 端点与参数正确

实测样本 (2026-07-09, 真实 weflow API): 文本(1)/图片(3)/引用(244813135921)/
系统(10000)/视频(43)/红包(8594229559345).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from z_winnow.content_enrich.raw_message_parser import parse_raw_message
from z_winnow.pipeline.weflow_client import (
    WeFlowClient,
    _normalize_weflow_to_ciphertalk,
)

# ============================================================
# 实测样本 (从真实 weflow API 响应截取/简化)
# ============================================================


def _text_msg() -> dict:
    return {
        "localId": 62,
        "serverId": "4795470301059486760",
        "localType": 1,
        "createTime": 1783588536,
        "sortSeq": 1783588536000,
        "isSend": 0,
        "senderUsername": "ietatg",
        "content": "应该是 好多好多年前的吧",
        "rawContent": "ietatg:\n应该是 好多好多年前的吧",
        "parsedContent": "应该是 好多好多年前的吧",
    }


def _image_msg() -> dict:
    return {
        "localId": 59,
        "serverId": "2231178810915289729",
        "localType": 3,
        "createTime": 1783587519,
        "sortSeq": 1783587519000,
        "isSend": 0,
        "senderUsername": "wxid_ttttttttttttt",
        "content": "[图片]",
        "rawContent": 'wxid_ttttttttttttt:\n<?xml version="1.0"?>\n<msg><img aeskey="abc" /></msg>',
        "parsedContent": "[图片]",
        "mediaType": "image",
        "mediaFileName": "x.jpg",
        "mediaUrl": "http://0.0.0.0:5031/api/v1/media/room/images/x.jpg",
        "mediaLocalPath": "C:\\cache\\x.jpg",
    }


def _reply_msg() -> dict:
    return {
        "localId": 54,
        "serverId": "1282159734916938305",
        "localType": 244813135921,
        "createTime": 1783569145,
        "sortSeq": 1783569145000,
        "isSend": 0,
        "senderUsername": "wxid_tttttttttttttt",
        "content": "每一张图只能有一个零标高",
        "rawContent": (
            'wxid_tttttttttttttt:\n<?xml version="1.0"?>\n<msg>'
            "<appmsg><title>每一张图只能有一个零标高</title></appmsg>"
            "<refermsg><svrid>5633956792435836630</svrid></refermsg></msg>"
        ),
        "parsedContent": "每一张图只能有一个零标高",
        "replyToMessageId": "5633956792435836630",
        "quote": {"platformMessageId": "5633956792435836630", "content": "[图片]"},
    }


def _system_msg() -> dict:
    return {
        "localId": 16,
        "serverId": "7737571342005415768",
        "localType": 10000,
        "createTime": 1783590183,
        "sortSeq": 1783590183000,
        "isSend": 0,
        "senderUsername": None,
        "content": '"黄雪娟"通过扫描二维码加入群聊',
        "rawContent": '"黄雪娟"通过扫描二维码加入群聊',
        "parsedContent": '"黄雪娟"通过扫描二维码加入群聊',
    }


def _video_msg() -> dict:
    return {
        "localId": 3,
        "serverId": "3823111430670180990",
        "localType": 43,
        "createTime": 1783515639,
        "sortSeq": 1783515639000,
        "isSend": 0,
        "senderUsername": "wxid_mgb208oyd27f22",
        "content": "[视频]",
        "rawContent": 'wxid_mgb208oyd27f22:\n<?xml version="1.0"?>\n<msg><videomsg aeskey="x" /></msg>',
        "parsedContent": "[视频]",
    }


def _redpacket_msg() -> dict:
    return {
        "localId": 24,
        "serverId": "7079611477674755853",
        "localType": 8594229559345,
        "createTime": 1783566081,
        "sortSeq": 1783566081000,
        "isSend": 0,
        "senderUsername": "wxid_mgb208oyd27f22",
        "content": "[红包]",
        "rawContent": "wxid_mgb208oyd27f22:\n<msg><appmsg><des>我给你发了一个红包</des></appmsg></msg>",
        "parsedContent": "[红包]",
    }


_ALL_FACTORIES = [_text_msg, _image_msg, _reply_msg, _system_msg, _video_msg, _redpacket_msg]


# ============================================================
# 规范化单元测试
# ============================================================


class TestNormalize:
    def test_text(self):
        n = _normalize_weflow_to_ciphertalk(_text_msg())
        assert n["messageKind"] == "text"
        assert n["serverId"] == "4795470301059486760"
        assert n["senderUsername"] == "ietatg"
        assert n["parsedContent"] == "应该是 好多好多年前的吧"
        assert n["sortSeq"] == 1783588536000
        assert n["metadata"]["isSystem"] is False

    def test_image_media_mapping(self):
        n = _normalize_weflow_to_ciphertalk(_image_msg())
        assert n["messageKind"] == "image"
        # weflow 扁平 mediaUrl → CipherTalk media.imageCachePath
        assert n["media"]["imageCachePath"] == "http://0.0.0.0:5031/api/v1/media/room/images/x.jpg"

    def test_reply_is_quote(self):
        """引用消息 → messageKind='quote' (CipherTalk messageKind, L2 映射为 reply 分支)."""
        n = _normalize_weflow_to_ciphertalk(_reply_msg())
        assert n["messageKind"] == "quote"
        assert n["replyToMessageId"] == "5633956792435836630"

    def test_system_is_system(self):
        n = _normalize_weflow_to_ciphertalk(_system_msg())
        assert n["metadata"]["isSystem"] is True

    def test_video_via_local_type_map(self):
        """localType=43 → 'video' (LOCAL_TYPE_MAP 扩展, 不走 <msg> 兜底)."""
        n = _normalize_weflow_to_ciphertalk(_video_msg())
        assert n["messageKind"] == "video"

    def test_redpacket_is_appmsg(self):
        """红包 (大数 localType + <appmsg>) → 'appmsg', 不丢消息."""
        n = _normalize_weflow_to_ciphertalk(_redpacket_msg())
        assert n["messageKind"] == "appmsg"

    def test_no_account_name_underscore(self):
        """规范化 dict 不含下划线 account_name (否则 _is_already_parsed 误判跳过 L2)."""
        for factory in _ALL_FACTORIES:
            n = _normalize_weflow_to_ciphertalk(factory())
            assert "account_name" not in n, f"{factory.__name__} 顶层含下划线 account_name"
            assert "account_name" not in json.dumps(n), (
                f"{factory.__name__} raw_json 含 account_name"
            )

    def test_server_id_fallback(self):
        """serverId 缺失时回退 platformMessageId / localId."""
        m = _text_msg()
        m.pop("serverId")
        m["platformMessageId"] = "plat-123"
        n = _normalize_weflow_to_ciphertalk(m)
        assert n["serverId"] == "plat-123"

    def test_timestamp_fallback_create_time(self):
        """sortSeq 缺失时用 createTime(s)*1000."""
        m = _text_msg()
        m["sortSeq"] = 0
        n = _normalize_weflow_to_ciphertalk(m)
        assert n["sortSeq"] == 1783588536 * 1000


# ============================================================
# L2 兼容测试 (规范化 dict 喂 parse_raw_message)
# ============================================================


class TestL2Compat:
    def test_text_parses(self):
        parsed = parse_raw_message(_normalize_weflow_to_ciphertalk(_text_msg()))
        assert parsed is not None
        assert parsed["msg_type"] == "text"
        assert "好多好多年" in parsed["content"]

    def test_image_parses(self):
        parsed = parse_raw_message(_normalize_weflow_to_ciphertalk(_image_msg()))
        assert parsed is not None
        assert parsed["msg_type"] == "image"

    def test_system_filtered_by_l2(self):
        """系统消息 metadata.isSystem=True → L2 return None (过滤)."""
        parsed = parse_raw_message(_normalize_weflow_to_ciphertalk(_system_msg()))
        assert parsed is None

    def test_reply_parses_as_reply(self):
        """引用消息 → L2 msg_type='reply' (走 reply 解析分支, 提取 svrid)."""
        parsed = parse_raw_message(_normalize_weflow_to_ciphertalk(_reply_msg()))
        assert parsed is not None
        assert parsed["msg_type"] == "reply"


# ============================================================
# WeFlowClient HTTP 方法测试 (mock _request_with_retry)
# ============================================================


class TestWeFlowClientHTTP:
    async def test_health_check_ok(self):
        client = WeFlowClient(base_url="http://fake", token="t")
        resp = MagicMock()
        resp.status_code = 200
        with patch.object(client, "_request_with_retry", new_callable=AsyncMock) as m:
            m.return_value = resp
            assert await client.health_check() is True
            m.assert_awaited_once_with("GET", "/api/v1/health")

    async def test_health_check_fail(self):
        client = WeFlowClient(base_url="http://fake", token="t")
        with patch.object(client, "_request_with_retry", new_callable=AsyncMock) as m:
            m.side_effect = Exception("conn refused")
            assert await client.health_check() is False

    async def test_get_sessions_reads_top_level(self):
        """weflow sessions 在顶层 data['sessions'] (非 CipherTalk 的 data['data']['sessions'])."""
        client = WeFlowClient(base_url="http://fake", token="t")
        resp = MagicMock()
        resp.json.return_value = {
            "success": True,
            "count": 2,
            "sessions": [
                {"username": "123@chatroom", "displayName": "群A"},
                {"username": "wxid_x", "displayName": "好友"},
            ],
        }
        with patch.object(client, "_request_with_retry", new_callable=AsyncMock) as m:
            m.return_value = resp
            sessions = await client.get_sessions()
            assert len(sessions) == 2
            assert sessions[0]["username"] == "123@chatroom"

    async def test_get_messages_normalizes_and_uses_talker(self):
        """get_messages 用 talker 参数 (非 sessionId), 返回规范化 dict."""
        client = WeFlowClient(base_url="http://fake", token="t")
        resp = MagicMock()
        resp.json.return_value = {
            "success": True,
            "messages": [_text_msg(), _image_msg()],
        }
        with patch.object(client, "_request_with_retry", new_callable=AsyncMock) as m:
            m.return_value = resp
            msgs = await client.get_messages(session_username="123@chatroom", limit=10)
            assert len(msgs) == 2
            assert msgs[0]["messageKind"] == "text"
            assert msgs[1]["messageKind"] == "image"
            # weflow 用 talker 参数 (vs CipherTalk sessionId)
            called_params = m.call_args.kwargs["params"]
            assert called_params["talker"] == "123@chatroom"
            assert "sessionId" not in called_params
            assert called_params["media"] == "1"

    async def test_get_messages_date_to_time_range(self):
        """date 参数 → start/end unix 秒 (weflow 用 start/end, 非 startTime/endTime)."""
        client = WeFlowClient(base_url="http://fake", token="t")
        resp = MagicMock()
        resp.json.return_value = {"success": True, "messages": []}
        with patch.object(client, "_request_with_retry", new_callable=AsyncMock) as m:
            m.return_value = resp
            await client.get_messages(session_username="r@chatroom", date="20260709")
            called_params = m.call_args.kwargs["params"]
            assert "start" in called_params
            assert "end" in called_params
            assert called_params["end"] - called_params["start"] == 86400

    async def test_inherits_fetch_messages_from_parent(self):
        """WeFlowClient 继承父类 fetch_messages — get_messages 返回规范化 dict 后由父类映射."""
        client = WeFlowClient(base_url="http://fake", token="t")
        # mock get_messages 返回规范化 dict (模拟父类 fetch_messages 调 get_messages)
        with patch.object(client, "get_messages", new_callable=AsyncMock) as m:
            m.return_value = [_normalize_weflow_to_ciphertalk(_text_msg())]
            # fetch_messages 会调 find_group_session (因 chatroom_id 直传跳过)
            result = await client.fetch_messages(
                group_name="g", date="20260709", chatroom_id="123@chatroom"
            )
            assert len(result) == 1
            assert result[0]["server_id"] == "4795470301059486760"
            assert result[0]["msg_type"] == "text"
            # raw_json 是规范化 dict, 不含下划线 account_name
            assert "account_name" not in result[0]["raw_json"]
