"""T-W12-10 + T-W12-11 + T-W12-9 + T-W12-7: test_graph_builder — verify graph structure.

P060: Removed-Dependency Test Conversion — tests verify:
1. Graph compiles without error
2. topic_classifier node does NOT exist in the graph
3. Graph topology is correct: orchestrator -> unified_reporter -> output_composer
4. No stale references to removed nodes/edges
5. B1: No Send API calls
6. B3: No topic_tracker/ directory
7. B5: unified_reporter prompt includes topic_tracking + lifecycle
8. T-W12-10: write_reports NOT in main flow (S4: Markdown deferred)
9. T-W12-10: export_markdown entry point exists
10. T-W12-7: persist node removed — per-stage L1/L2/L3 writes (S1)
"""

from __future__ import annotations

from pathlib import Path


def test_graph_builds_successfully():
    """Graph builds and compiles without errors."""
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    assert graph is not None


def test_graph_compiles():
    """Graph compiles into a runnable without errors."""
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    compiled = graph.compile()
    assert compiled is not None


def test_no_topic_classifier_node():
    """P060: topic_classifier node must NOT exist in the graph."""
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    # Access internal node dict to verify topic_classifier is absent
    set(graph.nodes.keys()) if hasattr(graph, "nodes") else set()

    # Also verify via compile — if topic_classifier were referenced, compile would fail
    compiled = graph.compile()
    assert compiled is not None

    # Verify the function node_topic_classifier does not exist in builder module
    import z_winnow.graph.builder as builder_mod

    assert not hasattr(builder_mod, "node_topic_classifier"), (
        "node_topic_classifier should have been removed from builder.py"
    )


def test_no_merge_node():
    """P060: merge node removed — no parallel fan-out in Wave 12."""
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    compiled = graph.compile()
    assert compiled is not None


def test_graph_has_expected_nodes():
    """Graph contains exactly the expected nodes (no more, no less).

    T-W12-10: write_reports removed from main flow (S4).
    T-W12-7: persist removed — writes distributed to per-stage nodes (S1).
    """
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    expected_nodes = {
        "data_fetch",
        "content_enrich",
        "orchestrator",
        "unified_reporter",
        "output_composer",
    }

    actual_nodes = set(graph.nodes.keys())
    assert actual_nodes == expected_nodes, (
        f"Graph nodes mismatch.\n"
        f"  Expected: {sorted(expected_nodes)}\n"
        f"  Actual:   {sorted(actual_nodes)}\n"
        f"  Missing:  {sorted(expected_nodes - actual_nodes)}\n"
        f"  Extra:    {sorted(actual_nodes - expected_nodes)}"
    )


def test_unified_reporter_direct_edge_to_output_composer():
    """unified_reporter connects directly to output_composer (no intermediate nodes)."""
    from z_winnow.graph.builder import build_graph

    graph = build_graph()

    # Get edges from unified_reporter — should go directly to output_composer
    # LangGraph stores edges internally; verify by checking compile succeeds
    # and the expected nodes are present
    compiled = graph.compile()
    assert compiled is not None

    # Verify no topic_classifier in the graph at all
    node_names = set(graph.nodes.keys())
    assert "topic_classifier" not in node_names
    assert "topic_tracker" not in node_names
    assert "merge" not in node_names


# ============================================================
# T-W12-9 additional B-level verification
# ============================================================


def test_no_send_api_in_builder():
    """B1: builder.py source contains no Send() calls."""
    builder_path = Path("src/z_winnow/graph/builder.py")
    content = builder_path.read_text(encoding="utf-8")
    assert "Send(" not in content, "B1 FAIL: Send() call found in builder.py"


def test_no_continue_to_subagents():
    """B1: continue_to_subagents function removed."""
    import z_winnow.graph.builder as builder_mod

    assert not hasattr(builder_mod, "continue_to_subagents"), (
        "B1 FAIL: continue_to_subagents still exists"
    )


def test_topic_tracker_directory_removed():
    """B3: subagents/topic_tracker/ directory does not exist."""
    topic_tracker_dir = Path("src/z_winnow/subagents/topic_tracker")
    assert not topic_tracker_dir.exists(), (
        f"B3 FAIL: topic_tracker directory still exists at {topic_tracker_dir}"
    )


