#!/usr/bin/env python3
"""winnow MCP 连接 + 鉴权 + 权限自检（Python FastMCP client）。

读 WINNOW_MCP_KEY 环境变量，依次验证:
  ① 服务可达 + key 有效（ping）
  ② 6 个工具可见（list_tools）
  ③ key 权限生效（list_groups 返回可见群）

用法:
  export WINNOW_MCP_KEY=wn_xxx
  python mcp_client_check.py
  # 本仓库内: poetry run python .claude/skills/winnow-mcp/scripts/mcp_client_check.py
  # 自定义 endpoint: WINNOW_MCP_URL=https://host/mcp python mcp_client_check.py
"""
from __future__ import annotations

import os
import sys

URL = os.environ.get("WINNOW_MCP_URL", "https://mcp.example.com/mcp")
EXPECTED_TOOLS = {
    "list_groups",
    "search_topics",
    "get_topic",
    "get_daily_report",
    "list_resources",
    "submit_feedback",
}


def _check_env() -> str:
    key = os.environ.get("WINNOW_MCP_KEY", "")
    if not key:
        print("❌ 未设置 WINNOW_MCP_KEY 环境变量")
        print("   export WINNOW_MCP_KEY=wn_xxxxxxxxxxxxxxxxxxxxxxxx")
        sys.exit(2)
    return key


def _parse_groups(result) -> list[dict] | None:
    """从 call_tool 结果解析群组列表（兼容 FastMCP CallToolResult.content 结构）。

    tool 返回 list[dict] → FastMCP 序列化成 TextContent(text=json)。
    """
    import json

    try:
        content = getattr(result, "content", None) or []
        for c in content:
            txt = getattr(c, "text", None)
            if txt:
                data = json.loads(txt)
                if isinstance(data, list):
                    return data
        if isinstance(result, list):
            return result
    except Exception:
        return None
    return None


async def main() -> int:
    key = _check_env()
    print(f"▸ 目标: {URL}")
    print(f"▸ Key:  {key[:8]}...（脱敏）\n")

    try:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
    except ImportError:
        print("❌ 未安装 fastmcp。本仓库用: poetry run python mcp_client_check.py")
        return 3

    transport = StreamableHttpTransport(URL, headers={"x-api-key": key})
    try:
        async with Client(transport) as client:
            # ① ping（连通 + 鉴权）
            try:
                await client.ping()
                print("① ping            ✅ 连接 + 鉴权通过")
            except Exception as e:
                print(f"① ping            ❌ {e}")
                return 1

            # ② list_tools
            tools = await client.list_tools()
            names = {t.name for t in tools}
            missing = EXPECTED_TOOLS - names
            extra = names - EXPECTED_TOOLS
            if missing:
                print(f"② list_tools      ❌ 缺工具: {sorted(missing)}")
            else:
                print(f"② list_tools      ✅ 6 工具齐全（共 {len(names)} 个）")
                if extra:
                    print(f"   额外工具: {sorted(extra)}")

            # ③ list_groups（验证权限生效）
            res = await client.call_tool("list_groups", {})
            groups = _parse_groups(res)
            if groups is None:
                print("③ list_groups     ⚠️  返回解析失败")
            else:
                print(f"③ list_groups     ✅ {len(groups)} 个可见群")
                for g in groups[:5]:
                    print(f"      - {g.get('group_id')}  {g.get('display_name', '')}")
                if len(groups) > 5:
                    print(f"      ... 还有 {len(groups) - 5} 个")

            ok = not missing
            print("\n✅ 自检通过" if ok else "\n❌ 有缺失工具，检查 server 版本")
            return 0 if ok else 1
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Invalid" in msg or "unknown API key" in msg:
            print(f"❌ 鉴权失败（key 无效）: {msg}")
        elif "403" in msg or "无权" in msg:
            print(f"❌ 权限不足: {msg}")
        else:
            print(f"❌ 连接失败: {msg}")
        return 1


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
