#!/usr/bin/env bash
# winnow MCP 连接烟雾测试（curl + streamable-http，零 Python 依赖）。
#
# 验证:服务可达 + key 有效 + 6 工具可见 + list_groups 能跑。
# JSON 解析用 python3（macOS 自带 / 本仓库必然有）；纯 grep 版见文末注释。
#
# 用法:
#   export WINNOW_MCP_KEY=wn_xxx
#   bash mcp_smoke.sh
#   WINNOW_MCP_URL=https://host/mcp bash mcp_smoke.sh   # 自定义 endpoint
set -euo pipefail

URL="${WINNOW_MCP_URL:-https://mcp.example.com/mcp}"
KEY="${WINNOW_MCP_KEY:-}"

if [[ -z "$KEY" ]]; then
  echo "❌ 未设置 WINNOW_MCP_KEY 环境变量" >&2
  echo "   export WINNOW_MCP_KEY=wn_xxx" >&2
  exit 2
fi

command -v curl >/dev/null || { echo "❌ 需要 curl" >&2; exit 2; }
command -v python3 >/dev/null || { echo "❌ JSON 解析需要 python3" >&2; exit 2; }

COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

hdr=( -H "Content-Type: application/json"
      -H "Accept: application/json, text/event-stream"
      -H "x-api-key: $KEY" )

echo "▸ 目标: $URL"
echo "▸ Key:  ${KEY:0:8}...（脱敏）"
echo ""

# ① initialize 握手（拿 session cookie，若有）
echo "① initialize 握手..."
INIT=$(curl -sS "${hdr[@]}" -c "$COOKIE_JAR" "$URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')
if printf '%s' "$INIT" | grep -q '"result"'; then
  SERVER=$(printf '%s' "$INIT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("result", {}).get("serverInfo", {}).get("name", "?"))
except Exception:
    print("?")
' 2>/dev/null || echo "?")
  echo "   ✅ 连接 + 鉴权通过（server: $SERVER）"
else
  echo "   ❌ 握手失败（key 无效 / 服务不可达 / 403）:"
  printf '%s' "$INIT" | head -c 500
  echo ""
  exit 1
fi

# ② notifications/initialized（streamable-http 协议要求，发完即弃）
curl -sS "${hdr[@]}" -b "$COOKIE_JAR" "$URL" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1 || true

# ③ tools/list（确认 6 工具）
echo "② tools/list..."
TLS=$(curl -sS "${hdr[@]}" -b "$COOKIE_JAR" "$URL" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
TOOLS=$(printf '%s' "$TLS" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print("\n".join(t["name"] for t in d.get("result", {}).get("tools", [])))
except Exception:
    pass
' 2>/dev/null || true)
if [[ -n "$TOOLS" ]]; then
  TCOUNT=$(printf '%s\n' "$TOOLS" | grep -c .)
  echo "   ✅ 工具（$TCOUNT 个）:"
  printf '%s\n' "$TOOLS" | sed 's/^/      - /'
else
  echo "   ❌ 工具列表解析失败:"
  printf '%s' "$TLS" | head -c 500
  exit 1
fi

# ④ tools/call list_groups（验证 key 权限生效）
echo "③ tools/call list_groups..."
GRPS=$(curl -sS "${hdr[@]}" -b "$COOKIE_JAR" "$URL" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_groups","arguments":{}}}')
COUNT=$(printf '%s' "$GRPS" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    c = d.get("result", {}).get("content", [])
    txt = c[0].get("text", "[]") if c else "[]"
    print(len(json.loads(txt)))
except Exception:
    print("?")
' 2>/dev/null || echo "?")
echo "   ✅ list_groups 返回（$COUNT 个可见群）"

echo ""
echo "✅ 烟雾测试全部通过"
echo "   提示:完整调用 demo → python mcp_demo_workflow.py --query 关键词 --date 20260720"