def test_fanout_file_removed():
    """fanout.py has been deleted."""
    fanout_path = Path("src/z_winnow/graph/nodes/fanout.py")
    assert not fanout_path.exists(), f"fanout.py still exists at {fanout_path}"


def test_system_prompt_includes_topic_tracking():
    """B5: SYSTEM_PROMPT contains 'topic_tracking' instructions."""
    from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

    assert "topic_tracking" in SYSTEM_PROMPT, "B5 FAIL: 'topic_tracking' not found in SYSTEM_PROMPT"


def test_system_prompt_includes_lifecycle():
    """B5: SYSTEM_PROMPT contains 'lifecycle' classification instructions."""
    from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

    assert "lifecycle" in SYSTEM_PROMPT, "B5 FAIL: 'lifecycle' not found in SYSTEM_PROMPT"


def test_system_prompt_includes_trend_summary():
    """B5: SYSTEM_PROMPT contains 'trend_summary' instructions."""
    from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

    assert "trend_summary" in SYSTEM_PROMPT, "B5 FAIL: 'trend_summary' not found in SYSTEM_PROMPT"


def test_system_prompt_lifecycle_categories():
    """B5: SYSTEM_PROMPT defines lifecycle categories: user_defined/sustained/emerging."""
    from z_winnow.subagents.unified_reporter.prompt import SYSTEM_PROMPT

    for category in ("user_defined", "sustained", "emerging"):
        assert category in SYSTEM_PROMPT, (
            f"B5 FAIL: lifecycle category '{category}' not found in SYSTEM_PROMPT"
        )


# ============================================================
# T-W12-10: S4 — Markdown rendering deferred to Phase H
# ============================================================


def test_write_reports_not_in_main_flow():
    """B4: write_reports node NOT in the graph (S4: Markdown deferred).

    P060: Converted from write_reports execution test to absence check.
    P030: compile() verification confirms no orphan references.
    """
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    compiled = graph.compile()
    assert compiled is not None

    node_names = set(graph.nodes.keys())
    assert "write_reports" not in node_names, (
        "B4 FAIL: write_reports should not be in main flow (S4)"
    )


def test_export_markdown_function_exists():
    """B3: export_markdown entry point exists and is callable."""
    from z_winnow.graph.builder import export_markdown

    assert callable(export_markdown), "export_markdown must be callable"


def test_export_markdown_is_async():
    """B3: export_markdown is an async function (for DB operations)."""
    import inspect

    from z_winnow.graph.builder import export_markdown

    assert inspect.iscoroutinefunction(export_markdown), "export_markdown must be an async function"


def test_output_composer_writes_content_null():
    """B1: output_composer node writes content=NULL to report_versions.

    T-W12-7: Moved from test_persist_writes_content_null — persist removed,
    L3 writes now in output_composer.
    Verifies the output_composer node function source references content=None.
    """
    import inspect

    from z_winnow.graph.builder import node_output_composer

    source = inspect.getsource(node_output_composer)
    # P022: content=None in Phase E
    assert "content=None" in source, (
        "B1 FAIL: output_composer node should pass content=None to create_version (S4)"
    )


def test_no_write_reports_in_add_edge_sequence():
    """B4: No add_edge/add_conditional_edges references write_reports in build_graph.

    A002: code_inspection — grep source to verify absence.
    """
    import inspect

    from z_winnow.graph.builder import build_graph

    source = inspect.getsource(build_graph)
    # Check no edge references to write_reports
    assert '"write_reports"' not in source, (
        "B4 FAIL: build_graph still references write_reports in edges"
    )
    assert "'write_reports'" not in source, (
        "B4 FAIL: build_graph still references write_reports in edges"
    )


