# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目

winnow（仓库名 z-winnow）— **信息降噪压缩平台**。群聊 → [LangGraph 降噪压缩管道] → **L3 核心知识层**（议题/资源/工程/日报并列产物）→ [MCP 接口] → 用户/Agent。日报是 L3 的一个产出视角（非"最终产出"）；**MCP 接口**（key-based 权限：key→成员+群组白名单）是对外知识消费入口；服务器常驻部署实现 7×24 可查可收反馈。

## 常用命令

```bash
# 安装依赖 + pre-commit hooks
poetry install
pre-commit install

# 运行全部测试（mock 模式 — 无需 API key）
poetry run pytest tests/ -v --tb=short

# 运行单个测试文件 / 按名称
poetry run pytest tests/test_graph_builder.py -v
poetry run pytest tests/test_graph_builder.py::test_build_graph_returns_compiled -v

# 跳过 slow/integration/e2e
poetry run pytest tests/ -v -m "not slow and not integration and not e2e"

# 覆盖率 / lint / 格式 / 类型 / 安全
poetry run pytest tests/ --cov=src/z_winnow --cov-report=term-missing
poetry run ruff check .
poetry run ruff format --check .
poetry run ruff check --fix . && poetry run ruff format .   # 自动修复
poetry run mypy src/
poetry run bandit -r src/ -ll --configfile pyproject.toml
pre-commit run --all-files

# CLI（入口均为 winnow）
poetry run winnow ingest --date 20260428 --group "群名"
poetry run winnow trace --server-id 1234567890
poetry run winnow web                                       # Web 控制面板，端口 :8100
poetry run winnow judge --group "群名" --from 2026-04-20 --to 2026-04-28   # LLM-as-judge 质量评估

# 群组管理
poetry run winnow group list
poetry run winnow group resolve --name "群名"                # 群名 → group_id + chatroom_id
poetry run winnow group resolve --room-id xxx@chatroom       # chatroom_id → group_id
poetry run winnow group add --chatroom-id xxx@chatroom --display-name "名称"  # 注册新群组（幂等）
# 首次运行须注册群组: poetry run winnow group add --chatroom-id xxx@chatroom

# MCP
poetry run winnow mcp                                            # stdio（本地 Claude Desktop/Cursor）
poetry run winnow mcp --transport http --port 8000 --host 0.0.0.0 # http（远程部署）
poetry run winnow mcp-key list                                   # key 管理（add/allow/revoke）

# 数据同步（本地 ↔ 部署服务器；.env 配 WINNOW_ECS_SSH_HOST/KEY）
poetry run winnow sync status
poetry run winnow sync push          # 推 L3 快照 + processed JSON + mcp_keys.yaml
poetry run winnow sync pull          # 拉反馈 inbox → merge 本地 → 清 inbox

# MemOS 运维
poetry run winnow memos status
poetry run winnow memos rebuild --group X --from sqlite
poetry run winnow memos search --group X --query "..."
poetry run winnow memos flush

# 定时日报调度（独立于 web，按各群 daily_schedule_cron 触发；目标=前一天，宕机跨多天补跑）
poetry run winnow scheduler               # 交互菜单（Rich 看板 + 设定向导）
poetry run winnow scheduler run           # 常驻守护（预检+补跑+每分钟 tick）
poetry run winnow scheduler run --once    # 单次评估（系统 cron 每分钟调）
poetry run winnow scheduler doctor        # 环境体检

# 日报配图
poetry run winnow gen-image --record-id <report_id>

# 数据清理（测试隔离）
poetry run python scripts/clear_all.py --all-dates --groups "群A" "群B"

# MemOS Docker 服务（需先启动才能跑完整 pipeline）
cd deployments && docker compose --env-file ../.env up redis qdrant neo4j memos-api -d
# 启动必须 --env-file ../.env，否则容器内 API key 为空
# MOS_CHAT_MODEL 不带 provider 前缀: "deepseek-v4-flash" ✓, "openai/deepseek-v4-flash" ✗
```

---

## 架构

### 平台全景

```
群聊 → [生产流水线] → L3 核心知识层 ──sync push──▶ 服务器(ro 快照 + rw inbox)
                        ↑                                │ MCP 接口
                        │                                ▼
                 MemOS 语义记忆                   外部 Agent / Claude
                        ↑                                │ submit_feedback
                  └── 反馈回路(pull inbox → 版本化重生成) ◄─┘
```

- **L3 = 核心知识层**：议题/资源/工程/日报**并列产物** + 版本管理（非"日报的最终产出"）
- **MCP 接口**是对外知识消费入口（key→成员+群组白名单）；MemOS 留本地不暴露
- **反馈回路**：服务器收反馈 → 本地 pull → 版本化重生成（守 L3 不可变红线）

### 图流水线（LangGraph StateGraph）

单 agent 直线流水线：

```
START → data_fetch → content_enrich → orchestrator → unified_reporter
    → output_composer → END

飞书推送不在主图，由 Web UI 通过 lark-cli 独立调用。
```

