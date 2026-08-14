---
name: winnow-mcp
description: |
  winnow 知识库的使用引导 —— 查询群聊沉淀的议题、日报、资源，对内容提反馈，
  或把 winnow 接入自己的 agent / 脚本使用。
  当用户想：查某天的群日报、搜群里聊过的话题、看话题怎么演化、看群分享的资源、
  给日报内容纠错/补充/认可、在自己的工具里调用 winnow 知识库时，触发此技能。
  即使用户只说「怎么查 winnow 日报」「把 winnow 接到我的 agent」
  「群聊知识库怎么用」「群聊议题怎么搜」，也应触发。
agent_instructions: |
  这是一份**对话式逐步引导**，不是一次性参考文档。你必须用对话形式辅助用户完成部署，
  不要一次性展示所有步骤。按以下方式执行：
  1. 先介绍 winnow 是什么，问用户准备好了没有
  2. 加载 INSTALL.md，但**每次只展示一步**，完成后再展示下一步
  3. 完成确认后自然过渡到下一步
  4. 失败时帮助排查，不要跳过
  5. 全部完成后总结并询问用户下一步
  6. 不要引用折叠块、<details> 等 Markdown 语法——用户看不到这些
  7. 此后 6 工具 Cookbook 和参考信息是给你自己查阅的，用户问到再引用，不要一次性展示
---

# winnow 知识库使用指南

> 🚀 **接入引导（推荐 Agent 用对话方式执行）**：[`INSTALL.md`](INSTALL.md) —— 5 步逐步引导，
> 每一步有明确完成确认。本文件是完整的概念与场景编排手册。

## 这是什么

winnow 把群聊沉淀成**议题、日报、资源**等知识。本技能帮你从外部 agent / 脚本查询这些知识、对内容提反馈。

公网服务地址：`https://mcp.example.com/mcp`（HTTPS，需 API key）。

```
你的 agent / 脚本  ──HTTPS + API key──▶  winnow 服务
                                           ├─ 查：议题 / 日报 / 资源
                                           └─ 提：纠错 / 补充 / 认可（择期处理）
```

**6 个工具**：

| 工具 | 干什么 | 典型场景 |
|------|--------|---------|
| `list_groups` | 列出你有权访问的群 | 入口：拿到 `group_id` |
| `search_topics` | 关键词模糊检索议题 | 「记得群里聊过 X」 |
| `get_topic` | 议题详情 + 跨天演化时间线 + 相关反馈 | 判断话题成熟度 |
| `get_daily_report` | 读某群某日日报（支持指定版本） | 日报回看 |
| `list_resources` | 读某群某日资源列表 | 看推荐资源 |
| `submit_feedback` | 提交反馈（不即时处理） | 纠错 / 补充 / 认可 |

> `search_topics` 支持中英文关键词模糊匹配（如「因子」「回测」「世界模型」都能命中）。

## 第 0 步：拿到 API key

服务用 **API key 鉴权**：一个 key 绑一个身份 + 可访问的群授权，管理员 key 全权。key 格式 `wn_<random>`。

**key 不是自己生成的**——向平台提供方申请，由提供方发给你（格式 `wn_xxx`），并告知你能访问哪些群。

**权限模型**：

- **普通 key**：只能访问授权的群；提反馈时你的身份由 key 自动绑定（**调用方传不了，也伪造不了**）
- **管理员 key**：全权，所有群都能访问

**⚠️ key 安全**：key 是凭证，**不要写进 git / 配置文件明文 / 聊天截图**。本技能所有示例统一从环境变量 `WINNOW_MCP_KEY` 读：

```bash
export WINNOW_MCP_KEY=wn_xxxxxxxxxxxxxxxxxxxxxxxx
```

## 第 1 步：连上服务（选一种）

| 方式 | 适合 | 速查 |
|------|------|------|
| **Agent 应用配置** | Codex / Hermes / OpenClaw / WorkBuddy | 见 `references/client-configs.md`（每种都有可复制配置） |
| **Python 脚本** | 自己写程序调 | `python SKILL_DIR/scripts/mcp_client_check.py` |
| **curl 烟雾** | 最快验证服务可达 + key 有效 | `bash SKILL_DIR/scripts/mcp_smoke.sh` |

鉴权 header：优先 `x-api-key: wn_xxx`（也兼容 `Authorization: Bearer wn_xxx`）。

> **Claude Desktop / Cursor 等只支持本地 stdio MCP 的客户端**连不了远程服务？用 `uvx fastmcp-remote` 桥——见 `references/client-configs.md` §通用桥。

## 第 2 步：连接自检

```bash
export WINNOW_MCP_KEY=wn_xxx
python SKILL_DIR/scripts/mcp_client_check.py
```

输出 `✅ 连接 / ✅ 鉴权 / ✅ N 个工具 / ✅ M 个可见群` 即通过。失败会给出具体原因（key 无效 / 无权 / 网络不通）。

## 6 工具 Cookbook（典型任务编排）

> 所有工具调用前先 `list_groups` 拿到 `group_id`——它是**内部 ID**（如 `g_9bbb910567af`），**不是群名**。

### 场景 A：「记得群里聊过 X」——模糊找议题

