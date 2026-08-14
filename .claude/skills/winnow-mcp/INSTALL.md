---
agent_instructions: |
  这是一份**对话式逐步引导**，不是一次性参考文档。你必须用对话形式辅助用户完成部署，
  不要一次性展示所有步骤。按以下方式执行：
  1. 先介绍这个系统是什么，问用户准备好了没有
  2. 每步只展示 Step N 的内容，完成后再展示 Step N+1
  3. 完成确认后，自然过渡到下一步，不要说"展开下一步"——用户看不到折叠块
  4. 失败时帮助排查，不要跳过
  5. 全部完成后总结并询问用户下一步
---

# winnow MCP 接入引导

> 这是给**消费方 Agent** 的对话式引导。**不要一次性展示所有步骤**。
> 用提问推进：问一步，做一步，确认了再下一步。

## 你需要准备的

- 一个 MCP 兼容客户端（Claude Desktop / OpenClaw / Cursor / 任何 HTTP 客户端）
- 一个网络环境（能访问 `https://mcp.example.com`）

---

## 逐步引导

<details open>
<summary><strong>▸ Step 1：获取 API key</strong></summary>

### 你需要做的事

找 winnow 平台提供方申请一个 key，格式为 `wn_xxx`。同时告知对方你**需要访问哪些群**。

Key 分两种：

| 类型 | 权限 |
|------|------|
| 普通 key | 只能访问授权的群；提反馈时身份由 key 自动绑定 |
| 管理员 key | 全权访问所有群 |

### key 的安全规则

- 只从**环境变量**读，不要写进代码 / git / 聊天截图
- 拿到后立即设到环境里，后续步骤都假设它已经在环境变量中

```bash
export WINNOW_MCP_KEY=wn_xxxxxxxxxxxxxxxxxxxxxxxx
```

### ✅ 完成确认

> 你已经拿到了 `wn_xxx` 格式的 key，并设为了 `$WINNOW_MCP_KEY`。
>
> 还不知道能访问哪些群？没关系，下一步验证时就知道了。
>
> **确认后展开 Step 2。**

</details>

<details>
<summary><strong>▸ Step 2：验证连通性</strong></summary>

### 你需要做的事

在**配置任何客户端之前**，先用一个零依赖的 curl 命令确认服务可达、key 有效。

```bash
curl -s https://mcp.example.com/mcp \
  -H "x-api-key: $WINNOW_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}' \
  | python3 -m json.tool
```

### 正常输出

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      { "name": "list_groups", ... },
      { "name": "search_topics", ... },
      { "name": "get_topic", ... },
      { "name": "get_daily_report", ... },
      { "name": "list_resources", ... },
      { "name": "submit_feedback", ... }
    ]
  }
}
```

看到 6 个工具即 `✅ 连通`。

### 如果失败了

| 现象 | 原因 |
|------|------|
| `curl: (28) Connection timed out` | 你的网络无法到达 `mcp.example.com` — 检查网络/防火墙 |
| `401 Unauthorized` 或 `Invalid API key` | key 无效 / 拼错 / 未设环境变量 |
| 空结果 / `method not found` | MCP 协议不匹配 — 确认用的 streamable-http |

### ✅ 完成确认

> curl 返回了 6 个工具的列表，服务可达、key 有效。
>
> **确认后展开 Step 3。**

</details>

<details>
<summary><strong>▸ Step 3：配置 MCP 客户端</strong></summary>

### 你需要做的事

在你的 MCP 客户端中注册远程服务。所有客户端都填这三个参数：

| 参数 | 值 |
|------|-----|
| **URL** | `https://mcp.example.com/mcp` |
| **transport** | `streamable-http` |
| **鉴权 header** | `x-api-key: $WINNOW_MCP_KEY` |

> key 从环境变量读，**不**硬编码进配置文件。

以下是各客户端的可直接复制配置。

<details>
<summary><strong>OpenClaw</strong></summary>

```bash
export WINNOW_MCP_KEY=wn_xxx
openclaw mcp add winnow \
  --url https://mcp.example.com/mcp \
  --transport streamable-http \
  --header "x-api-key: $WINNOW_MCP_KEY"
openclaw mcp doctor --probe
```

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

