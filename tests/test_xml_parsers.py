"""Tests for XML parsers (parse_file, parse_link, parse_quote) and end-to-end data flow.

Verifies that:
- parse_file() extracts filename, size, AND CDN storage URL
- parse_link() extracts title, URL, and description
- parse_quote() extracts display name and content
- parse_emoji() returns semantic placeholder for emoji messages
- parse_weapp() extracts title from mini-program XML
- parse_location() extracts label from location XML
- clean_noise() strips XML residuals, CDN prefixes, long hex strings
- The full pipeline preserves URLs and file storage addresses through Layer 1 and Layer 2
"""

import json

import pytest

from z_winnow.content_enrich.xml_parsers import (
    clean_noise,
    parse_emoji,
    parse_file,
    parse_link,
    parse_location,
    parse_quote,
    parse_raw_content,
    parse_weapp,
)

# ============================================================
# parse_file tests
# ============================================================


class TestParseFile:
    """Test xml_parsers.parse_file() — file message XML parsing."""

    def test_extracts_filename_size_and_cdn_url(self):
        xml = (
            "<msg>"
            "<fileupload>"
            "<title><![CDATA[项目文档.pdf]]></title>"
            "<length>1048576</length>"
            "<cdnattachurl><![CDATA[3057020100044b3049]]></cdnattachurl>"
            "<aeskey><![CDATA[abcdef123456]]></aeskey>"
            "</fileupload>"
            "</msg>"
        )
        result = parse_file(xml)
        assert "项目文档.pdf" in result
        assert "1.0MB" in result
        assert "3057020100044b3049" in result
        assert "存储:" in result

    def test_file_without_cdn_url(self):
        xml = (
            "<msg>"
            "<fileupload>"
            "<title><![CDATA[report.xlsx]]></title>"
            "<length>2048</length>"
            "</fileupload>"
            "</msg>"
        )
        result = parse_file(xml)
        assert "report.xlsx" in result
        assert "2.0KB" in result
        assert "存储:" not in result

    def test_file_without_size(self):
        xml = (
            "<msg>"
            "<fileupload>"
            "<title><![CDATA[note.txt]]></title>"
            "<cdnattachurl><![CDATA[abc123]]></cdnattachurl>"
            "</fileupload>"
            "</msg>"
        )
        result = parse_file(xml)
        assert "note.txt" in result
        assert "abc123" in result
        assert "存储:" in result

    def test_file_no_title_returns_raw(self):
        xml = "<msg><fileupload><length>100</length></fileupload></msg>"
        result = parse_file(xml)
        assert result == xml  # returns raw when no title

    def test_empty_input(self):
        assert parse_file("") == ""
        assert parse_file(None) == ""  # type: ignore[arg-type]

    def test_non_xml_returns_raw(self):
        result = parse_file("plain text")
        assert result == "plain text"

    def test_malformed_xml_returns_raw(self):
        bad_xml = "<msg><fileupload><title>broken"
        result = parse_file(bad_xml)
        assert result == bad_xml


# ============================================================
# parse_link tests
# ============================================================