```
list_groups                          → 拿到 group_id
search_topics(query="世界模型",
              group_id="g_xxx",
              date_from="20260601",
              date_to="20260720")    → 命中的议题列表，每项有 summary_id
get_topic(summary_id="...")          → 看详情 + 跨天演化 + 已有反馈
```

### 场景 B：判断某话题成熟度

`get_topic` 返回三块：

- `detail`：参与人（participants）、是否形成结论（conclusion）
- `timeline`：同群同名议题在不同日期的记录（讨论持续多久、是否仍在进行）
- `feedback`：该议题已收到的反馈（纠错 / 补充 / 认可）

### 场景 C：回看某天日报

```
get_daily_report(group_id="g_xxx", date="20260720")            # 默认取当前生效版本
get_daily_report(group_id="g_xxx", date="20260720", version=3)  # 指定版本
```

### 场景 D：看资源

```
list_resources(group_id="g_xxx", date="20260720")   → 当日资源列表
```

### 场景 E：提反馈（纠错 / 补充 / 认可）

```
submit_feedback(
  group_id="g_xxx",
  date="20260720",
  target_type="topic",               # topic/report/resource/trend/section/...
  signal="correction",               # correction纠错 / supplement补充 / approval认可 / stale过时 / quality质量
  content="这里的结论应该是……",       # correction/supplement 时作为「正确/补充文本」；其他 signal 作为说明
  target_topic_id="summary_xxx",     # 议题级反馈（推荐）
  target_version_id="report_xxx-v3", # 日报版本级反馈（推荐，精确定位）
  original_text="原内容..."          # 可选，被反馈的原内容
)
→ {"feedback_id": "...", "accepted": true}
```

**signal 路由**：`correction` / `supplement` → `content` 存为「正确/补充文本」；`approval` / `stale` / `quality` → `content` 存为说明。

> 📋 **格式校验**：服务端会做 schema 校验——`signal` 必须是上面 5 个之一、`target_type`
> 必须是合法值（基础 6 值 `topic/report/trend/highlights/resource/section` + 平台注册的
> 自定义表 id）、`date` 必须是真实日历日期、必填字段不能空。**不符合的请求会被直接拒绝
> （不写库）**。权威规格见 [`references/feedback-format.md`](references/feedback-format.md)。
> 提交前可用 `scripts/validate_feedback.py` 本地预检（零依赖，见脚本目录）。

> 反馈提交后**不会即时生效**——由平台择期处理（不会立即改变你已经读到的内容）。这是设计如此。

## 参数与语义坑（必读）

| 坑 | 正确做法 |
|----|---------|
| `group_id` 是什么 | **内部 ID**（`g_xxx`），不是群名——用 `list_groups` 查 |
| `date` 格式 | `get_daily_report` / `list_resources` / `search_topics` 的 `date_from`/`date_to` 用 **YYYYMMDD**；`submit_feedback` 的 `date` 兼容 YYYYMMDD 或 YYYY-MM-DD |
| `version` | 省略 = 取当前**生效**版本，**不一定是最新版本**（可能有回滚） |
| 反馈人身份 | `submit_feedback` 的反馈人由 key 自动绑定，调用方传不了也伪造不了 |
| 权限 | 普通 key 越权访问非授权群 → 报错；管理员 key 全权 |
| search 召回空 | 关键词需匹配议题正文；若一直空，找提供方更新数据 |
| 反馈没生效 | `submit_feedback` 不即时处理——由平台择期生效（设计如此） |
| 向量/语义检索 | 无。模糊检索用 `search_topics`，精确议题用 `get_topic` |

## 客户端配置详情

5 种 Agent / 工具的**可直接复制配置**（含安全传 key 的最佳实践）——见 `references/client-configs.md`：

- OpenClaw / Hermes Agent / 腾讯 WorkBuddy / Codex CLI / Python / curl / fastmcp-remote 通用桥

6 工具的**完整参数签名 + 返回值结构**——见 `references/tools-reference.md`。

## 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| `Invalid or unknown API key`（401） | key 没注册 / 拼错 / 已撤销 | 找提供方确认 key |
| `无权访问群组`（403） | key 没被授权该群 | 找提供方给该群授权 |
| 连接超时 / 502 | 服务暂时不可用 | 找提供方排查 |
| `search_topics` 一直空 | 平台数据未更新 | 找提供方更新数据 |
| 工具列表里只有部分群 | 正常——普通 key 只能看到授权的群 | 用管理员 key 或让提供方扩权 |
| 配了客户端但工具不出现 | 客户端没认出远程服务 / header 没生效 | 先用 `mcp_smoke.sh` 验证服务+key；再核对 `references/client-configs.md` 对应客户端的字段名 |

## 脚本目录

- `scripts/mcp_smoke.sh` — curl 最小烟雾（零依赖）
- `scripts/mcp_client_check.py` — 连接 + 鉴权 + 权限自检（读 `WINNOW_MCP_KEY`）
- `scripts/mcp_demo_workflow.py` — 完整任务编排 demo（找议题 → 看演化 → 看日报 → 提反馈）
- `scripts/validate_feedback.py` — **提交反馈前本地预检** payload 格式（零依赖，裸 `python3` 即可跑；通过退出 0，不通过退出 1 并打印违规清单 + 合法值 + 修正示例）

使用时把 `SKILL_DIR` 替换为技能目录实际路径。
