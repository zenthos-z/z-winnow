# winnow MCP 客户端配置（可直接复制）

所有客户端共用这三个参数：

| 项 | 值 |
|----|----|
| **URL** | `https://mcp.example.com/mcp` |
| **transport** | `streamable-http`（MCP 规范传输） |
| **鉴权 header** | `x-api-key: wn_xxx`（推荐）；或 `Authorization: Bearer wn_xxx` |

> **安全**：key 统一从环境变量 `WINNOW_MCP_KEY` 读，不要把明文 key 提交进任何配置文件 / git。
> ```bash
> export WINNOW_MCP_KEY=wn_xxxxxxxxxxxxxxxxxxxxxxxx
> ```

---

## 1. OpenClaw

官方文档：<https://docs.openclaw.ai/zh-CN/cli/mcp>

**CLI 方式**（推荐，会自动 probe 验证）：

```bash
export WINNOW_MCP_KEY=wn_xxx
openclaw mcp add winnow \
  --url https://mcp.example.com/mcp \
  --transport streamable-http \
  --header "x-api-key: $WINNOW_MCP_KEY"
openclaw mcp doctor --probe    # 验证连接 + 列出工具
```

**或直接编辑 `~/.openclaw/openclaw.json`**：

```json
{
  "mcp": {
    "servers": {
      "winnow": {
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable-http",
        "headers": { "x-api-key": "<your-key>" },
        "timeout": 20,
        "connectTimeout": 5
      }
    }
  }
}
```

注意：
- transport 规范名是 `streamable-http`（CLI 原生的 `type: "http"` 会被自动规范化）
- `openclaw mcp doctor` 会**警告**配置里的明文敏感 header —— 尽量用 CLI + `$WINNOW_MCP_KEY` 展开，别提交明文
- 验证命令：`openclaw mcp probe winnow`（建实时连接，列出 6 个工具）

---

## 2. Hermes Agent（Nous Research）

官方文档：<https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/features/mcp>

编辑 `~/.hermes/config.yaml` 的 `mcp_servers`（HTTP 模式，Hermes 同时支持 stdio + HTTP）：

```yaml
mcp_servers:
  winnow:
    url: "https://mcp.example.com/mcp"
    headers:
      x-api-key: "wn_xxx"        # 或用 shell 展开 / Hermes 的变量插值（别提交明文进 git）
    timeout: 20
    connect_timeout: 5
    # 可选：只暴露需要的工具
    # tools:
    #   include: [list_groups, search_topics, get_topic, get_daily_report, list_resources, submit_feedback]
```

改完在 Hermes 会话里执行 `/reload-mcp` 重载。

注意：
- Hermes 给 MCP 工具加前缀 `mcp__<server>_<tool>`，如 `mcp__winnow_search_topics`——正常推理时 Hermes 自动选用，不用手写全名
- 工具集名 `mcp-winnow`
- 排错：`cd ~/.hermes/hermes-agent && uv pip install -e ".[mcp]"` 确保 MCP 依赖装了

---

## 3. 腾讯 WorkBuddy（CodeBuddy）

官方文档：<https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide>

> ⚠️ **未实测**：WorkBuddy 官方文档只给了 **stdio** 的 `mcp.json` 样例（`command`/`args`/`env`），**远程 HTTP + 自定义 header 的确切字段名未文档化**。下面是最可能的配置，需在 WorkBuddy GUI 里按实际字段微调。

配置文件位置：用户级 `~/.workbuddy/mcp.json`（所有项目复用）或项目级 `<项目目录>/.workbuddy/mcp.json`。

```json
{
  "mcpServers": {
    "winnow": {
      "url": "https://mcp.example.com/mcp",
      "type": "http",
      "headers": {
        "x-api-key": "wn_xxx"
      }
    }
  }
}
```

GUI 操作路径：**WorkBuddy → 侧边栏「插件」→ 右上角「MCP 服务器」→「配置 MCP」→ 粘贴 JSON → 保存**。保存后看状态灯：🟢 绿 = 连接成功；🔴 红 = 配置异常。

**字段名 fallback**：若 `"type": "http"` 不被识别，依次尝试：
- `"type": "streamableHttp"`
- 在 UI 里选「HTTP」传输方式 + 填 URL + 在「鉴权」处填 header 名 `x-api-key` + 值

---

## 4. Codex CLI（OpenAI）