- `unified_reporter`：单次 LLM 调用完成日报 + 资源 + 工程问题 + 议题追踪 + 生命周期分类
- `output_composer`：L3 JSON 持久化 + `topic_summaries` / `report_versions` 写库 + MemOS 同步入队
- 每阶段各自写入存储层：data_fetch→L1, content_enrich→L2, output_composer→L3
- Markdown 渲染延迟到 Phase H（`export_markdown()` 手动触发），不在主图中

**入口**：`src/z_winnow/graph/builder.py` — `build_graph()` 返回编译后的 `CompiledStateGraph`。5 个主线节点，主图 `output_composer → END` 直连终止。

### 状态 Schema

- **`OverallState`**（`src/z_winnow/state.py`）— 主图状态 TypedDict，按生命周期分阶段。`errors` 字段使用 `Annotated[list, operator.add]` reducer。
- ⚠️ LangGraph 会**静默丢弃**节点返回的未在 OverallState 声明的 key——下游会收到 None，新增字段必须先改 TypedDict。

### 源码结构（`src/z_winnow/`）

| 模块 | 用途 |
|--------|---------|
| `graph/` | LangGraph StateGraph 构建器（`builder.py` 主图） |
| `subagents/` | `unified_reporter/`（日报/资源/议题 + 自定义表统一 LLM agent）、`output_composer/`（L3 JSON 组装 + Jinja2 渲染）、`contracts`、`incremental_prompt` |
| `pipeline/` | CipherTalk / WeFlow 客户端、SQLite 3 层存储、上下文组装、溯源、SQL 迁移 |
| `mcp_server/` | MCP 网关（FastMCP v3, 6 工具 + key-based 鉴权 + contextvars 注入 + 群组白名单） |
| `sync/` | 本地↔服务器数据同步（push/pull/status）+ transport（ssh/rsync） |
| `scheduler/` | 定时日报调度（独立 CLI：engine + preflight + Rich 看板）；幂等真源=report_versions，⚠️ 勿用 `pipeline_runs.group_id`（存 display_name） |
| `web/` | FastAPI 纯 API 后端（`/api/v1` 前缀，16 路由模块）+ `static/` 静态 HTML 前端（vanilla JS + Tailwind，挂载于 `/ui/`） |
| `content_enrich/` | XML 解析、Vision 图片分析、链接预览、卡片解析、媒体落盘 |
| `custom_tables/` | 自定义表框架（`TableDef` registry + Skill 提取 prompt；engineering/world_models 内置，YAML 加表即扩展） |
| `outputs/` | 日报配图生成、Markdown 报告写出 |
| `object_storage/` | Cloudflare R2 客户端（附件上传 + 预签名 URL） |
| `config/` | pydantic-settings（`Settings` 类）、模型工厂、日志配置 |
| `templates/` | Jinja2 模板（日报/工程/资源/议题/飞书日报）+ renderer |
| `memory/` | MemOS 适配器（real/mock/disabled）、同步队列 worker、feedback 同步 |
| `orchestrator/` | 任务编排入口 `orchestrate()`（Web API `runs`/`batch` 调用，非主图节点） |
| `observability/` | LangSmith 追踪、structlog 指标 |

### MCP Server（FastMCP v3，6 工具 + key-based 鉴权）

`mcp_server/server.py`：
- **6 个 MCP 工具**: `list_groups` / `search_topics` / `get_topic` / `get_daily_report` / `list_resources` / `submit_feedback`
- **key-based 鉴权**: middleware（contextvars）校验 key → `resolve_member` 查 YAML → set `MemberInfo`；6 工具按群组白名单过滤（admin 全权）；http 无 key 拒绝；stdio 本地 admin 兜底
- **双库路由**: `get_l3_db()`（ro snapshot, mtime 懒重连）+ `get_inbox_db()`（rw inbox）；按 `deployment_target` 切换
- **key 注册表**: `mcp_keys.py`（mtime 热重载），`config/mcp_keys.yaml`（gitignored）

消费者接入文档：`docs/mcp.md` + `.claude/skills/winnow-mcp/`。

### Web API（约 79 端点）

`app.py` 创建 FastAPI 应用，16 个路由模块聚合到 `/api/v1` 前缀：health / overview / groups / core_topics / judge / system / data / data_preview / reports / memos / key_people / runs / batch / rl(遗留) / feedback / auth。前端架构见 `docs/web-frontend-architecture.md`，端点速查见 `docs/api-cheatsheet.md`。

⚠️ Web 服务**不热重载**：改后端代码须 kill 重启，否则新路由不生效。

### 数据存储

**3 层 SQLite —— L1/L2 当日快照，L3 核心知识层**：

| 层 | 表 | 写入节点 | 内容 |
|-------|-----|---------|------|
| L1 | `raw_messages` | data_fetch | 数据源原始消息（serverID 溯源），不可变 |
| L2 | `parsed_contexts` | content_enrich | Token 边界内的上下文块（含 Vision 图片描述/链接预取富化），不可变 |
| L3 | `topic_summaries` + `data/processed/{group_id}/{date}/v{n}/*.json` | output_composer | 议题摘要、日报/资源/工程 JSON。按版本写 `v{version}/` 目录；重跑产新版本（INSERT OR REPLACE + 确定性 summary_id） |

