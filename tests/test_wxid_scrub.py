"""M.8.2 回归防护 — 验证 _scrub_wxid 辅助函数。

确保 wxid_ 标识符不会泄漏到 MemOS 记忆节点。
"""

from __future__ import annotations

from z_winnow.graph.builder import _scrub_wxid


class TestScrubWxid:
    """M.8.2: _scrub_wxid 替换 wxid_ 模式为 [成员]。"""

    def test_replaces_single_wxid(self) -> None:
        assert _scrub_wxid("wxid_abc123 发言了") == "[成员] 发言了"

    def test_replaces_multiple_wxid(self) -> None:
        assert _scrub_wxid("wxid_aaa 和 wxid_bbb 讨论") == "[成员] 和 [成员] 讨论"

    def test_preserves_normal_names(self) -> None:
        assert _scrub_wxid("张三和李四讨论") == "张三和李四讨论"

    def test_no_wxid_passthrough(self) -> None:
        assert _scrub_wxid("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert _scrub_wxid("") == ""