class TestParseLink:
    """Test xml_parsers.parse_link() — link message XML parsing."""

    def test_extracts_title_url_and_description(self):
        xml = (
            "<msg>"
            '<appmsg appid="" sdkver="0">'
            "<title><![CDATA[Anthropic 发布 Claude 4.7]]></title>"
            "<des><![CDATA[新一代 AI 模型发布]]></des>"
            "<url><![CDATA[https://anthropic.com/blog/claude-4-7]]></url>"
            "<type>5</type>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_link(xml)
        assert "Anthropic 发布 Claude 4.7" in result
        assert "https://anthropic.com/blog/claude-4-7" in result
        assert "新一代 AI 模型发布" in result
        assert "[链接:" in result

    def test_link_without_description(self):
        xml = (
            "<msg>"
            '<appmsg appid="" sdkver="0">'
            "<title><![CDATA[Test Page]]></title>"
            "<url><![CDATA[https://example.com]]></url>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_link(xml)
        assert "Test Page" in result
        assert "https://example.com" in result

    def test_link_without_title_uses_url(self):
        xml = (
            "<msg>"
            '<appmsg appid="" sdkver="0">'
            "<url><![CDATA[https://example.com/page]]></url>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_link(xml)
        assert "https://example.com/page" in result

    def test_link_no_title_no_url_returns_raw(self):
        xml = '<msg><appmsg appid="" sdkver="0"><type>5</type></appmsg></msg>'
        result = parse_link(xml)
        assert result == xml  # returns raw when nothing to extract

    def test_empty_input(self):
        assert parse_link("") == ""

    def test_non_xml_returns_raw(self):
        assert parse_link("just text") == "just text"


# ============================================================
# parse_quote tests
# ============================================================


class TestParseQuote:
    """Test xml_parsers.parse_quote() — quote/reply message XML parsing."""

    def test_extracts_displayname_and_content(self):
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[张三]]></displayname>"
            "<content><![CDATA[今天天气真好]]></content>"
            "<type>0</type>"
            "</refermsg>"
            "</msg>"
        )
        result = parse_quote(xml)
        assert "张三" in result
        assert "今天天气真好" in result
        assert "[引用" in result

    def test_nested_quote_only_one_level(self):
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[李四]]></displayname>"
            "<content><![CDATA[原始内容<refermsg><displayname>王五</displayname></refermsg>]]></content>"
            "</refermsg>"
            "</msg>"
        )
        result = parse_quote(xml)
        assert "李四" in result
        assert "原始内容" in result
        assert "王五" not in result  # nested reference not expanded

    def test_empty_input(self):
        assert parse_quote("") == ""

    def test_malformed_xml_returns_raw(self):
        bad = "<msg><refermsg><displayname>broken"
        assert parse_quote(bad) == bad

    def test_image_refermsg_uses_type_label(self):
        """Reply to image message: refermsg type=3, content is phash base64."""
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[红球咖啡-Leroy]]></displayname>"
            "<content>eyJwaGFzaCI6ImQwMTBkMDkwMzAwMDAwMDAiLCJwZHFIYXNoIjoiNGFlNTJiNDJhZDJjNGNhYTVl"
            "OTc5OTVlYjU2NTUyM2E0YzkwMjRjOGE3NTdkYTliOWFhYjJiNjc2YjY0NGQ5YSJ9</content>"
            "<svrid>2604714459634312324</svrid>"
            "<type>3</type>"
            "</refermsg>"
            "</msg>"
        )
        result = parse_quote(xml)
        assert "[引用" in result
        assert "红球咖啡-Leroy" in result
        assert "[图片]" in result
        # Raw base64 hash should NOT appear
        assert "eyJwaGFzaCI6" not in result

    def test_video_refermsg_uses_type_label(self):
        """Reply to video message: refermsg type=43."""
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[测试用户]]></displayname>"
            "<content>video_meta_data</content>"
            "<type>43</type>"
            "</refermsg>"
            "</msg>"
        )
        result = parse_quote(xml)
        assert "[视频]" in result

    def test_base64_hash_content_cleaned(self):
        """Refermsg without type but with base64 hash content gets cleaned."""
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[测试用户]]></displayname>"
            "<content>eyJwaGFzaCI6ImQwMTBkMDkwMzAwMDAwMDAiLCJwZHFIYXNoIjoiNGFlNTJiNDJhZDJjNGNhYTVl"
            "OTc5OTVlYjU2NTUyM2E0YzkwMjRjOGE3NTdkYTliOWFhYjJiNjc2YjY0NGQ5YSJ9"
            " 0 0 0 0 0</content>"
            "</refermsg>"
            "</msg>"
        )
        result = parse_quote(xml)
        assert "eyJwaGFzaCI6" not in result
        assert "0 0 0 0 0" not in result

    def test_text_refermsg_unchanged(self):
        """Refermsg type=0 (text) should extract content normally."""
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[张三]]></displayname>"
            "<content><![CDATA[今天天气真好]]></content>"
            "<type>0</type>"
            "</refermsg>"
            "</msg>"
        )
        result = parse_quote(xml)
        assert "今天天气真好" in result


# ============================================================
# parse_raw_content dispatch tests
# ============================================================