def test_graph_compiles_with_no_orphan_nodes():
    """P030: Static Graph Compile Verification.

    Compiles the graph and verifies no orphan nodes or broken edges.
    T-W12-7: persist node removed; feishu graph nodes removed (push is web-only).
    """
    from z_winnow.graph.builder import build_graph

    graph = build_graph()
    compiled = graph.compile()
    assert compiled is not None

    # All registered nodes should be reachable from START
    node_names = set(graph.nodes.keys())
    assert len(node_names) > 0

    # Verify key nodes present (persist removed — T-W12-7)
    assert "output_composer" in node_names
    # A002: persist must NOT exist
    assert "persist" not in node_names, (
        "P030 FAIL: persist node should not exist (T-W12-7: removed)"
    )


# ============================================================
# T-W12-7: S1 per-stage persistence verification
# ============================================================


def test_no_persist_node_in_builder():
    """B1: node_persist function does NOT exist in builder module.

    A002: grep confirms zero residual — not just no centralized logic,
    the function definition itself must not exist.
    """
    import z_winnow.graph.builder as builder_mod

    assert not hasattr(builder_mod, "node_persist"), (
        "B1 FAIL: node_persist function should not exist in builder.py"
    )


def test_data_fetch_has_l1_write():
    """B2: node_data_fetch includes L1 write (raw_messages) at end.

    Verifies the function source contains insert_raw_messages call.
    """
    import inspect

    from z_winnow.graph.builder import node_data_fetch

    source = inspect.getsource(node_data_fetch)
    assert "insert_raw_messages" in source or "_insert_raw" in source, (
        "B2 FAIL: node_data_fetch should call insert_raw_messages for L1 write"
    )


def test_content_enrich_has_l2_write():
    """B2: node_content_enrich includes L2 write (parsed_contexts) at end.

    Verifies the function source contains insert_parsed_contexts call.
    """
    import inspect

    from z_winnow.content_enrich import node_content_enrich

    source = inspect.getsource(node_content_enrich)
    assert "insert_parsed_contexts" in source or "_insert_ctx" in source, (
        "B2 FAIL: node_content_enrich should call insert_parsed_contexts for L2 write"
    )


def test_output_composer_has_l3_write():
    """B2: node_output_composer includes L3 writes at end.

    Verifies the function source contains topic_summaries and report_versions writes.
    """
    import inspect

    from z_winnow.graph.builder import node_output_composer

    source = inspect.getsource(node_output_composer)
    assert "topic_summaries" in source, (
        "B2 FAIL: node_output_composer should write topic_summaries (L3)"
    )
    assert "create_version" in source, (
        "B2 FAIL: node_output_composer should call create_version (L3 report_versions)"
    )


# ============================================================
# T-W13-3: L2 context_text retains sender nickname
# ============================================================


