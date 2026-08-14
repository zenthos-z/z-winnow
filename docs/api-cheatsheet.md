# API 接口速查表

winnow 群日报系统的全部 HTTP 接口，按**业务板块**（不是技术模块）分组。
每个接口一句话说明它干什么；完整说明（何时用、返回、出错码）见在线文档。

> **在线文档**：启动 `winnow web` 后，浏览器打开 **`/docs`**（Swagger，可试调）或 **`/redoc`**（只读，排版更清爽）。前端控制台在 `/ui/`。
>
> **异步任务约定**：标 `202` 的接口都是「提交后在后台跑，立即返回 `task_id`」，用对应的 `GET .../{task_id}` 查进度。这类接口：生成日报、重建/清理记忆、AI 评分、导出 RL 数据。
>
> 共 **56 个接口**，分 11 个板块。

---

## ① 系统总览 — 看系统是否健康、整体统计、配置

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查：服务活着没、数据库连上没 |
| GET | `/api/v1/overview` | 首页大盘：群组数/报告数/消息数等汇总 |
| GET | `/api/v1/system/info` | 系统运行信息（版本 + 数据库状态） |
| GET | `/api/v1/system/config` | 当前配置（已脱敏，不含密钥） |

## ② 群组配置 — 生成日报前，先在这里登记要分析的群

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/groups` | 列出所有已注册群组（分页/搜索） |
| GET | `/api/v1/groups/sessions` | 从 CipherTalk 拉真实群聊列表（新建群时选） |
| GET | `/api/v1/groups/{group_id}` | 查看单个群详情 |
| POST | `/api/v1/groups` | 注册一个新群 |
| PUT | `/api/v1/groups/{group_id}` | 修改群信息（改名/启停） |
| DELETE | `/api/v1/groups/{group_id}` | 删除群（级联清理本地数据 + 磁盘 L3 + MemOS 记忆） |

## ③ 核心议题 — 管理每个群要长期追踪的话题

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/core-topics` | 列出某群的核心议题 |
| POST | `/api/v1/core-topics` | 新增一个核心议题 |
| PUT | `/api/v1/core-topics/{topic_id}` | 修改核心议题 |
| DELETE | `/api/v1/core-topics/{topic_id}` | 删除核心议题 |

## ④ 关键人物 — 标记群里的重要成员

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/key-people` | 列出某群关键人物（带发言统计） |
| POST | `/api/v1/key-people` | 手动标记某人为关键人物 |
| PUT | `/api/v1/key-people/{sender}` | 修改关键人物信息（按群隔离） |
| DELETE | `/api/v1/key-people/{sender}` | 取消关键人物标记（软删除） |

## ⑤ 数据抓取 — 发起和查看日报生成任务（跑流水线）

| 方法 | 路径 | 干什么 |
|---|---|---|
| POST | `/api/v1/runs` | **发起一次日报生成**（抓消息→解析→AI生成→落库）`202` |
| GET | `/api/v1/runs` | 列出历次生成任务 |
| GET | `/api/v1/runs/stream` | 实时推送运行状态（SSE 流） |
| GET | `/api/v1/runs/{run_id}` | 查看某次任务的状态 |
| POST | `/api/v1/runs/batch` | 批量提交多个生成任务 `202` |
| POST | `/api/v1/runs/{run_id}/cancel` | 取消排队中的任务 |

## ⑥ 原始数据 — 浏览抓下来的三层中间数据 + 消息溯源

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/data/{layer}/{group_id}/{date}` | 按层浏览：L1原文 / L2上下文 / L3总结 |
| GET | `/api/v1/data/stats` | 三层数据的汇总统计 |
| GET | `/api/v1/data/provenance/{server_id}` | 按 serverId 溯源：消息→进了哪些议题 |
| GET | `/api/v1/data/l1/{group_id}/{date}/detail/{server_id}` | 某条原始消息的完整详情 |

## ⑦ 报告产出 — 查看 / 导出 / 重跑 / 推送飞书

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/reports` | 列出所有报告（分页/筛选） |
| GET | `/api/v1/reports/{report_id}` | 查看单个报告概要 |
| GET | `/api/v1/reports/{report_id}/content` | 读取报告正文（JSON，前端渲染用） |
| GET | `/api/v1/reports/{report_id}/export` | 导出报告为 Markdown |
| GET | `/api/v1/reports/{report_id}/versions` | 列出报告的所有历史版本 |
| GET | `/api/v1/reports/{report_id}/diff` | 对比最近两个版本的差异 |
| POST | `/api/v1/reports/{rid}/regenerate` | 重新生成报告 `202` |
| POST | `/api/v1/reports/{report_id}/feishu` | 推送报告到飞书 `202` |
| GET | `/api/v1/reports/{rid}/tasks/{task_id}` | 查询报告后台任务进度 |

## ⑧ 长期记忆（MemOS）— 记忆的运维：健康/搜索/重建/清理

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/memos/status` | 检查记忆服务 MemOS 是否正常 |
| POST | `/api/v1/memos/search` | 在记忆里做语义搜索 |
| GET | `/api/v1/memos/cubes` | 列出某群的所有记忆库（cube） |
| GET | `/api/v1/memos/cubes/{cube_id}` | 查看某记忆库详情 |
| DELETE | `/api/v1/memos/cubes/{cube_id}` | 删除整个记忆库（需 `{confirm:true}` 二次确认） |
| POST | `/api/v1/memos/cubes/{cube_id}/rebuild` | 从 SQLite 重建记忆库 `202` |
| POST | `/api/v1/memos/cubes/{cube_id}/vacuum` | 记忆库生命周期清理（归档/删除旧记忆） `202` |
| GET | `/api/v1/memos/memory/{memory_id}` | 查看单条记忆详情 |
| DELETE | `/api/v1/memos/memory/{memory_id}` | 删除单条记忆 |
| DELETE | `/api/v1/memos` | 全量清空所有群 MemOS 记忆（开发调试，需 `{confirm:"WIPE_ALL_MEMORIES"}`） |
| POST | `/api/v1/memos/flush` | 冲刷积压的同步任务 `202` |

## ⑨ 质量评估 — 用 AI 给报告打分

| 方法 | 路径 | 干什么 |
|---|---|---|
| POST | `/api/v1/judge` | 用 AI 从 4 个维度给报告打分 `202` |
| GET | `/api/v1/judge/{task_id}` | 查询评分任务进度和结果 |

## ⑩ 用户反馈 — 记录/处理用户对报告的意见

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/v1/feedback` | 列出某群某天未处理的反馈 |
| POST | `/api/v1/feedback` | 提交一条反馈（点赞/点踩/纠错） |
| GET | `/api/v1/feedback/{feedback_id}` | 查看单条反馈 |
| POST | `/api/v1/feedback/{feedback_id}/consume` | 标记反馈为「已处理」 |
| POST | `/api/v1/feedback/{feedback_id}/rollback` | 撤销「已处理」回「未处理」 |

## ⑪ 训练数据 — 导出强化学习数据集

| 方法 | 路径 | 干什么 |
|---|---|---|
| POST | `/api/v1/rl/export` | 导出指定日期范围的 RL 训练数据集 `202` |
| GET | `/api/v1/rl/export/{task_id}` | 查询 RL 导出任务进度 |

---

## 维护说明

- 本表的「一句话功能」与 `/docs` 里每个接口的标题（summary）一一对应，后者由各路由函数 docstring 的第一行自动生成（`web/app.py` 里有注入逻辑）。
- 改接口说明时，改对应路由函数的 docstring 即可，`/docs` 和本表同步生效；新增/删除接口记得同步更新本表。