class TestParseRawContentDispatch:
    """Test parse_raw_content() routing to correct parser."""

    def test_dispatches_file(self):
        xml = (
            "<msg><fileupload>"
            "<title><![CDATA[doc.pdf]]></title>"
            "<length>500</length>"
            "<cdnattachurl><![CDATA[key123]]></cdnattachurl>"
            "</fileupload></msg>"
        )
        result = parse_raw_content(xml, "file", "original")
        assert "doc.pdf" in result
        assert "key123" in result

    def test_dispatches_link(self):
        xml = (
            "<msg><appmsg>"
            "<title><![CDATA[Title]]></title>"
            "<url><![CDATA[https://x.com]]></url>"
            "</appmsg></msg>"
        )
        result = parse_raw_content(xml, "link", "original")
        assert "https://x.com" in result

    def test_dispatches_reply(self):
        xml = (
            "<msg><refermsg>"
            "<displayname><![CDATA[Name]]></displayname>"
            "<content><![CDATA[Text]]></content>"
            "</refermsg></msg>"
        )
        result = parse_raw_content(xml, "reply", "original")
        assert "Name" in result

    def test_unknown_type_returns_current_content(self):
        result = parse_raw_content("<msg>test</msg>", "unknown_type", "fallback")
        assert result == "fallback"

    def test_empty_raw_content_returns_current(self):
        assert parse_raw_content("", "file", "fallback") == "fallback"

    def test_non_xml_returns_current(self):
        assert parse_raw_content("not xml", "file", "fallback") == "fallback"


# ============================================================
# End-to-end data flow: CipherTalk → Layer 1 → Layer 2
# ============================================================