**MemOS 长期语义记忆** 按内容类型细拆 cube：`winnow:{gid}:topics` / `:resources` / `:daily` 固定 + `winnow:{gid}:{table_id}` 自定义表（registry 驱动）。cube scope 真源：`memos_service.cube_scopes_for_group(config)`。

### 反馈与版本管理

- **闭环**：提交反馈（仅入 SQLite）→「根据反馈重生成」→ regenerate 产新版本 → 回填 `produced_version_id` + `mark_consumed` + 派生 `group_experiences` + 记忆纠正。
- **溯源**：`GET /feedback/{id}/provenance` → 四元组。
- **回滚**：`POST /reports/{rid}/versions/{vid}/rollback` → `report_versions.is_active` 重指。
- **regenerate**：去重 + 超时护栏（ainvoke 480s）+ regen 时从 L2 还原富化内容（图片描述不丢）。
- **经验消费**：`correction_loader` 主源 `group_experiences`（active），注入 unified_reporter `<prior_corrections>`。
- ⚠️ `feedback_events.date` 存 YYYY-MM-DD，`report_versions.date` 存 YYYYMMDD——读取处已容忍两种格式。

### 数据目录地图（测试前清空数据必读）

**文件**：`data/winnow.db`（+ WAL）。`groups` / `core_topics` / `group_members` / `async_tasks` / `batch_jobs` **不清**（永久保留/历史记录）；L1/L2/L3 表按 group_id + date 删；`memos_sync_queue` 按 cube_id 删；`feedback_events` / `group_experiences` 按 group_id 删。

> **关键坑**：`pipeline_runs.group_id` 存 **display_name**，而 `raw_messages`/`parsed_contexts`/`topic_summaries`/`report_versions` 的 `group_id` 存内部 UUID。清空时两者匹配方式不同。

#### MemOS 已知坑（实测）

- **Qdrant collection 不自动重建**：collection 不存在时向量插入 404，节点落 `vector_sync: failed` 且 search 召回不到。手动建 `neo4j_vec_db`（dim=3072 Cosine）后跑 `memos rebuild` 补历史向量。
- **`delete_memory()` 只删树节点不清 Qdrant 向量**：彻底清空必须删 collection（`clear_all.py` 已处理）。
- **MemReader 行为**：`/product/add` 不接受外部 metadata（tags 由 fine LLM 自动提取）；`memory_content` 灌法走 `add_structured_memory`；`/product/search` 支持 `mode`（fast/fine/mixture），fine 偶发空结果传 mixture。
- memos-api 容器启动需预缓存 gpt2 tokenizer（`deployments/` 已带缓存卷）。

### 配置

- `.env` — 全部配置项见 `.env.example`（每项有注释）；pydantic-settings 加载
- `Settings` 类（`config/settings.py`）— `WINNOW_*` 环境变量覆盖标准名，线程安全单例 `get_settings()`；默认 provider=deepseek（`deepseek-v4-flash`）
- `data/config_overrides.json` — onboarding 向导写入，**最高优先级**；测试 autouse fixture `_neutralize_config_overrides` 中和它
- Mock 模式：`WINNOW_REAL_LLM=false` 禁用 LLM 调用（测试自动启用）
- 配置优先级：`Field 默认值 < .env < 标准环境变量 < WINNOW_* < data/config_overrides.json < CLI 参数`（⚠️ `config/defaults.yaml` 是参考文档，不被代码加载）

### 测试设施

- `tests/conftest.py`：pytest 标记（slow/integration/e2e）+ autouse `_neutralize_config_overrides`（中和 onboarding overrides，让测试由环境变量驱动隔离）
- 每个测试文件自行构造数据（内联 dict 或 `_make_*`/`_seed_*` 辅助），无集中式数据工厂
- pytest 配置：`asyncio_mode = "auto"`、`pythonpath = ["src"]`、`--strict-markers`
- 全部测试在 mock 模式运行 — 无需 API key

### 代码质量

**Ruff**（lint + format，行宽 100）/ **MyPy**（基础，非阻断）/ **Bandit**（-ll）/ **Pre-commit**（YAML/TOML + ruff + mypy + bandit + 大文件/私钥检测）。配置均在 `pyproject.toml` 与 `.pre-commit-config.yaml`。

## 技术栈

LangGraph（StateGraph 主流水线）/ FastMCP v3 / FastAPI + 静态 HTML 前端 / SQLite 3 层 + L3 JSON / MemOS（Qdrant + Redis）/ Cloudflare R2 / Poetry / GitHub Actions CI（lint → test → security）

## 参考

- [部署指南](docs/deployment.md) — 环境搭建全流程 + 常见坑
- [MCP 指南](docs/mcp.md) — 本地/公网 MCP 接入
- [架构蓝图](docs/Project_Architecture_Blueprint.md) — 全量架构图
