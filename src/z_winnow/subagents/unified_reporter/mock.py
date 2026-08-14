"""unified_reporter mock — deterministic output for mock mode.

P010: Returns a complete UnifiedReporterOutput with mock data covering
all sections. Topic Unification: unified `topics[]` list with all three
lifecycle types (user_defined, sustained, emerging).
"""

from __future__ import annotations

from typing import Any

from z_winnow.subagents.unified_reporter.models import (
    EngineeringIssue,
    Resource,
    Topic,
    UnifiedReporterOutput,
)


def _mock_generate_unified_report(
    messages: list[dict[str, Any]],
    date: str,
    group_name: str,
) -> UnifiedReporterOutput:
    """Generate a deterministic mock unified report.

    Covers all sections so the full pipeline (graph node -> output_composer
    -> persist -> write_reports) can be tested without real LLM/MemOS.

    Topics include all three lifecycle types:
      - user_defined: matched from core_topics (user-created)
      - sustained: appeared across multiple days (MemOS historical match)
      - emerging: first appearance today

    Args:
        messages: Chat messages (used only for count in overview).
        date: Target date YYYYMMDD.
        group_name: Chat group display name.

    Returns:
        UnifiedReporterOutput with mock data for all sections.
    """
    display_date = date
    if len(date) == 8:
        display_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

    msg_count = len(messages)

    return UnifiedReporterOutput(
        # ── Daily report section ──
        overview=(
            f"Mock 日报概览 — {display_date}：今日{group_name}群聊共{msg_count}条消息，"
            "讨论了技术架构、工具链和安全防护话题，整体讨论积极有深度。"
        ),
        important_notice="",  # W16-A1: str default '' (was None, root out B7)
        topics=[
            Topic(
                topic_id="tp_a1b2c3d4",
                topic_name="LangGraph Send API 并行 fan-out 实践",
                lifecycle="user_defined",
                status="active",
                weight=0.85,
                background="团队计划在日报 pipeline 中引入并行处理，原先用 asyncio.gather 实现 3 路并行但错误隔离困难。",
                process=(
                    "张三演示了 Send API 的 conditional_edges 返回机制，"
                    "李四验证了确定性并行在不同消息量下的表现。"
                ),
                conclusion="决定采用 Send API 替代原有的 asyncio.gather 方案，预期延迟降低 40%。",
                description=(
                    "LangGraph Send API 在群日报 pipeline 中的并行处理实践。"
                    "归入边界：涉及 Send API、conditional_edges、fan-out 调度的讨论。"
                    "不包括 LangGraph 基础概念学习。"
                ),
                trend=(
                    "团队从1月初开始研究 LangGraph 并行机制。最初使用 asyncio.gather "
                    "实现 3 路并行，但发现错误隔离困难。1月中旬张三发现 Send API 的 "
                    "conditional_edges 机制可以提供确定性并行，经过两周验证后性能表现稳定。"
                    "今天讨论确认正式采用此方案，下周开始代码迁移。"
                ),
                participants=["张三", "李四"],
                first_seen="2025-01-15",
                last_seen=display_date,
                source_server_ids=["msg_002", "msg_003"],
            ),
            Topic(
                topic_id="tp_c3d4e5f6",
                topic_name="Prompt 注入防护方案设计",
                lifecycle="sustained",
                status="discussion",
                weight=0.7,
                background="群日报系统暴露在不可信输入（群聊消息）下，存在 Prompt 注入风险。",
                process=(
                    "王五提出三层防护架构（XML 包装 + 角色边界 + 指令剥离），"
                    "赵六补充了 tiktoken token 预算控制的必要性。"
                ),
                conclusion="采用三层防护方案，新增 P006 token 预算规则。",
                description=(
                    "微信群聊 Prompt 注入防护方案的设计与实施。"
                    "归入边界：涉及 injection 检测、角色边界、沙箱机制的讨论。"
                    "不包括通用 LLM 安全理论。"
                ),
                trend=(
                    "讨论从5天前的概念设计阶段进入具体实现。王五完成了 XML 包装层，"
                    "赵六昨天加入了 token 预算控制。今天确定了三层防护的最终架构，"
                    "并计划本周内完成全部实现和测试。"
                ),
                participants=["王五", "赵六"],
                first_seen=f"{display_date[:4]}-{display_date[5:7]}-{str(int(display_date[8:10]) - 5).zfill(2)}",
                last_seen=display_date,
                source_server_ids=["msg_004", "msg_005"],
            ),
            Topic(
                topic_id="tp_e5f6g7h8",
                topic_name="Docker Compose 服务编排优化",
                lifecycle="emerging",
                status="discussion",
                weight=0.5,
                background="MemOS 依赖四个 Docker 容器（Redis、Neo4j、Qdrant、memos-api），编排配置需优化。",
                process="团队成员讨论了 docker-compose 的健康检查配置和依赖启动顺序。",
                conclusion="需要进一步测试 --env-file 参数在容器内的传递效果。",
                description="首次讨论 Docker Compose 多服务编排的最佳实践。",
                trend="今日首次出现，讨论了 Docker Compose 健康检查和服务依赖配置。",
                participants=["张三"],
                first_seen=display_date,
                last_seen=display_date,
                source_server_ids=["msg_009"],
            ),
        ],
        trend_analysis=(
            "今日主要讨论三条技术线：LangGraph Send API 的并行 fan-out 方案持续推进，"
            "成员已明确实现路径；Prompt 注入防护方案讨论进入第五天，架构已确定；"
            "Docker Compose 服务编排是今天新出现的话题，尚在探索阶段。"
        ),
        trend_summary="今日1个核心议题持续推进，1个持续议题架构确定，1个新议题首次出现。",
        highlights=[
            "「Send API 在 conditional_edges 中返回，支持确定性并行」",
            "「注入防护的核心是数据与指令的边界，而非简单的关键词过滤」",
        ],
        # ── Resources section ──
        resources=[
            Resource(
                time_range="09:00-10:00",
                resource_type="repo",
                summary="LangGraph 官方示例仓库，包含 Send API 使用范例",
                content="https://github.com/langchain-ai/langgraph",
                source_server_ids=["msg_006"],
            ),
            Resource(
                time_range="14:00-15:00",
                resource_type="article",
                summary="关于 LLM Prompt 注入防护的最佳实践文章",
                content="https://example.com/prompt-injection-defense",
                source_server_ids=["msg_004"],
            ),
            Resource(
                time_range="16:00-17:00",
                resource_type="image",
                summary="Send API 并行 fan-out 的数据流架构图，高密度标注了节点分发与归并路径",
                content="Send API 并行 fan-out 数据流架构图",
                source_server_ids=["msg_010"],
            ),
        ],
        resource_count_by_type={"repo": 1, "article": 1, "image": 1},
        # ── Custom tables (通用槽位) ──
        # engineering 与 world_models 都走 custom_tables 槽位，形状见各表 YAML。
        custom_tables={
            "engineering": {
                "issues": [
                    EngineeringIssue(
                        datetime=f"{display_date} 10:30",
                        group="部署与基础设施",
                        description="CI 构建服务器磁盘空间不足，Jenkins 构建频繁失败",
                        solution="清理旧的构建产物，增加磁盘配额",
                        status="⚠️",
                        status_desc="待解决",
                        source_members="Alice, Bob",
                        key_operations="磁盘清理 / 配额管理",
                        source_server_ids=["msg_007", "msg_008"],
                    ).model_dump(),
                ],
                "group_summary": {
                    "部署与基础设施": "CI 磁盘告警需要立即处理",
                    "开发与调试工具": "无重大问题",
                    "记忆与进化机制": "无重大问题",
                    "生态与工具链": "无重大问题",
                    "成本控制与性能优化": "无重大问题",
                    "安全与合规": "Prompt 注入防护方案讨论中",
                },
            },
            "world_models": {
                "items": [
                    {
                        "datetime": f"{display_date} 14:20",
                        "school": "生成派",
                        "model_or_system": "Sora",
                        "topic": "Sora 长视频生成的因果漂移问题",
                        "progress": "讨论了像素空间自回归预测导致的物体变形",
                        "signal": "观点质疑",
                        "significance": "印证生成派弱点：因果稳定性差、易幻觉漂移",
                        "source_members": "Alice",
                        "source_server_ids": ["msg_009"],
                    },
                ],
                "school_summary": {
                    "生成派": "Sora 漂移问题讨论，验证像素空间预测局限",
                },
            },
        },
        model_used="mock",
    )
