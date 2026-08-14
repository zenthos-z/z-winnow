# submit_feedback 反馈格式规格（权威）

`submit_feedback` 是 winnow MCP **唯一**向知识库写反馈的入口。服务端对入参做
**schema 校验——不符合格式的请求会被拒绝（`ToolError` / HTTP 400），不会写入**。
本文是 payload 的权威规格；提交前建议用 `scripts/validate_feedback.py` 本地预检。

> 速查：6 工具签名与返回结构见 `tools-reference.md`；本文件只讲 `submit_feedback`
> 的字段合法性。场景编排见 `SKILL.md` 场景 E。

---

## 字段表

| 字段 | 必填 | 类型 | 合法取值 / 说明 |
|------|:----:|------|----------------|
| `group_id` | ✅ | str | 群 ID（内部 ID `g_xxx`，用 `list_groups` 查）；须在 key 授权群内，非空 |
| `date` | ✅ | str | 日期锚点。**`YYYYMMDD`（如 `20260720`）或 `YYYY-MM-DD`（如 `2026-07-20`）**，且必须是真实日历日期（`2026-13-40` 会被拒） |
| `target_type` | ✅ | str | 反馈对象类型，见下表 |
| `signal` | ✅ | str | 反馈意图，见下表 |
| `content` | ✅ | str | 反馈正文，非空。落库位置由 `signal` 决定（见下） |
| `target_id` | ⬜ | str | 被反馈对象 ID |
| `target_version_id` | ⬜ | str | **推荐**。被反馈的日报版本 ID（`{report_id}-v{n}`），精确定位溯源 |
| `target_topic_id` | ⬜ | str | **推荐**。议题级反馈时的议题 ID（来自 `search_topics` 的 `summary_id`） |
| `original_text` | ⬜ | str | 被反馈的原内容（便于后续 regenerate 对照） |

---

## `signal` 合法取值（5 个，区分大小写）

| signal | 含义 | `content` 落到哪里 |
|--------|------|--------------------|
| `correction` | 纠错（你给的是正确文本） | `corrected_text`（+ `correction_mode=free_text`） |
| `supplement` | 补充（你给的是补充文本） | `corrected_text`（+ `correction_mode=free_text`） |
| `approval` | 认可（点赞/确认对） | `correction_note`（说明性文字） |
| `stale` | 过时标注（内容已过时） | `correction_note` |
| `quality` | 质量反馈（泛泛评价） | `correction_note` |

> 关键：`correction` / `supplement` 时，`content` 是「**正确的/补充后的文本**」
> （会被纳入重生成）；其余 signal 时，`content` 是「**说明**」。填错语义会导致
> 纠错文本被当成说明丢失。

## `target_type` 合法取值

**基础集（6 个）**：

| target_type | 指向 |
|-------------|------|
| `topic` | 议题（配 `target_topic_id`） |
| `report` | 日报整体（overview） |
| `trend` | 趋势分析 |
| `highlights` | 亮点（配 `target_id` 指定某条） |
| `resource` | 资源（`target_id` 为资源标题） |
| `section` | 报告某段落 |

**自定义表扩展**：平台注册的自定义表 id（如 `engineering` / `world_models`）也是
合法 `target_type`，由 `custom_tables` registry 动态注册。**服务端实际合法全集 =
基础集 ∪ 平台已注册表 id**——以服务端校验返回的清单为准。`validate_feedback.py`
面向外部用户只内置基础集；若你确实在用某自定义表，预检报「不在基础集」是正常的，
以服务端响应为准。

---

## 完整合法示例

```json
{
  "group_id": "g_9bbb910567af",
  "date": "20260720",
  "target_type": "topic",
  "signal": "correction",
  "content": "这里的结论应该是 Factor Zoo 可行，而非不可行。",
  "target_topic_id": "sum_abc123",
  "target_version_id": "report_xxx-v3",
  "original_text": "Factor Zoo 不可行。"
}
```

返回（通过校验 + 入库）：

```json
{ "feedback_id": "<uuid>", "accepted": true }
```

> 反馈提交后**不即时生效**——由平台择期消费（不会立即改变你已读到的内容）。这是设计如此。

---

## 非法示例（服务端会拒绝，不写库）

| # | 错误 payload 片段 | 被拒原因 |
|---|-------------------|----------|
| 1 | `"signal": "fix"` | signal 不在 5 值集 |
| 2 | `"target_type": "asdfg"` | target_type 不在合法全集 |
| 3 | `"date": "2026-13-40"` | 形态像日期但不是真实日历日期 |
| 4 | `"date": "notadate"` | date 格式不符 |
| 5 | `"content": ""` | 必填字段为空 |
| 6 | `{}`（缺全部必填字段） | 缺 `group_id` / `date` / `target_type` / `signal` / `content` |

被拒时返回的 `ToolError` 消息会**一次性列出全部违规项**（不只第一个）+ 合法取值
清单，便于一次改对。例如同时填错 `signal` 和 `target_type`：

```
反馈格式校验失败，已拒绝写入（未落库）。违规项：
  • signal：Input should be 'correction', 'supplement', 'approval', 'stale' or 'quality'
  • target_type：target_type 必须是下列之一（不区分先后）：['engineering', 'highlights', ...]
参考 —— 合法 signal：['approval', 'correction', 'quality', 'stale', 'supplement']；
      合法 target_type：['engineering', 'highlights', 'report', 'section', ...]
```

---

## 提交前本地预检

```bash
# 内联
python3 scripts/validate_feedback.py --inline '{...上面合法示例...}'
# 文件
python3 scripts/validate_feedback.py --file payload.json
# stdin
cat payload.json | python3 scripts/validate_feedback.py
```

通过退出码 `0` 并打印归一化 payload；不通过退出码 `1` 并打印违规清单 + 合法值 +
修正示例。详见 `SKILL.md` 脚本目录。