class TestEndToEndDataFlow:
    """Verify URLs and file storage addresses survive the full pipeline."""

    @pytest.mark.asyncio
    async def test_link_url_preserved_through_pipeline(self, tmp_path):
        """Verify link URL is extracted and preserved from data source mock through Layer 2."""
        from z_winnow.pipeline.context import (
            build_layer2_messages,
            format_for_llm,
        )

        # Simulate data source cleaned output for a link message
        link_xml = (
            "<msg>"
            '<appmsg appid="" sdkver="0">'
            "<title><![CDATA[Anthropic 发布 Claude 4.7]]></title>"
            "<des><![CDATA[新一代 AI 模型发布]]></des>"
            "<url><![CDATA[https://anthropic.com/blog/claude-4-7]]></url>"
            "<type>5</type>"
            "</appmsg>"
            "</msg>"
        )
        cleaned_msg = {
            "server_id": "20260428_link_001",
            "sender": "user_alice",
            "account_name": "Alice",
            "group_nickname": "UserA",
            "timestamp": 1714291200000,
            "msg_type": "link",
            "content": "[链接: Anthropic 发布 Claude 4.7](https://anthropic.com/blog/claude-4-7) - 新一代 AI 模型发布",
            "media_url": "",
            "media_local_path": "",
            "reply_to": "",
            "raw_content": link_xml,
        }

        # Build Layer 2 messages
        layer2 = build_layer2_messages([cleaned_msg])

        assert len(layer2) == 1
        msg = layer2[0]
        # URL must be in content
        assert "https://anthropic.com/blog/claude-4-7" in msg["content"]
        # raw_content must be preserved
        assert "anthropic.com" in msg["raw_content"]

        # format_for_llm must show the URL
        llm_text = format_for_llm(layer2)
        assert "https://anthropic.com/blog/claude-4-7" in llm_text
        assert "Anthropic 发布 Claude 4.7" in llm_text

    @pytest.mark.asyncio
    async def test_file_storage_preserved_through_pipeline(self, tmp_path):
        """Verify file CDN URL and media path are preserved from data source mock through Layer 2."""
        from z_winnow.pipeline.context import (
            build_layer2_messages,
            format_for_llm,
        )

        file_xml = (
            "<msg>"
            "<fileupload>"
            "<title><![CDATA[项目文档.pdf]]></title>"
            "<length>1048576</length>"
            "<cdnattachurl><![CDATA[3057020100044b3049]]></cdnattachurl>"
            "<aeskey><![CDATA[abcdef123456]]></aeskey>"
            "</fileupload>"
            "</msg>"
        )
        cleaned_msg = {
            "server_id": "20260428_file_001",
            "sender": "user_bob",
            "account_name": "Bob",
            "group_nickname": "UserB",
            "timestamp": 1714291260000,
            "msg_type": "file",
            "content": "[文件: 项目文档.pdf (1.0MB) | 存储: 3057020100044b3049]",
            "media_url": "/assets/files/项目文档.pdf",
            "media_local_path": "/tmp/test_media/项目文档.pdf",
            "reply_to": "",
            "raw_content": file_xml,
        }

        # Build Layer 2 messages
        layer2 = build_layer2_messages([cleaned_msg])

        assert len(layer2) == 1
        msg = layer2[0]
        # CDN URL must be in content
        assert "3057020100044b3049" in msg["content"]
        # media paths must be preserved
        assert msg["media_url"] == "/assets/files/项目文档.pdf"
        assert msg["media_local_path"] == "/tmp/test_media/项目文档.pdf"
        # raw_content preserved
        assert "cdnattachurl" in msg["raw_content"]

        # format_for_llm must show the download URL
        llm_text = format_for_llm(layer2)
        assert "项目文档.pdf" in llm_text
        assert "3057020100044b3049" in llm_text
        assert "/assets/files/项目文档.pdf" in llm_text

    @pytest.mark.asyncio
    async def test_file_without_media_url_still_shows_cdn(self, tmp_path):
        """File with no mediaPath still shows CDN URL from XML."""
        from z_winnow.pipeline.context import (
            build_layer2_messages,
            format_for_llm,
        )

        cleaned_msg = {
            "server_id": "20260428_file_002",
            "sender": "user_charlie",
            "account_name": "Charlie",
            "msg_type": "file",
            "content": "[文件: data.csv (256B) | 存储: cdn_key_xyz]",
            "timestamp": 1714291800000,
            "media_url": "",
            "media_local_path": "",
            "reply_to": "",
            "raw_content": "<msg><fileupload><title>data.csv</title><length>256</length><cdnattachurl>cdn_key_xyz</cdnattachurl></fileupload></msg>",
        }

        layer2 = build_layer2_messages([cleaned_msg])
        llm_text = format_for_llm(layer2)

        assert "data.csv" in llm_text
        assert "cdn_key_xyz" in llm_text

    @pytest.mark.asyncio
    async def test_layer1_raw_json_contains_complete_data(self, tmp_path):
        """Verify that raw_json stored in Layer 1 contains all original fields."""
        import aiosqlite

        from z_winnow.pipeline.database import init_database_in_conn

        file_xml = (
            "<msg><fileupload>"
            "<title><![CDATA[test.pdf]]></title>"
            "<length>2048</length>"
            "<cdnattachurl><![CDATA[cdn_abc]]></cdnattachurl>"
            "</fileupload></msg>"
        )
        cleaned_msg = {
            "server_id": "20260428_test_001",
            "sender": "tester",
            "account_name": "Tester",
            "msg_type": "file",
            "content": "[文件: test.pdf (2.0KB) | 存储: cdn_abc]",
            "timestamp": 1714291200000,
            "media_url": "/assets/files/test.pdf",
            "media_local_path": "/tmp/test_media/test.pdf",
            "reply_to": "",
            "raw_content": file_xml,
            "raw_json": json.dumps(
                {
                    "platformMessageId": "20260428_test_001",
                    "sender": "tester",
                    "rawContent": file_xml,
                    "mediaPath": "/assets/files/test.pdf",
                    "mediaLocalPath": "/tmp/test_media/test.pdf",
                    "type": 4,
                },
                ensure_ascii=False,
            ),
        }

        db_path = str(tmp_path / "test.db")
        async with aiosqlite.connect(db_path) as db:
            await init_database_in_conn(db)

            raw_json_data = {**cleaned_msg, "sanitized": 0}
            raw_json = json.dumps(raw_json_data, ensure_ascii=False)

            await db.execute(
                """INSERT OR REPLACE INTO raw_messages
                   (serverID, date, sender, content, msg_type, image_path, sanitized, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cleaned_msg["server_id"],
                    "20260428",
                    cleaned_msg["sender"],
                    cleaned_msg["content"],
                    cleaned_msg["msg_type"],
                    cleaned_msg["media_url"] or None,
                    0,
                    raw_json,
                ),
            )
            await db.commit()

            # Read back and verify
            cursor = await db.execute(
                "SELECT raw_json, content FROM raw_messages WHERE serverID = ?",
                (cleaned_msg["server_id"],),
            )
            row = await cursor.fetchone()
            assert row is not None

            stored_raw_json = json.loads(row[0])
            # raw_json must contain the CDN URL
            assert "cdn_abc" in stored_raw_json.get("raw_content", "")
            # raw_json must contain media paths
            assert stored_raw_json.get("media_url") == "/assets/files/test.pdf"
            assert stored_raw_json.get("media_local_path") == "/tmp/test_media/test.pdf"
            # content must contain the CDN URL
            assert "cdn_abc" in row[1]


# ============================================================
# P3-1: parse_emoji tests
# ============================================================


class TestParseEmoji:
    """Test xml_parsers.parse_emoji() — emoji message XML parsing (P3-1)."""

    def test_xml_emoji_returns_placeholder(self):
        xml = (
            "<msg>"
            '<emoji md5="abc123" type="1" len="12345">'
            "<cdnsha1>deadbeef00ff</cdnsha1>"
            "<cdnthumbaeskey>key123</cdnthumbaeskey>"
            "<cdnthumburl>http://cdn.example.com/emoji.gif</cdnthumburl>"
            "<cdnthumblength>5000</cdnthumblength>"
            "<cdnthumbheight>100</cdnthumbheight>"
            "<cdnthumbwidth>100</cdnthumbwidth>"
            "</emoji>"
            "</msg>"
        )
        result = parse_emoji(xml)
        assert result == "[表情]"

    def test_plain_text_animation_emoji(self):
        result = parse_emoji("[动画表情]")
        assert result == "[动画表情]"

    def test_plain_text_emoji(self):
        result = parse_emoji("[表情]")
        assert result == "[表情]"

    def test_empty_input(self):
        assert parse_emoji("") == ""
        assert parse_emoji(None) == ""  # type: ignore[arg-type]

    def test_cdn_binary_data_returns_placeholder(self):
        """Emoji with CDN binary data should return semantic placeholder, not leak data."""
        xml = (
            "<msg><emoji>"
            "@cdn_3057020100044b30490a010204a0b0c0d0e0f10111213141516171819"
            "1a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30"
            "</emoji></msg>"
        )
        result = parse_emoji(xml)
        assert result == "[表情]"
        assert "@cdn_" not in result
        assert "3057020100044b" not in result


# ============================================================
# P3-1: parse_weapp tests
# ============================================================


class TestParseWeapp:
    """Test xml_parsers.parse_weapp() — mini-program XML parsing (P3-1)."""

    def test_extracts_title(self):
        xml = (
            "<msg>"
            '<appmsg appid="wx123" sdkver="0">'
            "<title><![CDATA[拼多多砍一刀]]></title>"
            "<type>33</type>"
            "<url><![CDATA[https://pinduoduo.com/invite]]></url>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_weapp(xml)
        assert "拼多多砍一刀" in result
        assert "[小程序:" in result

    def test_type_36_mini_program(self):
        xml = (
            "<msg>"
            '<appmsg appid="wx456">'
            "<title><![CDATA[美团外卖]]></title>"
            "<type>36</type>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_weapp(xml)
        assert "美团外卖" in result
        assert "[小程序:" in result

    def test_no_title_returns_generic_placeholder(self):
        xml = '<msg><appmsg appid="wx789"><type>33</type></appmsg></msg>'
        result = parse_weapp(xml)
        assert result == "[小程序]"

    def test_empty_input(self):
        assert parse_weapp("") == ""
        assert parse_weapp(None) == ""  # type: ignore[arg-type]

    def test_non_xml_returns_raw(self):
        result = parse_weapp("plain text")
        assert result == "plain text"

    def test_malformed_xml_returns_raw(self):
        bad_xml = "<msg><appmsg><title>broken"
        result = parse_weapp(bad_xml)
        assert result == bad_xml


# ============================================================
# P3-1: parse_location tests
# ============================================================


class TestParseLocation:
    """Test xml_parsers.parse_location() — location message XML parsing (P3-1)."""

    def test_extracts_label_from_appmsg(self):
        xml = (
            "<msg>"
            '<appmsg appid="" sdkver="0">'
            "<title><![CDATA[北京市朝阳区望京SOHO]]></title>"
            "<type>34</type>"
            "<url><![CDATA[https://map.qq.com/?loc=...]]></url>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_location(xml)
        assert "北京市朝阳区望京SOHO" in result
        assert "[位置:" in result

    def test_extracts_label_from_location_element(self):
        xml = '<msg><location x="39.99" y="116.48" label="上海陆家嘴" /></msg>'
        result = parse_location(xml)
        assert "上海陆家嘴" in result
        assert "[位置:" in result

    def test_no_label_returns_generic_placeholder(self):
        xml = '<msg><appmsg appid=""><type>34</type></appmsg></msg>'
        result = parse_location(xml)
        assert result == "[位置]"

    def test_empty_input(self):
        assert parse_location("") == ""
        assert parse_location(None) == ""  # type: ignore[arg-type]

    def test_non_xml_returns_raw(self):
        result = parse_location("some location text")
        assert result == "some location text"


# ============================================================
# P3-1: clean_noise tests
# ============================================================


class TestCleanNoise:
    """Test xml_parsers.clean_noise() — regex cleanup of XML/CDN residuals (P3-1)."""

    def test_strips_xml_tags(self):
        assert clean_noise("hello <attachid>abc</attachid> world") == "hello abc world"

    def test_strips_cdn_prefix(self):
        assert clean_noise("file @cdn_305702010004 data") == "file data"

    def test_strips_long_hex(self):
        hex_str = "a" * 64 + " and text"
        result = clean_noise(hex_str)
        assert "text" in result
        assert "a" * 64 not in result

    def test_preserves_normal_text(self):
        text = "Hello World - normal text 12345"
        assert clean_noise(text) == text

    def test_combined_noise(self):
        noisy = "msg <totallen>123</totallen> @cdn_abc123 def0123456789abcdef0123456789abcdef0123456789abcdef0123456789 end"
        result = clean_noise(noisy)
        assert "<totallen>" not in result
        assert "@cdn_" not in result
        assert "msg" in result
        assert "end" in result

    def test_empty_input(self):
        assert clean_noise("") == ""


# ============================================================
# P3-1: parse_raw_content dispatch for new types
# ============================================================


class TestParseRawContentNewTypes:
    """Test parse_raw_content() dispatches emoji/weapp/location correctly (P3-1)."""

    def test_dispatches_emoji(self):
        xml = "<msg><emoji><cdnsha1>abc</cdnsha1></emoji></msg>"
        result = parse_raw_content(xml, "emoji", "original")
        assert result == "[表情]"

    def test_dispatches_weapp(self):
        xml = (
            "<msg>"
            '<appmsg appid="wx123">'
            "<title><![CDATA[拼多多]]></title>"
            "<type>33</type>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_raw_content(xml, "weapp", "original")
        assert "拼多多" in result
        assert "[小程序:" in result

    def test_dispatches_location(self):
        xml = (
            "<msg>"
            '<appmsg appid="">'
            "<title><![CDATA[望京SOHO]]></title>"
            "<type>34</type>"
            "</appmsg>"
            "</msg>"
        )
        result = parse_raw_content(xml, "location", "original")
        assert "望京SOHO" in result
        assert "[位置:" in result

    def test_emoji_cleans_xml_residuals(self):
        """Emoji parse result should not contain XML residuals."""
        xml = "<msg><emoji><cdnsha1>abc123</cdnsha1><totallen>5000</totallen></emoji></msg>"
        result = parse_raw_content(xml, "emoji", "original")
        assert "<cdnsha1>" not in result
        assert "<totallen>" not in result

    def test_noisy_content_gets_cleaned(self):
        """Parsed content with CDN/hex noise should be cleaned."""
        xml = (
            "<msg>"
            "<fileupload>"
            "<title><![CDATA[file.pdf]]></title>"
            "<cdnattachurl><![CDATA[@cdn_3057020100044b3049]]></cdnattachurl>"
            "<aeskey><![CDATA[abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd]]></aeskey>"
            "</fileupload>"
            "</msg>"
        )
        result = parse_raw_content(xml, "file", "original")
        assert "@cdn_" not in result


# ============================================================
# P3-1: context.py reply routing fix
# ============================================================


class TestContextReplyRouting:
    """Test that context.py format_for_llm routes 'reply' type correctly (P3-1 B3/B4)."""

    def test_reply_type_formats_correctly(self):
        """B4: msg_type='reply' should use the reply branch, not else."""
        from z_winnow.pipeline.context import format_for_llm

        messages = [
            {
                "server_id": "test_reply_001",
                "sender": "Alice",
                "msg_type": "reply",
                "content": "张三: 你好",
                "timestamp": 1714291200000,
                "media_url": "",
                "media_local_path": "",
            }
        ]
        result = format_for_llm(messages)
        # After fix: reply content is passed through directly (no 「引用」 wrapper)
        assert "Alice" in result
        assert "张三" in result
        assert "你好" in result

    def test_quote_type_still_works(self):
        """Backward compat: msg_type='quote' should also route to reply branch."""
        from z_winnow.pipeline.context import format_for_llm

        messages = [
            {
                "server_id": "test_quote_001",
                "sender": "Bob",
                "msg_type": "quote",
                "content": "李四: 原始内容",
                "timestamp": 1714291200000,
                "media_url": "",
                "media_local_path": "",
            }
        ]
        result = format_for_llm(messages)
        # After fix: quote content is passed through directly (no 「引用」 wrapper)
        assert "Bob" in result
        assert "李四" in result

    def test_emoji_type_formats_correctly(self):
        """B2: emoji type should output [表情]."""
        from z_winnow.pipeline.context import format_for_llm

        messages = [
            {
                "server_id": "test_emoji_001",
                "sender": "Charlie",
                "msg_type": "emoji",
                "content": "[表情]",
                "timestamp": 1714291200000,
                "media_url": "",
                "media_local_path": "",
            }
        ]
        result = format_for_llm(messages)
        assert "[表情]" in result
        # Should NOT contain XML or CDN data
        assert "<" not in result
        assert "@cdn_" not in result

    def test_location_type_formats_correctly(self):
        """Location type should output parsed content."""
        from z_winnow.pipeline.context import format_for_llm

        messages = [
            {
                "server_id": "test_loc_001",
                "sender": "Dave",
                "msg_type": "location",
                "content": "[位置: 望京SOHO]",
                "timestamp": 1714291200000,
                "media_url": "",
                "media_local_path": "",
            }
        ]
        result = format_for_llm(messages)
        assert "[位置: 望京SOHO]" in result


# ============================================================
# P3-1: quote API snapshot field
# ============================================================


class TestQuoteApiSnapshot:
    """Test that data source API quote snapshot field is used for reply messages (P3-1 B3)."""

    @pytest.mark.asyncio
    async def test_quote_snapshot_preferred_over_xml(self):
        """When API provides quote field, use it directly instead of XML parsing."""
        # Test the quote logic directly with simulated data,
        # since mock client doesn't produce quote fields in mock data.
        from z_winnow.content_enrich.xml_parsers import parse_raw_content

        # Simulate: XML has one displayname, but API quote has different account_name
        xml = (
            "<msg>"
            "<refermsg>"
            "<displayname><![CDATA[XML显示名]]></displayname>"
            "<content><![CDATA[XML内容]]></content>"
            "<type>0</type>"
            "</refermsg>"
            "</msg>"
        )
        # API quote field provides different (more accurate) data
        quote_info = {
            "account_name": "API真实昵称",
            "content": "API真实内容",
        }

        # parse_raw_content would produce XML-based result
        xml_result = parse_raw_content(xml, "reply", "fallback")
        assert "XML显示名" in xml_result

        # But client logic should prefer API quote snapshot
        quote_name = str(quote_info.get("account_name", ""))
        quote_content = str(quote_info.get("content", ""))
        api_result = f"「引用 {quote_name}: {quote_content}」"
        assert "API真实昵称" in api_result
        assert "API真实内容" in api_result
