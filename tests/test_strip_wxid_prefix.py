"""Tests for _strip_wxid_prefix — CT.4.7 回归防护。

验证通用化的 sender 前缀剥离：wxid_、数字用户名、@openim 等格式。
"""

from __future__ import annotations

from z_winnow.content_enrich.raw_message_parser import _strip_wxid_prefix


class TestStripWxidPrefix:
    """CT.4.7: _strip_wxid_prefix 处理各种 sender 前缀格式。"""

    def test_wxid_prefix_stripped(self) -> None:
        assert _strip_wxid_prefix("wxid_abc123:\n<msg>data</msg>") == "<msg>data</msg>"

    def test_numeric_username_stripped(self) -> None:
        assert _strip_wxid_prefix("l333308:\n<msg>data</msg>") == "<msg>data</msg>"

    def test_openim_username_stripped(self) -> None:
        assert _strip_wxid_prefix("25984983287196487@openim:\n<msg>data</msg>") == "<msg>data</msg>"

    def test_no_prefix_passthrough(self) -> None:
        assert _strip_wxid_prefix("<msg>data</msg>") == "<msg>data</msg>"

    def test_empty_string_passthrough(self) -> None:
        assert _strip_wxid_prefix("") == ""

    def test_none_passthrough(self) -> None:
        assert _strip_wxid_prefix(None) is None  # type: ignore[arg-type]

    def test_plain_text_no_prefix_passthrough(self) -> None:
        assert _strip_wxid_prefix("hello world") == "hello world"

    def test_plain_text_with_prefix_stripped(self) -> None:
        assert _strip_wxid_prefix("l333308:\nhello world") == "hello world"
