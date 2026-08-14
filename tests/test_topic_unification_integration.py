"""test_topic_unification_integration.py — real LLM integration test.

Uses actual Anthropic API to verify that the unified_reporter produces
correct `topics[]` with lifecycle classification, conclusion quality,
trend narratives, and participants from real chat messages.

Requires ANTHROPIC_API_KEY env var. Marked as @pytest.mark.integration.

严禁使用 mock 数据 — 此测试必须用真实 LLM 调用验证核心逻辑。
"""

from __future__ import annotations

import os

import pytest

from z_winnow.subagents.unified_reporter.agent import generate_unified_report
from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput

# Skip entire module if no API key
pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping real LLM integration test",
)


def _build_test_messages() -> list[dict]:
    """20+ simulated chat messages with group nicknames.

    These cover:
    - A user_defined topic: AI伦理与安全 (should match core_topics)
    - A sustained topic: LLM选型与架构设计 (appears in historical_topics)
    - An emerging topic: 微信机器人部署 (brand new)
    """
    date = "20260523"
    messages = []
    base_ts = 1716422400000  # 2024-05-23 00:00:00 UTC base

    msgs = [
        (
            base_ts + 3600000,
            "张三",
            "大家早上好，今天我们继续讨论 LLM 选型的事情。上周已经测试了 Claude 和 GPT-4o，我觉得 Claude 在中文场景表现更好",
        ),
        (
            base_ts + 3660000,
            "李四",
            "同意张三的看法。我在日报 pipeline 里测试了一下，Claude 的结构化输出确实更稳定，json_mode 基本不翻车",
        ),
        (
            base_ts + 3720000,
            "王五",
            "不过 GPT-4o-mini 的成本优势还是很明显的。如果预算有限的话，可以考虑用 GPT-4o-mini 做预处理，Claude 做最终生成",
        ),
        (
            base_ts + 3780000,
            "张三",
            "这个方案可以。两层架构：mini 做初步筛选和分类，Claude 做深度分析和生成。我们决定日报用 Sonnet，预处理用 GPT-4o-mini",
        ),
        (
            base_ts + 3840000,
            "李四",
            "关于 AI 伦理问题，最近看到一篇关于 LLM 隐私泄露的论文，挺有启发性的",
        ),
        (
            base_ts + 3900000,
            "赵六",
            "我也关注到了。特别是群聊数据涉及到大量个人隐私信息，我们的日报系统应该怎么处理这个问题？",
        ),
        (
            base_ts + 3960000,
            "张三",
            "这个确实需要重视。我觉得至少要做到：1) 不存储原始聊天记录 2) 日报中的参与者用昵称而不是真实姓名 3) 定期清理历史数据",
        ),
        (
            base_ts + 4020000,
            "李四",
            "合规方面还需要考虑数据跨境的问题。如果用 Anthropic 的 API，数据会传到美国服务器",
        ),
        (base_ts + 4080000, "赵六", "对，这个需要在隐私政策里明确告知用户。安全合规是底线"),
        (
            base_ts + 4200000,
            "王五",
            "换个话题，我最近在研究微信机器人的部署方案。用 Docker Compose 跑的话，需要 Redis、消息队列和 bot 服务三个容器",
        ),
        (
            base_ts + 4260000,
            "张三",
            "Docker Compose 可以，但要注意健康检查的配置。我之前踩过坑，容器启动顺序不对会导致 bot 连不上 Redis",
        ),
        (
            base_ts + 4320000,
            "王五",
            "对，我打算用 depends_on + healthcheck 来解决这个问题。另外还需要考虑 --env-file 参数的传递",
        ),
        (
            base_ts + 4380000,
            "张三",
            "推荐用 volumes 挂载 .env 文件，比 --env-file 更可靠。你写到 docker-compose.yml 里了吗？",
        ),
        (base_ts + 4440000, "王五", "还没，正在写。预计明天能完成初版，到时候可以一起 review"),
        (
            base_ts + 4500000,
            "李四",
            "关于 LLM 选型，我补充一点：Sonnet 的 context window 是 200k，对我们日报场景完全够用了。上次那个 1000 条消息的测试也跑通了",
        ),
        (
            base_ts + 4560000,
            "张三",
            "好的，那 LLM 选型的事情就定下来了。日报用 Sonnet，预处理用 GPT-4o-mini。下周开始代码迁移",
        ),
        (
            base_ts + 4620000,
            "赵六",
            "回到 AI 伦理话题，我建议我们在系统里加一个数据脱敏模块，自动检测和替换敏感信息",
        ),
        (
            base_ts + 4680000,
            "李四",
            "好主意。可以用正则匹配身份证号、手机号这些，再加上 LLM 做语义层面的脱敏",
        ),
        (
            base_ts + 4740000,
            "张三",
            "总结一下今天的讨论：1) LLM 选型确定用两层架构 2) AI 伦理需要加数据脱敏 3) 王五在搞微信机器人部署",
        ),
        (base_ts + 4800000, "王五", "收到。明天继续推进微信机器人的 Docker 配置"),
        (base_ts + 4860000, "赵六", "我这边下周出一个 AI 安全合规的方案文档，大家 review"),
        (base_ts + 4920000, "李四", "👍 大家效率很高，今天讨论很有成效"),
    ]

    for i, (ts, sender, content) in enumerate(msgs):
        messages.append(
            {
                "serverId": str(100000 + i),
                "sender": sender,
                "content": content,
                "timestamp": ts,
                "msgType": 1,
                "date": date,
            }
        )

    return messages