Claude Desktop 不支持直接连远程 HTTP。需要桥接工具：

```bash
# 安装桥接工具
uv tool install fastmcp-remote

# 在 claude_desktop_config.json 中添加：
{
  "mcpServers": {
    "winnow": {
      "command": "fastmcp-remote",
      "args": [
        "--url", "https://mcp.example.com/mcp",
        "--header", "x-api-key: $WINNOW_MCP_KEY"
      ]
    }
  }
}
```

</details>

<details>
<summary><strong>Cursor</strong></summary>

Cursor 同样需要桥接：

```json
{
  "mcpServers": {
    "winnow": {
      "command": "fastmcp-remote",
      "args": [
        "--url", "https://mcp.example.com/mcp",
        "--header", "x-api-key: $WINNOW_MCP_KEY"
      ]
    }
  }
}
```

</details>

<details>
<summary><strong>Codex CLI</strong></summary>

```bash
export WINNOW_MCP_KEY=wn_xxx
codex mcp add winnow \
  --url https://mcp.example.com/mcp \
  --transport streamable-http \
  --header "x-api-key: $WINNOW_MCP_KEY"
```

</details>

<details>
<summary><strong>Hermes / WorkBuddy / 其他 streamable-http 客户端</strong></summary>

这些原生支持远程 MCP，直接在配置界面填：
- URL: `https://mcp.example.com/mcp`
- Transport: `streamable-http`
- Header: `x-api-key: $WINNOW_MCP_KEY`

</details>

<details>
<summary><strong>纯 curl（无客户端）</strong></summary>

没有 MCP 客户端也能用，只是每次要手写 JSON-RPC：

```bash
# 列出工具
curl -s https://mcp.example.com/mcp \
  -H "x-api-key: $WINNOW_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}'

# 调用工具（以 list_groups 为例）
curl -s https://mcp.example.com/mcp \
  -H "x-api-key: $WINNOW_MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"list_groups","arguments":{}},"id":2}'
```

</details>

### ✅ 完成确认

> 你的客户端已经配好 winnow MCP 服务，`doctor` 或 probe 验证通过。
>
> **确认后展开 Step 4。**

</details>

<details>
<summary><strong>▸ Step 4：首次查询</strong></summary>

### 你需要做的事

调用第一个工具 `list_groups`，拿到你有权访问的群组列表。

在客户端中执行（或在 curl 里调 `tools/call`）：

```json
工具名: list_groups
参数: {}
```

### 返回示例

```json
{
  "groups": [
    {
      "group_id": "g_abc123",
      "display_name": "某技术交流群",
      "description": "..."
    }
  ]
}
```

**⚠️ 注意 `group_id` 格式**：它是内部 ID（`g_xxx`），不是群名。后续所有查询和反馈都需要它。

### 接下来你可以尝试

拿到 `group_id` 后，试试这几个查询：

```json
// 查某群的日报
工具名: get_daily_report
参数: { "group_id": "g_abc123", "date": "20260720" }

// 语义检索议题
工具名: search_topics
参数: { "group_id": "g_abc123", "query": "你的关键词" }

// 查看议题详情
工具名: get_topic
参数: { "topic_id": "sum_xxx" }

// 查看资源列表
工具名: list_resources
参数: { "group_id": "g_abc123", "date": "20260720" }
```

### ✅ 完成确认

> 你已经成功调用了 `list_groups`，拿到了 `group_id`，并至少成功执行了一次查询。
>
> **确认后展开 Step 5。**

</details>

<details>
<summary><strong>▸ Step 5：提交反馈</strong></summary>

### 理解反馈

反馈是**写给平台看的**，不是即时修改。你提交一条反馈后，平台会在下一次重生成时参考它。

**关键规则：**

| 规则 | 要求 |
|------|------|
| `signal` | 只能是以下 5 个值之一 |
| `target_type` | 标识你要反馈的目标类型 |
| `date` | 真实日历日期 |
| 必填 | `group_id` / `date` / `target_type` / `signal` / `content` |

**signal 含义：**

