# winnow 工具参考（6 工具完整参数）

所有工具走 HTTPS + `x-api-key` 鉴权。

- **读工具**（`list_groups` / `search_topics` / `get_topic` / `get_daily_report` / `list_resources`）→ 查询知识库
- **写工具**（`submit_feedback`）→ 提交反馈（由平台择期处理，不即时生效）

---

## list_groups

列出当前 key 有权访问的群。

| 参数 | 类型 | 说明 |
|------|------|------|
| （无） | | |

**返回**：`list[{group_id, display_name, chatroom_id}]`

**权限**：管理员返回全部活跃群；普通 key 只返回授权群。

> 这是所有其他工具的入口——先调它拿 `group_id`（内部 ID，如 `g_9bbb910567af`，**不是群名**）。

---

## search_topics

关键词模糊检索议题（场景 A：用户「记得聊过 X」）。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | str | —（必填） | 关键词，中英文均可 |
| `group_id` | str? | None | 限定群 |
| `date_from` | str? | None | 起始日期 **YYYYMMDD** |
| `date_to` | str? | None | 截止日期 **YYYYMMDD** |
| `limit` | int | 20 | 返回上限 |

**匹配方式**：关键词模糊匹配（中英文子串均可），检索 `topic_name` / `summary_text` / `conclusion` / `background` / `participants` 字段。

**返回**：`list[{summary_id, group_id, date, topic_name, summary_text, participants, lifecycle, conclusion}]`

**权限**：普通 key 限定授权群；显式传 `group_id` 越权 → 报错。

---

## get_topic

议题详情 + 同名议题跨天演化时间线 + 相关反馈（场景 B：话题确认 / 成熟度判断）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `summary_id` | str | 议题摘要 ID（来自 `search_topics`） |

**返回**：

```jsonc
{
  "detail": { /* 议题完整字段：topic_name, summary_text, participants,
                 lifecycle, conclusion, background, date, group_id, ... */ },
  "timeline": [
    // 同 group 同 topic_name 的其他日期记录（讨论持续情况）
    { "date": "20260719", "summary_id": "...", "lifecycle": "...", "participants": "..." }
  ],
  "feedback": [
    // 该议题已收到的反馈（按 target_topic_id 或 target_id 匹配）
    { "feedback_id": "...", "date": "...", "signal": "correction",
      "severity": "...", "corrected_text": "...", "correction_note": "...",
      "status": "...", "created_at": "..." }
  ]
}
```

未找到 → `{ "error": "topic not found", "summary_id": "..." }`。

**权限**：越权访问 → 报错。

---

## get_daily_report

读某群某日日报（场景 C：日报回看）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `group_id` | str | 群 ID（用 `list_groups` 查） |
| `date` | str | 日期 **YYYYMMDD** |
| `version` | int? | 版本号；省略取当前**生效**版本（不一定是最新——可能回滚过） |

**返回**：

```jsonc
{
  "group_id": "g_xxx",
  "date": "20260720",
  "version": 3,
  "content": { /* overview, topics, resources, trend, highlights, notice, ... 日报完整内容 */ }
}
```

未找到 → `{ "error": "report not found", "group_id": ..., "date": ..., "version": ... }`。

**权限**：越权 → 报错。

---

## list_resources

读某群某日资源列表。

| 参数 | 类型 | 说明 |
|------|------|------|
| `group_id` | str | 群 ID |
| `date` | str | 日期 **YYYYMMDD** |
| `version` | int? | 版本号；省略取最新版本 |

**返回**：

```jsonc
{
  "group_id": "g_xxx",
  "date": "20260720",
  "resources": [ /* 资源条目 */ ],
  "count": 5
}
```

未找到 → `{ "error": "resources not found", "group_id": ..., "date": ... }`。

**权限**：越权 → 报错。

---

## submit_feedback

提交反馈（不触发即时处理，由平台择期消费）。

> 📋 **服务端做 schema 校验**：`signal` / `target_type` / `date` / 必填字段不符合
> 格式的请求会被**直接拒绝（不写库，返回 `ToolError` / HTTP 400）**，错误消息一次性
> 列出全部违规项 + 合法取值。权威字段规格 + 合法/非法示例见
> [`feedback-format.md`](feedback-format.md)；提交前可用
> `scripts/validate_feedback.py` 本地预检。

| 参数 | 类型 | 说明 |
|------|------|------|
| `group_id` | str | 必填，须在 key 的授权群内 |
| `date` | str | 必填，日期锚点。**YYYYMMDD 或 YYYY-MM-DD** 均可 |
| `target_type` | str | 必填，反馈对象类型：`topic` / `report` / `resource` / `trend` / `section` / ... |
| `signal` | str | 必填，反馈意图（见下） |
| `content` | str | 必填，反馈正文（路由见下） |
| `target_id` | str? | 被反馈对象 ID |
| `target_version_id` | str? | **推荐**，被反馈的日报版本 ID（`{report_id}-v{n}`），精确定位 |
| `target_topic_id` | str? | **推荐**，议题级反馈时的议题 ID |
| `original_text` | str? | 被反馈的原内容（便于后续对照） |

**signal 取值**：

| signal | 含义 | content 存到哪 |
|--------|------|----------------|
| `correction` | 纠错 | `corrected_text`（+ `correction_mode=free_text`） |
| `supplement` | 补充 | `corrected_text`（+ `correction_mode=free_text`） |
| `approval` | 认可 | `correction_note` |
| `stale` | 过时标注 | `correction_note` |
| `quality` | 质量反馈 | `correction_note` |

**反馈人身份**：由 key 自动绑定（调用方**传不了、伪造不了**）。

**返回**：`{ "feedback_id": "<uuid>", "accepted": true }`（`accepted: false` = 入库失败，找提供方查日志）。

**权限**：越权 → 报错。

---

## 共同错误

| 错误 | 含义 | 处理 |
|------|------|------|
| `Invalid or unknown API key` | key 没注册 / 拼错 / 已撤销 | 找提供方确认 |
| `API key required (...)` | 调用没带 key | 检查 header 配置 |
| `无权访问群组 g_xxx` | 普通 key 越权 | 找提供方授权 |
| 数据未就绪 | 平台数据未更新 | 找提供方更新数据 |

> 所有越权 / 鉴权失败都会返回明确的错误信息。