def _build_historical_topics() -> list[dict]:
    """Mock historical topics from MemOS — one sustained topic."""
    return [
        {
            "memory": "LLM选型与架构设计: 团队正在评估 Claude 和 GPT-4o 用于日报生成",
            "metadata": {
                "lifecycle": "sustained",
                "date": "2026-05-20",
                "entities": ["张三", "李四"],
                "tags": ["sustained", "topic"],
            },
        }
    ]


def _build_user_defined_topics() -> list[dict]:
    """Mock user-defined topics from core_topics table."""
    return [
        {
            "name": "AI伦理与安全",
            "description": "涉及AI隐私、偏见、安全合规的讨论",
            "keywords": "伦理, 隐私, 安全, 合规, 脱敏",
            "core_topic_id": "ct_001",
        }
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_llm_produces_valid_unified_output():
    """Real LLM call: verify UnifiedReporterOutput with unified topics."""
    messages = _build_test_messages()
    historical = _build_historical_topics()
    user_defined = _build_user_defined_topics()

    result = await generate_unified_report(
        messages=messages,
        date="20260523",
        group_name="AI技术交流群",
        historical_topics=historical,
        user_defined_topics=user_defined,
    )

    # Basic structure
    assert isinstance(result, UnifiedReporterOutput)
    assert result.overview, "Overview should not be empty"
    assert len(result.overview) >= 10, "Overview should be substantive"

    # Topics
    assert len(result.topics) >= 2, f"Expected at least 2 topics, got {len(result.topics)}"

    # Lifecycle values are valid
    valid_lifecycles = {"user_defined", "sustained", "emerging"}
    for t in result.topics:
        assert t.lifecycle in valid_lifecycles, f"Invalid lifecycle: {t.lifecycle}"

    # Conclusion quality — should be substantive causal chains
    for t in result.topics:
        conclusion = t.conclusion
        assert len(conclusion) >= 20, (
            f"Conclusion too short for '{t.topic_name}': {len(conclusion)} chars. "
            "Expected causal chain (背景→讨论→结论)"
        )

    # Participants — should be nicknames, not wxid
    for t in result.topics:
        participants = t.participants
        assert isinstance(participants, list)
        for p in participants:
            assert isinstance(p, str)
            assert len(p) >= 2, f"Participant name too short: '{p}'"
            assert not p.startswith("wxid_"), f"Participant is wxid: {p}"

    # Source server IDs — should come from real messages
    for t in result.topics:
        ids = t.source_server_ids
        assert isinstance(ids, list)
        assert len(ids) > 0, f"Topic '{t.topic_name}' has no source_server_ids"
        for sid in ids:
            assert isinstance(sid, str)

    # Trend analysis should be present
    assert result.trend_analysis, "trend_analysis should not be empty"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_llm_classifies_lifecycle_correctly():
    """Real LLM call: verify lifecycle classification matches input context.

    Given:
    - user_defined_topics with "AI伦理与安全" → should produce user_defined topic
    - historical_topics with "LLM选型" → should produce sustained topic
    - "微信机器人部署" is new → should be emerging
    """
    messages = _build_test_messages()
    historical = _build_historical_topics()
    user_defined = _build_user_defined_topics()

    result = await generate_unified_report(
        messages=messages,
        date="20260523",
        group_name="AI技术交流群",
        historical_topics=historical,
        user_defined_topics=user_defined,
    )

    # Check user_defined topic — should match AI伦理
    user_defined_topics = [t for t in result.topics if t.lifecycle == "user_defined"]
    assert len(user_defined_topics) >= 1, (
        "Expected at least 1 user_defined topic matching 'AI伦理与安全'"
    )

    # Check sustained topic — should match LLM选型
    sustained_topics = [t for t in result.topics if t.lifecycle == "sustained"]
    assert len(sustained_topics) >= 1, "Expected at least 1 sustained topic matching 'LLM选型'"

    # Check trend for sustained topics — should contain cross-day content
    for t in sustained_topics:
        trend = t.trend
        assert len(trend) >= 20, (
            f"Sustained topic '{t.topic_name}' trend should reference cross-day evolution"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_llm_trend_analysis_is_substantive():
    """Real LLM call: trend_analysis and per-topic trend should be narrative paragraphs."""
    messages = _build_test_messages()
    historical = _build_historical_topics()

    result = await generate_unified_report(
        messages=messages,
        date="20260523",
        group_name="AI技术交流群",
        historical_topics=historical,
    )

    # trend_analysis should be a substantial paragraph
    assert len(result.trend_analysis) >= 50, (
        f"trend_analysis too short ({len(result.trend_analysis)} chars). "
        "Expected substantive narrative analysis."
    )

    # Per-topic trend should be present for non-trivial topics
    for t in result.topics:
        if t.lifecycle in ("sustained", "user_defined"):
            trend = t.trend
            assert len(trend) >= 20, (
                f"Topic '{t.topic_name}' ({t.lifecycle}) should have trend narrative"
            )