官方源码（字段定义的最权威来源）：<https://github.com/openai/codex/blob/main/codex-rs/config/src/mcp_types.rs>

编辑 `~/.codex/config.toml`。Codex 原生支持 streamable-http，且专门为安全传凭证设计了 `env_http_headers`：

```toml
[mcp_servers.winnow]
url = "https://mcp.example.com/mcp"
env_http_headers = { "x-api-key" = "WINNOW_MCP_KEY" }
```

然后 `export WINNOW_MCP_KEY=wn_xxx`。

**源码级行为**（来自 `mcp_types.rs` + `rmcp-client/src/utils.rs`）：

- 有 `url` 字段 → 自动识别为 `streamable-http` 传输；有 `command` → stdio
- `env_http_headers` 的 **value 是环境变量名**，Codex 运行时用 `std::env::var()` 读取；环境变量未设或为空 → 该 header **静默跳过**（不报错）
- `http_headers` 是**明文静态 header**（不推荐放 key）
- 备选：用 `bearer_token_env_var = "WINNOW_MCP_KEY"`，Codex 自动加 `Authorization: Bearer <key>`（server 端兼容）
- ⚠️ `streamable-http` transport **禁止** `env` / `args` / `cwd` / `bearer_token` 字段——写了会校验报错（`env` 只能用于 stdio）
- 改完 `config.toml`，app-server 用 `config/mcpServer/reload` 热重载

---

## 5. Python（FastMCP client）

项目已依赖 `fastmcp`（v3）。最小连接 + 调用：

```python
import asyncio
import os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

URL = "https://mcp.example.com/mcp"
KEY = os.environ["WINNOW_MCP_KEY"]

async def main():
    async with Client(
        transport=StreamableHttpTransport(URL, headers={"x-api-key": KEY}),
    ) as client:
        await client.ping()                              # 连通 + 鉴权
        tools = await client.list_tools()                # 应看到 6 个工具
        print([t.name for t in tools])
        groups = await client.call_tool("list_groups", {})
        print(groups)

asyncio.run(main())
```

备选鉴权：`Client(URL, auth=BearerAuth(KEY))` 走 `Authorization: Bearer`（server 兼容，但 `x-api-key` header 更原生）。

完整编排 demo 见 `scripts/mcp_demo_workflow.py`。

---

## 6. curl（最小烟雾，零依赖）

streamable-http 是 JSON-RPC over HTTP。最小 `initialize` 握手（验证服务可达 + key 有效）：

```bash
export WINNOW_MCP_KEY=wn_xxx
curl -sS https://mcp.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "x-api-key: $WINNOW_MCP_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

返回 JSON-RPC `result`（含 `serverInfo`）即通过。完整可跑版本（含 session 头处理 + `tools/call`）见 `scripts/mcp_smoke.sh`。

---

## 7. fastmcp-remote 通用桥（stdio-only 客户端兜底）

**Claude Desktop / Cursor / 其他只支持本地 stdio MCP 的客户端**连不了远程 http MCP。用 `uvx fastmcp-remote` 把远程 http MCP 桥成 stdio 进程。

Claude Desktop 的 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "winnow": {
      "command": "uvx",
      "args": [
        "fastmcp-remote",
        "https://mcp.example.com/mcp",
        "--header", "x-api-key: wn_xxx"
      ]
    }
  }
}
```

⚠️ 注意：
- `--header` 的值这里写明文 key（`fastmcp-remote` 不自动展开 `${ENV}`）。**这个配置文件别提交 git**
- 想用环境变量隔离 key：把 key 放进 `env`，再用支持 env 插值的客户端；或包一层 shell 脚本导出 `$WINNOW_MCP_KEY` 后拼进 `--header`
- 需本机有 `uvx`（`uv` 自带）：`brew install uv` 或 `pip install uv`

---

## 鉴权 header 速查

两种 header 都可用，**推荐 `x-api-key`**：

1. `x-api-key: wn_xxx` → 推荐
2. `Authorization: Bearer wn_xxx` → 兼容

---

## 官方文档来源

- OpenClaw：<https://docs.openclaw.ai/zh-CN/cli/mcp>
- Hermes Agent：<https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/features/mcp>
- WorkBuddy：<https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide>
- Codex CLI：<https://github.com/openai/codex>（`codex-rs/config/src/mcp_types.rs`）
- FastMCP：<https://gofastmcp.com>（clients / transports / auth）
- MCP 协议规范：<https://modelcontextprotocol.io>