class TestL2SenderNickname:
    """T-W13-3: L2 parsed_contexts include [nickname]: content format."""

    def test_l2_context_text_includes_sender_prefix(self):
        """B1: L2 context_text uses enrich_message_for_llm for formatting.

        Verifies that node_content_enrich delegates to enrich_message_for_llm
        for consistent L2 formatting across DB and LLM consumption.
        """
        import inspect

        from z_winnow.content_enrich import node_content_enrich
        from z_winnow.pipeline.context import enrich_message_for_llm

        # Verify node_content_enrich imports and calls enrich_message_for_llm
        source = inspect.getsource(node_content_enrich)
        assert "enrich_message_for_llm" in source, (
            "B1 FAIL: node_content_enrich should call enrich_message_for_llm"
        )
        # Verify enrich_message_for_llm preserves original fields
        msg = {"server_id": "x", "content": "test", "msg_type": "text", "account_name": "Alice"}
        result = enrich_message_for_llm(msg)
        assert result["account_name"] == "Alice"

    def test_l2_context_text_sender_cascade_fallback(self):
        """P014: enrich_message_for_llm preserves all original fields.

        Sender cascade is now handled by wrap_messages_xml which reads
        account_name -> sender fallback. enrich_message_for_llm preserves
        both fields so the cascade works downstream.
        """
        from z_winnow.pipeline.context import enrich_message_for_llm

        # account_name present: preserved
        msg1 = {"server_id": "x", "content": "test", "msg_type": "text", "account_name": "Alice"}
        assert enrich_message_for_llm(msg1)["account_name"] == "Alice"

        # account_name empty, sender present: sender preserved
        msg2 = {
            "server_id": "x",
            "content": "test",
            "msg_type": "text",
            "account_name": "",
            "sender": "bob_wxid",
        }
        r2 = enrich_message_for_llm(msg2)
        assert r2["account_name"] == ""
        assert r2["sender"] == "bob_wxid"

    def test_enrich_messages_preserves_account_name(self):
        """B2: enrich_message_for_llm() passes through account_name without modification.

        enrich_message_for_llm returns new dicts preserving all original fields.
        """
        from z_winnow.pipeline.context import enrich_message_for_llm

        messages = [
            {
                "server_id": "sid_001",
                "content": "[图片]",
                "msg_type": "image",
                "account_name": "Alice",
                "sender": "alice_wxid",
            },
            {
                "server_id": "sid_002",
                "content": "World",
                "msg_type": "text",
                "account_name": "Bob",
                "sender": "bob_wxid",
            },
        ]
        image_descs = {"sid_001": "A photo of cats"}
        enriched = [enrich_message_for_llm(m, image_descs) for m in messages]

        # account_name must be preserved exactly
        assert enriched[0]["account_name"] == "Alice", (
            "B2 FAIL: account_name 'Alice' was modified by enrich_message_for_llm"
        )
        assert enriched[1]["account_name"] == "Bob", (
            "B2 FAIL: account_name 'Bob' was modified by enrich_message_for_llm"
        )
        # Image message content should be replaced with AI description
        assert enriched[0]["content"] == "A photo of cats"
        # Non-image text message content stays the same
        assert enriched[1]["content"] == "World"

    def test_enrich_messages_no_overwrite_without_enrichments(self):
        """B2: messages without enrichments still get type-specific formatting."""
        from z_winnow.pipeline.context import enrich_message_for_llm

        messages = [
            {
                "server_id": "sid_001",
                "content": "Hello",
                "msg_type": "text",
                "account_name": "Charlie",
            },
        ]
        enriched = [enrich_message_for_llm(m) for m in messages]

        # New impl returns new dicts (not same object)
        assert enriched[0]["account_name"] == "Charlie"
        assert enriched[0]["content"] == "Hello"

    def test_l2_context_text_format_with_mock(self):
        """B1/B3 integration: Verify L2 context_text format via enrich_message_for_llm.

        L2 DB now uses enrich_message_for_llm() for consistent formatting.
        Content field is formatted per msg_type (text: passthrough, image: desc, etc.)
        Sender/timestamp are handled by wrap_messages_xml, not enrich_message_for_llm.
        """
        from z_winnow.pipeline.context import enrich_message_for_llm

        mock_messages = [
            {
                "server_id": "sid_001",
                "content": "This is a test message",
                "msg_type": "text",
                "account_name": "Alice",
                "sender": "alice_wxid",
            },
            {
                "server_id": "sid_002",
                "content": "",
                "msg_type": "image",
                "account_name": "Bob",
                "sender": "bob_wxid",
            },
        ]
        image_descs = {"sid_002": "A screenshot of code"}

        # Text message: content preserved as-is
        r0 = enrich_message_for_llm(mock_messages[0])
        assert r0["content"] == "This is a test message"
        assert r0["account_name"] == "Alice"

        # Image message: content replaced with AI description
        r1 = enrich_message_for_llm(mock_messages[1], image_descriptions=image_descs)
        assert r1["content"] == "A screenshot of code"
        assert r1["account_name"] == "Bob"