| 值 | 何时用 | `content` 填什么 |
|----|--------|-----------------|
| `correction` | 内容错了 | 正确/修正后的文本 |
| `supplement` | 内容漏了 | 补充的文本 |
| `approval` | 内容准确 | 说明认可什么 |
| `stale` | 内容已过时 | 说明为什么过时 |
| `quality` | 整体质量不好 | 评价说明 |

**target_type 可选值：**

- `topic` — 议题
- `report` — 日报整体
- `trend` — 趋势分析
- `highlights` — 亮点
- `resource` — 资源
- `section` — 版块
- 自定义表 id（如 `engineering` / `world_models`）

### 合法 payload 示例

```json
{
  "group_id": "g_abc123",
  "date": "20260720",
  "target_type": "topic",
  "signal": "correction",
  "content": "这里的结论应该是 Factor Zoo 可行。",
  "target_topic_id": "sum_abc123",
  "target_version_id": "report_xxx-v3",
  "original_text": "Factor Zoo 不可行。"
}
```

### 提交

```json
工具名: submit_feedback
参数: { 上面的 payload }
```

### ✅ 完成确认

> 你已成功提交至少一条反馈。
>
> 现在你已经掌握了 winnow MCP 的全部核心能力。下面的参考信息供日常使用查阅。

</details>

---

## 参考信息

以下内容不需要一步步读，遇到问题或需要查参数时来翻。

<details>
<summary><strong>6 个工具完整说明</strong></summary>

| 工具 | 作用 | 关键参数 |
|------|------|---------|
| `list_groups` | 列你有权访问的群 | 无参数 |
| `search_topics` | 关键词模糊检索议题 | `group_id`, `query`, `date_from?`, `date_to?` |
| `get_topic` | 议题详情 + 跨天演化 | `topic_id` |
| `get_daily_report` | 读某群某日日报 | `group_id`, `date`, `version?` |
| `list_resources` | 读某群某日资源列表 | `group_id`, `date` |
| `submit_feedback` | 提反馈 | 见 Step 5 的 payload 格式 |

> 完整参数签名 + 返回结构见 [`references/tools-reference.md`](references/tools-reference.md)。

</details>

<details>
<summary><strong>反馈格式精要（最易错）</strong></summary>

| 规则 | 要求 |
|------|------|
| **signal** | ∈ `{correction, supplement, approval, stale, quality}` |
| **target_type** | ∈ `{topic, report, trend, highlights, resource, section}` ∪ 自定义表 id |
| **date** | `YYYYMMDD` 或 `YYYY-MM-DD`，需是真实日期 |
| **必填非空** | `group_id` / `date` / `target_type` / `signal` / `content` |

> `group_id` 是内部 ID（`g_xxx`）——先调 `list_groups` 拿到，非群名。

权威字段规格 + 合法/非法示例见 [`references/feedback-format.md`](references/feedback-format.md)。

</details>

<details>
<summary><strong>常见问题</strong></summary>

| 现象 | 原因 | 解决 |
|------|------|------|
| `Invalid or unknown API key`（401） | key 没注册 / 拼错 / 已撤销 | 找提供方确认 |
| `无权访问群组`（403） | key 没授权该群 | 找提供方授权 |
| 反馈格式校验失败（400） | payload 不符 schema | 看错误里的合法值清单 |
| 反馈没生效 | 设计如此——择期消费 | 等待，或找提供方了解排期 |
| `search_topics` 一直空 | 平台数据未更新 | 找提供方触发更新 |
| 客户端工具不出现 | 客户端没认出远程服务 / header 没生效 | 先回到 Step 2 用 curl 验证 |

</details>

<details>
<summary><strong>获取本地脚本 / 完整技能包</strong></summary>

这些脚本**不是必需的**——所有 MCP 工具都可以直接调。想用脚本辅助：

```bash
git clone https://github.com/zenthos-z/z-winnow
# 取 .claude/skills/winnow-mcp/ 目录
```

或向平台提供方索取该目录（zip 即可）。放进 `~/.claude/skills/winnow-mcp/` 可在对话中
自动触发 winnow 知识库使用引导。

</details>
