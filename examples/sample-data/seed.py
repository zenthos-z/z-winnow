#!/usr/bin/env python3
"""seed.py — 加载完全虚构的示例数据，让 Web UI / MCP 零依赖可逛。

所有群名、人名、消息、议题、链接均为虚构（link 使用 example.com 保留域），
与任何真实群聊、真实人物无关。可重复执行（幂等）：先清空示例群旧数据再写入。

用法（仓库根目录）：
    poetry run python examples/sample-data/seed.py
    poetry run winnow web     # 打开 http://127.0.0.1:8100/ui/ 浏览「示例群·AI 工具观察站」
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

# 以脚本位置 bootstrap 项目包路径（examples/sample-data/ → 仓库根/src）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import aiosqlite  # noqa: E402

from z_winnow.config.settings import get_settings  # noqa: E402
from z_winnow.pipeline.database import init_database  # noqa: E402

# ----------------------------------------------------------------------------
# 虚构数据定义
# ----------------------------------------------------------------------------

GROUP_ID = "g_demo_frontier"
DISPLAY_NAME = "示例群·AI 工具观察站"
CHATROOM_ID = "99990001@chatroom"

MEMBERS = ["林远舟", "苏砚清", "程亦凡", "顾栖桐", "周听澜"]

DAYS = ["20260812", "20260813", "20260814"]

# 三天消息流（完全虚构）。serverID 用 9000xxxxxx 段避免与真实数据碰撞。
MESSAGES: dict[str, list[dict]] = {
    "20260812": [
        ("9000000101", "林远舟", "text", "早上好，最近想在本地跑一个 7B 的模型做文档摘要，大家有推荐的长上下文方案吗"),
        ("9000000102", "苏砚清", "text", "我上周试过 llama.cpp + ollama 两条路，ollama 上手快但量化选项少一些"),
        ("9000000103", "程亦凡", "text", "看你要不要并发，单机自用 ollama 够了；要对外提供服务建议 vllm"),
        ("9000000104", "林远舟", "text", "主要是给团队内部用，大概 20 个人，并发不高"),
        ("9000000105", "顾栖桐", "text", "那可以看看 LM Studio，图形界面配好后直接暴露 OpenAI 兼容接口"),
        ("9000000106", "苏砚清", "link", "https://example.com/blog/local-llm-stack-2026 分享一篇文章：2026 年本地模型部署栈盘点"),
        ("9000000107", "程亦凡", "text", "这篇我读过，里面提到异构显卡调度那段写得不错"),
        ("9000000108", "周听澜", "text", "提醒一下，显存不够的话 7B 跑 Q4 量化大概 6G 出头，留好余量"),
        ("9000000109", "林远舟", "text", "收到，我的是 16G 卡，应该没问题"),
        ("9000000110", "顾栖桐", "text", "另外问下大家，Agent 框架现在用哪个比较多？我在 LangGraph 和别的之间纠结"),
        ("9000000111", "程亦凡", "text", "看复杂度。直线流水线用啥都行；有分支回环再上图编排"),
        ("9000000112", "苏砚清", "text", "我们组上周把一个多步工具调用的任务迁到了图编排，可观测性确实好很多"),
    ],
    "20260813": [
        ("9000000201", "林远舟", "text", "昨天说的本地部署，昨晚跑通了 ollama + OpenWebUI，摘要效果比预期好"),
        ("9000000202", "苏砚清", "text", "恭喜。中文摘要记得在 prompt 里明确输出格式，不然容易碎"),
        ("9000000203", "林远舟", "text", "对，我加了「按要点分行」之后稳定多了"),
        ("9000000204", "周听澜", "text", "RAG 时效性的问题大家踩过吗？知识库一周不更新，答案就开始一本正经地胡说"),
        ("9000000205", "程亦凡", "text", "踩过。我们的做法是给每条知识打时间戳，检索时按衰减加权"),
        ("9000000206", "顾栖桐", "text", "衰减权重怎么定的？纯线性还是半衰期"),
        ("9000000207", "程亦凡", "text", "半衰期，两周。调参试出来的，业务上勉强够用"),
        ("9000000208", "苏砚清", "file", "rag-decay-notes.pdf（虚构示例文件：检索衰减实验记录）"),
        ("9000000209", "周听澜", "text", "mark，晚上细看"),
        ("9000000210", "顾栖桐", "text", "Agent 框架那个事我决定先拿 LangGraph 做个原型，图结构对我们场景合适"),
        ("9000000211", "程亦凡", "text", "合理，先把状态机画清楚再动手写"),
    ],
    "20260814": [
        ("9000000301", "林远舟", "text", "本地部署方案定了：ollama 跑 7B-Q4，前端 OpenWebUI，内网穿透走 tailscale"),
        ("9000000302", "苏砚清", "text", "稳妥。记得给接口加个简单鉴权，内网也不裸奔"),
        ("9000000303", "林远舟", "text", "已经加了 API key 网关，谢谢提醒"),
        ("9000000304", "顾栖桐", "text", "Agent 原型跑通了，图编排 + 工具调用大概 200 行，比想象中顺"),
        ("9000000305", "程亦凡", "text", "不错，下一步把每步的 trace 接上，调试体验会好一个量级"),
        ("9000000306", "周听澜", "text", "团队知识库我想正式立项，把群里这些讨论沉淀下来，别散在聊天记录里"),
        ("9000000307", "苏砚清", "text", "支持。先定分类体系，再谈工具"),
        ("9000000308", "林远舟", "link", "https://example.com/papers/knowledge-distillation-survey 荐一篇综述：面向团队场景的知识蒸馏"),
        ("9000000309", "顾栖桐", "text", "这篇可以先放进知识库当第一批资料"),
        ("9000000310", "程亦凡", "text", "好，周五我们拉个会定分类"),
    ],
}

# 议题演化（三天）——sustained 议题展示 trend 迭代
TOPICS: dict[str, list[dict]] = {
    "20260812": [
        {
            "name": "本地大模型部署方案选型",
            "lifecycle": "emerging",
            "background": "林远舟计划在本地跑 7B 模型做团队文档摘要，需要选定技术栈。",
            "process": "群内对比了 ollama / llama.cpp / vLLM / LM Studio 四条路线：ollama 上手快但量化选项少；vLLM 适合对外高并发；LM Studio 有图形界面且暴露 OpenAI 兼容接口。",
            "conclusion": "初步倾向 ollama（内网自用、并发低），16G 显存跑 7B-Q4 有余量；待实际验证摘要效果。",
            "participants": ["林远舟", "苏砚清", "程亦凡", "顾栖桐", "周听澜"],
            "trend": "初步讨论本地部署技术选型，对比四个主流方案。",
            "confidence": 0.9,
        },
        {
            "name": "Agent 编排框架对比",
            "lifecycle": "emerging",
            "background": "顾栖桐在为团队选型 Agent 框架。",
            "process": "讨论了复杂度适配问题：直线流水线任意框架皆可，有分支回环时图编排更合适；苏砚清分享多步工具调用迁移到图编排后可观测性提升的实战经验。",
            "conclusion": "倾向先做原型验证，观察图编排与现有任务的匹配度。",
            "participants": ["顾栖桐", "程亦凡", "苏砚清"],
            "trend": "开始评估框架，关注可观测性。",
            "confidence": 0.8,
        },
    ],
    "20260813": [
        {
            "name": "本地大模型部署方案选型",
            "lifecycle": "sustained",
            "background": "延续首日讨论，林远舟完成实际部署验证。",
            "process": "ollama + OpenWebUI 跑通；中文摘要通过在 prompt 中明确「按要点分行」的输出格式获得稳定效果。",
            "conclusion": "方案验证通过，进入稳定使用阶段。",
            "participants": ["林远舟", "苏砚清"],
            "trend": "从方案对比推进到实际部署验证，中文摘要输出格式问题已解决。",
            "confidence": 0.95,
        },
        {
            "name": "检索增强生成的时效性",
            "lifecycle": "emerging",
            "background": "周听澜提出知识库更新滞后导致模型答案失真的问题。",
            "process": "程亦凡分享实践：为每条知识打时间戳，检索按半衰期（两周）衰减加权，为业务调参所得；附实验记录文档。",
            "conclusion": "时间戳 + 半衰期衰减是可落地的缓解方案；更系统的治理待讨论。",
            "participants": ["周听澜", "程亦凡", "顾栖桐"],
            "trend": "识别出 RAG 时效性问题，获得一个工程缓解方案。",
            "confidence": 0.85,
        },
        {
            "name": "Agent 编排框架对比",
            "lifecycle": "sustained",
            "background": "延续首日评估。",
            "process": "顾栖桐决定以 LangGraph 做原型；共识是先画清楚状态机再写代码。",
            "conclusion": "进入原型阶段。",
            "participants": ["顾栖桐", "程亦凡"],
            "trend": "从选型收敛到原型启动，方法上强调先设计状态机。",
            "confidence": 0.85,
        },
    ],
    "20260814": [
        {
            "name": "本地大模型部署方案选型",
            "lifecycle": "concluded",
            "background": "三日议题收尾。",
            "process": "最终方案确定：ollama 7B-Q4 + OpenWebUI + tailscale 内网穿透；苏砚清提醒接口鉴权，已加 API key 网关。",
            "conclusion": "方案落地完成，安全加固到位，议题关闭。",
            "participants": ["林远舟", "苏砚清"],
            "trend": "三天内完成「选型 → 验证 → 落地 + 安全加固」全流程闭环。",
            "confidence": 0.95,
        },
        {
            "name": "团队知识库立项",
            "lifecycle": "emerging",
            "background": "周听澜提议把群内讨论沉淀为团队知识库，避免知识散落在聊天记录中。",
            "process": "共识是先定分类体系再选工具；林远舟荐读知识蒸馏综述作为首批资料；程亦凡提议周五会议定分类。",
            "conclusion": "立项启动，周五会议定分类体系。",
            "participants": ["周听澜", "苏砚清", "林远舟", "顾栖桐", "程亦凡"],
            "trend": "立项启动，进入分类体系设计阶段。",
            "confidence": 0.9,
        },
    ],
}

RESOURCES: dict[str, list[dict]] = {
    "20260812": [
        {
            "title": "2026 年本地模型部署栈盘点",
            "content": "https://example.com/blog/local-llm-stack-2026",
            "resource_type": "article",
            "shared_by": "苏砚清",
            "summary": "盘点本地部署技术栈，含异构显卡调度章节。",
        },
    ],
    "20260813": [
        {
            "title": "rag-decay-notes.pdf",
            "content": "检索衰减实验记录：时间戳 + 半衰期加权方案（虚构示例文件）",
            "resource_type": "document",
            "shared_by": "程亦凡",
            "summary": "RAG 时效性缓解方案的实验数据。",
        },
    ],
    "20260814": [
        {
            "title": "面向团队场景的知识蒸馏综述",
            "content": "https://example.com/papers/knowledge-distillation-survey",
            "resource_type": "paper",
            "shared_by": "林远舟",
            "summary": "团队知识库建设参考的第一批资料。",
        },
    ],
}

DAILY_OVERVIEWS: dict[str, dict] = {
    "20260812": {
        "overview": "群内两条主线：本地大模型部署选型（四方案对比，初步倾向 ollama）与 Agent 编排框架评估（倾向图编排做原型）。附一篇本地部署栈盘点文章。",
        "highlights": ["本地部署四方案对比初步收敛到 ollama", "Agent 框架选型关注可观测性"],
        "notice": [],
        "trend": "技术选型日：两条主线同时开启，均处于方案对比阶段。",
    },
    "20260813": {
        "overview": "本地部署完成实际验证（ollama + OpenWebUI，中文摘要格式问题解决）；新开 RAG 时效性议题（时间戳 + 半衰期缓解）；Agent 框架进入原型阶段。",
        "highlights": ["本地部署验证通过", "RAG 时效性议题新增工程缓解方案", "Agent 框架原型启动"],
        "notice": [],
        "trend": "推进日：选型类议题从讨论走向验证，新增一条 RAG 工程议题。",
    },
    "20260814": {
        "overview": "本地部署议题闭环（含安全加固）；Agent 原型跑通约 200 行；团队知识库正式立项，周五定分类体系。",
        "highlights": ["本地部署全流程闭环", "知识库立项启动"],
        "notice": ["周五知识库分类体系讨论会"],
        "trend": "收敛日：一条议题闭环、一条立项启动，群内知识沉淀意识明显增强。",
    },
}

MODEL_USED = "example-mock-model（示例数据，非真实生成）"


# ----------------------------------------------------------------------------
# 写入逻辑
# ----------------------------------------------------------------------------


def _iso(date8: str) -> str:
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"


async def seed() -> None:
    settings = get_settings()
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_database(str(db_path))

    async with aiosqlite.connect(str(db_path)) as db:
        # ── 幂等：清示例群旧数据 ──────────────────────────────
        await db.execute("DELETE FROM raw_messages WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM parsed_contexts WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM topic_summaries WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM report_versions WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM pipeline_runs WHERE group_id = ?", (DISPLAY_NAME,))
        await db.execute(
            "DELETE FROM groups WHERE group_id = ? OR chatroom_id = ?", (GROUP_ID, CHATROOM_ID)
        )

        # ── 注册群组 ──────────────────────────────────────────
        await db.execute(
            "INSERT INTO groups (group_id, display_name, chatroom_id, is_active, created_by)"
            " VALUES (?, ?, ?, 1, 'sample-seed')",
            (GROUP_ID, DISPLAY_NAME, CHATROOM_ID),
        )
        # 群成员缓存
        for i, m in enumerate(MEMBERS, start=1):
            await db.execute(
                "INSERT OR REPLACE INTO group_members (member_id, group_id, name, wxid, role)"
                " VALUES (?, ?, ?, ?, 'member')",
                (f"member-demo-{i:02d}", GROUP_ID, m, f"wxid_demo_{i:05d}"),
            )

        # ── 逐日写 L1 / L2 / L3 ───────────────────────────────
        for date8 in DAYS:
            msgs = MESSAGES[date8]

            # L1 raw_messages
            for server_id, sender, mtype, content in msgs:
                await db.execute(
                    "INSERT OR REPLACE INTO raw_messages"
                    " (serverID, date, group_id, sender, content, msg_type, raw_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        server_id,
                        _iso(date8),
                        GROUP_ID,
                        sender,
                        content,
                        mtype,
                        json.dumps(
                            {"serverID": server_id, "sender": sender, "type": mtype},
                            ensure_ascii=False,
                        ),
                    ),
                )

            # L2 parsed_contexts（每天 2 块）
            mid = len(msgs) // 2
            for i, chunk in enumerate([msgs[:mid], msgs[mid:]]):
                ctx_text = "\n".join(f"{s}: {c}" for _, s, _, c in chunk)
                await db.execute(
                    "INSERT OR REPLACE INTO parsed_contexts"
                    " (context_id, date, group_id, server_ids, context_text, token_count,"
                    "  source_subagent)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"ctx-demo-{date8}-{i + 1}",
                        _iso(date8),
                        GROUP_ID,
                        json.dumps([sid for sid, *_ in chunk]),
                        ctx_text,
                        len(ctx_text) // 2,
                        "content_enrich",
                    ),
                )

            # L3 topic_summaries（summary_id 确定性，保证重跑幂等）
            context_ids = json.dumps([f"ctx-demo-{date8}-1", f"ctx-demo-{date8}-2"])
            for ti, t in enumerate(TOPICS[date8], start=1):
                await db.execute(
                    "INSERT OR REPLACE INTO topic_summaries"
                    " (summary_id, date, group_id, topic_name, summary_text, context_ids,"
                    "  source_server_ids, confidence, model_used, lifecycle,"
                    "  background, process, conclusion, participants, trend)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"sum-{GROUP_ID}-{date8}-{ti:02d}",
                        _iso(date8),
                        GROUP_ID,
                        t["name"],
                        f"{t['background']}{t['process']}{t['conclusion']}",
                        context_ids,
                        json.dumps([sid for sid, *_ in msgs]),
                        t["confidence"],
                        MODEL_USED,
                        t["lifecycle"],
                        t["background"],
                        t["process"],
                        t["conclusion"],
                        json.dumps(t["participants"], ensure_ascii=False),
                        t["trend"],
                    ),
                )

            # report_versions（每天 v1）
            report_id = f"{GROUP_ID}-{date8}"
            await db.execute(
                "INSERT OR REPLACE INTO report_versions"
                " (version_id, report_id, group_id, date, version_number, source,"
                "  is_active, created_at)"
                " VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
                (f"{report_id}-v1", report_id, GROUP_ID, date8, 1, f"{_iso(date8)}T22:00:00"),
            )

            # pipeline_runs（群维度；注意此表 group_id 列存 display_name）
            await db.execute(
                "INSERT OR REPLACE INTO pipeline_runs"
                " (run_id, component, status, started_at, completed_at, message_count,"
                "  group_id, date)"
                " VALUES (?, 'full_pipeline', 'completed', ?, ?, ?, ?, ?)",
                (
                    f"run-demo-{date8}",
                    f"{_iso(date8)}T21:50:00",
                    f"{_iso(date8)}T22:00:00",
                    len(msgs),
                    DISPLAY_NAME,
                    date8,
                ),
            )

            # L3 JSON 文件（data/processed/{gid}/{date}/v1/）
            l3_dir = Path(settings.layer3_output_dir) / GROUP_ID / date8 / "v1"
            l3_dir.mkdir(parents=True, exist_ok=True)
            ov = DAILY_OVERVIEWS[date8]
            daily = {
                "report_type": "daily",
                "group_id": GROUP_ID,
                "date": date8,
                "generated_at": f"{_iso(date8)}T22:00:00",
                "model_used": MODEL_USED,
                "overview": ov["overview"],
                "highlights": ov["highlights"],
                "notice": ov["notice"],
                "trend": ov["trend"],
                "active_members": sorted(
                    {p for t in TOPICS[date8] for p in t["participants"]}
                ),
            }
            (l3_dir / "daily.json").write_text(
                json.dumps(daily, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            topics_doc = {
                "group_id": GROUP_ID,
                "date": date8,
                "model_used": MODEL_USED,
                "topics": [
                    {
                        "name": t["name"],
                        "lifecycle": t["lifecycle"],
                        "background": t["background"],
                        "process": t["process"],
                        "conclusion": t["conclusion"],
                        "participants": t["participants"],
                        "trend": t["trend"],
                    }
                    for t in TOPICS[date8]
                ],
            }
            (l3_dir / "topics.json").write_text(
                json.dumps(topics_doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            resources_doc = {
                "group_id": GROUP_ID,
                "date": date8,
                "model_used": MODEL_USED,
                "resources": RESOURCES[date8],
            }
            (l3_dir / "resources.json").write_text(
                json.dumps(resources_doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        await db.commit()

    print("✅ 示例数据已写入（完全虚构）：")
    print(f"   群组：{DISPLAY_NAME}（{GROUP_ID} / {CHATROOM_ID}）")
    print(f"   日期：{_iso(DAYS[0])} ~ {_iso(DAYS[-1])}（{len(DAYS)} 天）")
    print(f"   消息 {sum(len(m) for m in MESSAGES.values())} 条 / 议题 "
          f"{sum(len(t) for t in TOPICS.values())} 条 / 资源 "
          f"{sum(len(r) for r in RESOURCES.values())} 条")
    print()
    print("下一步：poetry run winnow web  →  http://127.0.0.1:8100/ui/")
    print("清除示例数据：poetry run python examples/sample-data/seed.py --clean")


async def clean() -> None:
    settings = get_settings()
    async with aiosqlite.connect(str(settings.db_path)) as db:
        await db.execute("DELETE FROM raw_messages WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM parsed_contexts WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM topic_summaries WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM report_versions WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM pipeline_runs WHERE group_id = ?", (DISPLAY_NAME,))
        await db.execute("DELETE FROM group_members WHERE group_id = ?", (GROUP_ID,))
        await db.execute("DELETE FROM groups WHERE group_id = ?", (GROUP_ID,))
        await db.commit()
    proc_dir = Path(settings.layer3_output_dir) / GROUP_ID
    if proc_dir.exists():
        shutil.rmtree(proc_dir)
    print("🧹 示例数据已清除")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        asyncio.run(clean())
    else:
        asyncio.run(seed())