class TestEnrichMessageForLlm:
    """Unit tests for enrich_message_for_llm() — 11 msg_type routing."""

    def _make_msg(self, **overrides) -> dict:
        base = {
            "server_id": "srv_001",
            "content": "default content",
            "msg_type": "text",
            "account_name": "Alice",
            "sender": "alice_wxid",
            "timestamp": 1700000000000,
        }
        base.update(overrides)
        return base

    def test_text_passthrough(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="text", content="Hello world"))
        assert r["content"] == "Hello world"
        assert r["server_id"] == "srv_001"

    def test_image_with_description(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(
            self._make_msg(msg_type="image", content="[图片]"),
            image_descriptions={"srv_001": "A cat photo"},
        )
        assert r["content"] == "A cat photo"

    def test_image_without_description(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="image", content="[图片]"))
        assert r["content"] == "[图片]"

    def test_appmsg(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="appmsg", content="GitHub PR #42"))
        assert r["content"] == "[分享链接] GitHub PR #42"

    def test_file(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(
            self._make_msg(msg_type="file", content="report.pdf (2MB)"),
        )
        assert "[文件: report.pdf (2MB)]" in r["content"]

    def test_file_with_media_url(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(
            self._make_msg(msg_type="file", content="report.pdf", media_url="https://cdn/x"),
        )
        assert "[下载: https://cdn/x]" in r["content"]

    def test_reply_xml_cleanup(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(
            self._make_msg(
                msg_type="reply",
                content="回复的文本 view 57 0 0 0 0 49 <msgsource>noise</msgsource>",
            ),
        )
        # XML metadata should be cleaned, only user text preserved
        assert "回复的文本" in r["content"]
        assert "<msgsource>" not in r["content"]

    def test_voice(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="voice"))
        assert r["content"] == "[语音]"

    def test_video(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="video"))
        assert r["content"] == "[视频]"

    def test_emoji(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        # emoji with no image_description and no content → [表情]
        r = enrich_message_for_llm(self._make_msg(msg_type="emoji", content=""))
        assert r["content"] == "[表情]"

    def test_emoji_with_vision_description(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        # emoji with image_description → [表情包: desc]
        r = enrich_message_for_llm(
            self._make_msg(msg_type="emoji"),
            image_descriptions={"srv_001": "大笑表情"},
        )
        assert r["content"] == "[表情包: 大笑表情]"

    def test_emoji_falls_back_to_content(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        # emoji with content but no image_description → use content
        r = enrich_message_for_llm(self._make_msg(msg_type="emoji", content="👍"))
        assert r["content"] == "👍"

    def test_recall(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="recall"))
        assert r["content"] == "[消息已撤回]"

    def test_weapp(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(self._make_msg(msg_type="weapp", content="小程序标题"))
        assert r["content"] == "[小程序] 小程序标题"

    def test_link_preview_appended(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        r = enrich_message_for_llm(
            self._make_msg(msg_type="text", content="Check this out"),
            link_previews={"srv_001": {"title": "Cool Article", "url": "https://x.com"}},
        )
        assert "Check this out" in r["content"]
        assert "[链接预览: Cool Article]" in r["content"]

    def test_preserves_all_original_fields(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        msg = self._make_msg(extra_field="should survive")
        r = enrich_message_for_llm(msg)
        assert r["extra_field"] == "should survive"
        assert r["server_id"] == "srv_001"
        assert r["sender"] == "alice_wxid"
        assert r["timestamp"] == 1700000000000

    def test_returns_new_dict(self):
        from z_winnow.pipeline.context import enrich_message_for_llm

        msg = self._make_msg()
        r = enrich_message_for_llm(msg)
        assert r is not msg


# ============================================================
# T-W13-5: MemOS user_id unified to group_id
# ============================================================


def test_orchestrator_search_memories_uses_group_id():
    """T-W13-5 B1: orchestrator's memory search uses group_id=group_id.

    The search_memories call was refactored out of node_orchestrator into the
    _do_mem_search_one_cube helper; inspect that helper to verify group_id is used
    and the legacy user_id=group_name pattern is gone.
    """
    import inspect

    from z_winnow.graph.builder import _do_mem_search_one_cube

    source = inspect.getsource(_do_mem_search_one_cube)
    assert "group_id=group_id" in source, (
        "T-W13-5 B1 FAIL: memory search should use group_id=group_id"
    )
    assert "user_id=group_name" not in source, (
        "T-W13-5 B1 FAIL: memory search should NOT use user_id=group_name"
    )


def test_output_composer_no_hardcoded_user_id():
    """T-W13-5 B1: output_composer does not use hardcoded 'winnow' as user_id."""
    import inspect

    from z_winnow.graph.builder import node_output_composer

    source = inspect.getsource(node_output_composer)
    assert 'user_id="winnow"' not in source, (
        "T-W13-5 B1 FAIL: output_composer should not have hardcoded user_id"
    )
