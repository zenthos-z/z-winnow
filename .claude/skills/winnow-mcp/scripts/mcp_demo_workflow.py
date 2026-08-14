#!/usr/bin/env python3
"""winnow MCP 完整任务编排 demo（Python FastMCP client）。

演示一次完整的消费者任务:
  ① list_groups        列可见群，选一个 group_id
  ② search_topics      关键词搜议题
  ③ get_topic          看议题详情 + 跨天演化 + 已有反馈
  ④ get_daily_report   看某天日报
  ⑤ submit_feedback    对命中的议题提一条 supplement 演示反馈

可作二次开发模板。所有参数从环境变量 / 命令行读，key 不入命令行。

用法:
  export WINNOW_MCP_KEY=wn_xxx
  python mcp_demo_workflow.py --query "世界模型" --date 20260720
  python mcp_demo_workflow.py --group-id g_xxx --query "因子" --date 20260720 --skip-feedback
"""
from __future__ import annotations

import argparse
import json
import os
import sys

URL = os.environ.get("WINNOW_MCP_URL", "https://mcp.example.com/mcp")


def _text(result) -> str:
    """从 call_tool 结果提取文本（FastMCP content block → JSON 字符串）。"""
    content = getattr(result, "content", None) or []
    return "\n".join(getattr(c, "text", "") for c in content if getattr(c, "text", None))


def _jload(result):
    """call_tool 结果 → 反序列化的 Python 对象。"""
    txt = _text(result)
    return json.loads(txt) if txt else None


async def main() -> int:
    ap = argparse.ArgumentParser(description="winnow MCP 任务编排 demo")
    ap.add_argument("--query", default="世界模型", help="检索关键词")
    ap.add_argument("--date", required=True, help="日期 YYYYMMDD（日报/反馈锚点）")
    ap.add_argument("--group-id", default=None, help="群 ID；省略则用第一个可见群")
    ap.add_argument("--skip-feedback", action="store_true", help="跳过提反馈步骤")
    args = ap.parse_args()

    key = os.environ.get("WINNOW_MCP_KEY", "")
    if not key:
        print("❌ 未设置 WINNOW_MCP_KEY", file=sys.stderr)
        return 2

    try:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
    except ImportError:
        print("❌ 未安装 fastmcp。本仓库用: poetry run python mcp_demo_workflow.py")
        return 3

    transport = StreamableHttpTransport(URL, headers={"x-api-key": key})
    async with Client(transport) as client:
        # ① 列群
        print("① list_groups")
        groups = _jload(await client.call_tool("list_groups", {})) or []
        if not groups:
            print("   该 key 无可见群（检查 allowed_groups 或让提供方 mcp-key allow）")
            return 1
        for g in groups:
            print(f"   - {g['group_id']}  {g.get('display_name', '')}")
        gid = args.group_id or groups[0]["group_id"]
        print(f"   → 选用 {gid}\n")

        # ② 搜议题
        print(f"② search_topics(query={args.query!r})")
        topics = _jload(await client.call_tool("search_topics", {
            "query": args.query, "group_id": gid, "limit": 5,
        })) or []
        if not topics:
            print("   无命中（换个关键词，或找提供方确认数据是否已更新）")
            sid = None
        else:
            for t in topics:
                print(f"   - [{t.get('date')}] {t.get('topic_name', '?')}  → {t['summary_id']}")
            sid = topics[0]["summary_id"]
        print()

        # ③ 看议题演化 + 反馈
        if sid:
            print(f"③ get_topic(summary_id={sid})")
            topic = _jload(await client.call_tool("get_topic", {"summary_id": sid})) or {}
            detail = topic.get("detail", {})
            timeline = topic.get("timeline", [])
            feedback = topic.get("feedback", [])
            print(f"   议题: {detail.get('topic_name', '?')}   生命周期: {detail.get('lifecycle', '?')}")
            conclusion = detail.get("conclusion") or "（无结论）"
            print(f"   结论: {str(conclusion)[:80]}")
            print(f"   时间线: {len(timeline)} 条跨天记录   反馈: {len(feedback)} 条")
            print()

        # ④ 看日报
        print(f"④ get_daily_report(group_id={gid}, date={args.date})")
        report = _jload(await client.call_tool("get_daily_report", {
            "group_id": gid, "date": args.date,
        })) or {}
        if report.get("error"):
            print(f"   {report['error']}（确认该日期已生成日报，或找提供方确认数据已更新）")
        else:
            content = report.get("content", {}) or {}
            ov = content.get("overview") or {}
            print(f"   版本: {report.get('version')}   overview: {str(ov)[:100]}")
            print(f"   topics: {len(content.get('topics', []))} 条   "
                  f"resources: {len(content.get('resources', []))} 条")
        print()

        # ⑤ 提反馈（演示 supplement）
        if args.skip_feedback:
            print("⑤ 跳过反馈（--skip-feedback）")
        elif sid:
            print(f"⑤ submit_feedback（对议题 {sid} 提一条 supplement 演示反馈）")
            fb = _jload(await client.call_tool("submit_feedback", {
                "group_id": gid,
                "date": args.date,
                "target_type": "topic",
                "signal": "supplement",
                "content": "（demo 演示反馈）该议题还可补充……",
                "target_topic_id": sid,
            }))
            print(f"   → {fb}")
            print("   反馈已提交，由平台择期处理（不会立即改变已读内容）")
        else:
            print("⑤ 无议题命中，跳过反馈")

    print("\n✅ demo 完成")
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
