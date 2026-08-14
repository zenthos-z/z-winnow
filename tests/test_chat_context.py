"""Tests for ChatContextBuilder — markdown chat context generation."""

from __future__ import annotations

from z_winnow.pipeline.chat_context import ChatContextBuilder


def _make_msg(
    *,
    server_id: str = "1001",
    sender: str = "张三",
    account_name: str = "张三",
    timestamp: str = "1748151300000",  # ms timestamp, parsed to HH:MM:SS
    msg_type: str = "text",
    content: str = "你好",
    reply_to: str = "",
    media_url: str = "",
    media_local_path: str = "",
) -> dict:
    return {
        "server_id": server_id,
        "sender": sender,
        "account_name": account_name,
        "group_nickname": account_name,
        "timestamp": timestamp,
        "msg_type": msg_type,
        "content": content,
        "original_content": content,
        "media_url": media_url,
        "media_local_path": media_local_path,
        "reply_to": reply_to,
        "raw_content": "",
        "raw_json": {},
    }


class TestTextMessage:
    def test_formats_text_message(self):
        msg = _make_msg(content="讨论一下发布计划")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "###" in result
        assert "| 张三 | svrid:1001" in result
        assert "讨论一下发布计划" in result

    def test_header_contains_group_info(self):
        builder = ChatContextBuilder(
            [_make_msg()], group_name="GAiR", date="20260525", group_id="g_abc"
        )
        result = builder.build()
        assert "# 群聊记录 — GAiR — 2026-05-25" in result

    def test_footer_contains_message_count(self):
        msgs = [_make_msg(server_id=str(i)) for i in range(3)]
        builder = ChatContextBuilder(msgs)
        result = builder.build()
        assert "--- 共 3 条消息 ---" in result


class TestImageMessage:
    def test_image_with_description(self):
        msg = _make_msg(msg_type="image", content="[图片]")
        builder = ChatContextBuilder([msg], image_descriptions={"1001": "堆内存使用趋势截图"})
        result = builder.build()
        assert "堆内存使用趋势截图" in result
        assert "[图片:" not in result  # 描述直接替换 [图片]，不再包裹

    def test_image_without_description(self):
        msg = _make_msg(msg_type="image", content="[图片]")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[图片]" in result

    def test_text_with_image_placeholder_and_description(self):
        """核心 bug: msg_type=text + content=[图片] + 有描述 → 应替换为描述."""
        msg = _make_msg(msg_type="text", content="[图片]")
        builder = ChatContextBuilder(
            [msg], image_descriptions={"1001": "架构对比图，展示 Transformer vs CRATE"}
        )
        result = builder.build()
        assert "架构对比图，展示 Transformer vs CRATE" in result
        assert "[图片]" not in result


class TestReplyMessage:
    def test_reply_with_blockquote(self):
        original = _make_msg(
            server_id="2001", sender="李四", account_name="李四", content="原始消息内容"
        )
        reply = _make_msg(
            server_id="2002",
            sender="张三",
            account_name="张三",
            msg_type="reply",
            content="同意你的观点",
            reply_to="2001",
        )
        builder = ChatContextBuilder([original, reply])
        result = builder.build()
        assert "> 💬 引用 李四: 原始消息内容" in result
        assert "同意你的观点" in result

    def test_reply_reference_not_found(self):
        reply = _make_msg(msg_type="reply", content="回复内容", reply_to="nonexistent")
        builder = ChatContextBuilder([reply])
        result = builder.build()
        # Should not crash, just no blockquote
        assert "> 💬 引用" not in result
        assert "回复内容" in result

    def test_reply_truncates_long_reference(self):
        original = _make_msg(
            server_id="3001",
            account_name="王五",
            content="A" * 200,
        )
        reply = _make_msg(
            server_id="3002",
            msg_type="reply",
            content="简短回复",
            reply_to="3001",
        )
        builder = ChatContextBuilder([original, reply])
        result = builder.build()
        # Referenced content should be truncated
        assert '..." not in result' not in result  # sanity check
        quote_lines = [line for line in result.split("\n") if line.startswith("> 💬")]
        assert len(quote_lines) == 1
        assert "..." in quote_lines[0]


class TestEmojiMessage:
    def test_emoji_with_vision_description(self):
        msg = _make_msg(msg_type="emoji", content="", media_local_path="/tmp/sticker.png")
        builder = ChatContextBuilder([msg], image_descriptions={"1001": "一只猫无奈地摊手"})
        result = builder.build()
        assert "[表情包: 一只猫无奈地摊手]" in result

    def test_emoji_without_description_text_emoji(self):
        msg = _make_msg(msg_type="emoji", content="👍")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "👍" in result

    def test_emoji_without_description_no_content(self):
        msg = _make_msg(msg_type="emoji", content="")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[表情]" in result


class TestOtherTypes:
    def test_file_message(self):
        msg = _make_msg(msg_type="file", content="报告.pdf", media_url="http://cdn/file")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[文件: 报告.pdf]" in result

    def test_voice_message(self):
        msg = _make_msg(msg_type="voice", content="")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[语音]" in result

    def test_video_message(self):
        msg = _make_msg(msg_type="video", content="")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[视频]" in result

    def test_weapp_message(self):
        msg = _make_msg(msg_type="weapp", content="群接龙")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[小程序: 群接龙]" in result

    def test_location_message(self):
        msg = _make_msg(msg_type="location", content="北京市海淀区中关村")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "[位置: 北京市海淀区中关村]" in result


class TestSenderName:
    def test_uses_account_name(self):
        msg = _make_msg(sender="wxid_abc", account_name="张三")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        assert "| 张三 |" in result

    def test_fallback_to_sender_when_wxid(self):
        msg = _make_msg(sender="wxid_abc", account_name="wxid_abc")
        builder = ChatContextBuilder([msg])
        result = builder.build()
        # Both are wxid, should still show one of them
        assert "svrid:1001" in result


class TestEdgeCases:
    def test_empty_messages(self):
        builder = ChatContextBuilder([])
        result = builder.build()
        assert "--- 共 0 条消息 ---" in result

    def test_messages_ordered(self):
        msgs = [
            _make_msg(server_id="1", timestamp="1716617700"),  # 09:15
            _make_msg(server_id="2", timestamp="1716618000"),  # 09:20
        ]
        builder = ChatContextBuilder(msgs)
        result = builder.build()
        pos_1 = result.index("svrid:1")
        pos_2 = result.index("svrid:2")
        assert pos_1 < pos_2
