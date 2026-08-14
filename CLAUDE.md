# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目

z-winnow — **信息降噪压缩平台**。群聊 → [LangGraph 降噪压缩管道] → **L3 核心知识层**（议题/资源/工程/日报并列产物）→ [MCP 接口] → 用户/Agent。日报从"核心产物"降级为 L3 的一个产出视角；**MCP 接口**（`mcp.example.com`，key-based 权限：key→成员+群组白名单）是对外知识消费入口；ECS 常驻 7×24 可查可收反馈。架构定位详见 `docs/mcp-platform-checkpoint.md` §1-§2。

**关键约束**：禁止修改 `G:\code_library\winnow` 中的文件。所有新代码均在此目录中开发。

---

## 常用命令

```bash
# 安装依赖 + pre-commit hooks
python -m poetry install
pre-commit install

# 运行全部测试（mock 模式 — 无需 API key）
python -m poetry run pytest tests/ -v --tb=short

# 运行单个测试文件
python -m poetry run pytest tests/test_graph_builder.py -v

# 按名称运行指定测试
python -m poetry run pytest tests/test_graph_builder.py::test_build_graph_returns_compiled -v

# 跳过 slow/integration/e2e 测试
python -m poetry run pytest tests/ -v -m "not slow and not integration and not e2e"

# 测试覆盖率
python -m poetry run pytest tests/ --cov=src/z_winnow --cov-report=term-missing

# Lint + 格式检查
python -m poetry run ruff check .
python -m poetry run ruff format --check .

# 自动修复 lint 问题
python -m poetry run ruff check --fix .
python -m poetry run ruff format .

# 类型检查（基础模式，非阻断）
python -m poetry run mypy src/

# 安全扫描
python -m poetry run bandit -r src/ -ll --configfile pyproject.toml

# 运行全部 pre-commit 检查
pre-commit run --all-files

# CLI 命令
python -m poetry run winnow ingest --date 2026-04-28 --group "群名"
python -m poetry run winnow trace --server-id 1234567890
python -m poetry run winnow export --start 20260401 --end 20260428   # ⚠️ 导出 RL 训练数据（RL 已废弃，遗留保留）
python -m poetry run winnow web                                       # Web 控制面板，端口 :8100
python -m poetry run winnow judge --group "群名" --from 2026-04-20 --to 2026-04-28   # LLM-as-judge 报告质量评估（4 维度打分）
python -m poetry run winnow judge --group "群名" --date 2026-05-01                     # 单日评估
python -m poetry run winnow judge --group "群名" --latest 10                           # 最近 N 份
# 群组管理
python -m poetry run winnow group list                                    # 列出所有群组
python -m poetry run winnow group resolve --name "群名"                    # 按群名解析 → group_id + chatroom_id
python -m poetry run winnow group resolve --room-id xxx@chatroom           # 按 chatroom_id 解析 → group_id
python -m poetry run winnow group add --chatroom-id xxx@chatroom           # 注册新群组（幂等）
python -m poetry run winnow group add --chatroom-id xxx@chatroom --display-name "名称"  # 带显示名注册
# 首次运行须注册群组: python -m poetry run winnow group add --chatroom-id xxx@chatroom

# MemOS 管理
python -m poetry run winnow memos status                       # MemOS 健康状态
python -m poetry run winnow memos rebuild --group X --from sqlite  # 从 SQLite 重建 cube
python -m poetry run winnow memos vacuum --group X              # 触发生命周期扫描
python -m poetry run winnow memos export --group X --out path/   # dump cube 到文件
python -m poetry run winnow memos search --group X --query "..." # 命令行查询调试
python -m poetry run winnow memos delete-cube --group X          # 删除 :topics + :feedback cube 记忆（需确认；不含 resources/daily/自定义表）
python -m poetry run winnow memos delete-cube --group X -y       # 跳过确认直接删除
python -m poetry run winnow memos wipe-all                       # 全量清空所有群 MemOS 记忆（开发调试，需确认）
python -m poetry run winnow memos wipe-all --include-local -y    # 连本地数据一起清（保留 groups 注册）
python -m poetry run winnow memos flush                         # 强制处理 pending sync 任务
python -m poetry run winnow memos purge-wxid --group X [--dry-run]  # 清理含 wxid_ 的记忆节点

# 本地 ↔ ECS 数据同步（阶段 2，MCP 公网服务数据通道；需在 .env 配 WINNOW_ECS_SSH_HOST/KEY）
python -m poetry run winnow sync status                            # 本地 vs ECS 行数对比 + 待 pull 计数
python -m poetry run winnow sync push                             # 推 L3 快照 + processed JSON 到 ECS（mtime 懒重连，零中断）
python -m poetry run winnow sync push --dry-run                   # 只生成本地快照，不传输
python -m poetry run winnow sync push --no-processed              # 只推 l3_snapshot.db，跳过 processed JSON
python -m poetry run winnow sync pull                             # 拉 ECS 反馈 inbox → merge 本地 → 清 inbox（三阶段：WAL checkpoint → rsync+merge → DELETE 清；失败不清）
python -m poetry run winnow sync pull --dry-run                    # 只 merge 报告，不清 ECS inbox

# MCP API key 管理（key→成员/群组权限白名单，config/mcp_keys.yaml；改完 sync push 推 ECS）
python -m poetry run winnow mcp-key list                          # 列注册 key（脱敏：前缀+member+权限）
python -m poetry run winnow mcp-key add --member ID --name "名" --groups g1,g2  # 生成 key 绑成员+群权限
python -m poetry run winnow mcp-key add --member admin --admin    # 生成管理员 key（全权，忽略 --groups）
python -m poetry run winnow mcp-key revoke --key qrb_xxx          # 撤销 key
python -m poetry run winnow mcp-key allow --key qrb_xxx --groups g3  # 追加可访问群组

# MCP server 启动 + 配图
python -m poetry run winnow mcp                                            # MCP server（stdio 默认，本地 Claude Desktop/Cursor 集成）
python -m poetry run winnow mcp --transport http --port 8000 --host 0.0.0.0  # MCP server（http，远程/ECS 部署）
python -m poetry run winnow gen-image --record-id <report_id>             # 日报配图（DMX Gemini 原生 API）

# 定时日报调度（T-SCHED）——独立于 web，按各群 daily_schedule_cron 触发；目标=前一天，宕机跨多天补跑
python -m poetry run winnow scheduler                                       # 交互菜单（Rich 看板 + 设定向导：选群/时间预设/预览/立即跑）
python -m poetry run winnow scheduler status [--watch]                      # 看板：cron/下次触发/上次/缺失天数/调度器心跳
python -m poetry run winnow scheduler set <群> --cron "0 2 * * *" [--enable|--disable]  # 改 cron/启用（校验后落库）
python -m poetry run winnow scheduler run                                   # 常驻守护进程（启动先预检+补跑+每分钟 tick；tmux/nohup/launchd/systemd 保活）
python -m poetry run winnow scheduler run --once --now "2026-07-23T02:00:00+08:00"  # 单次评估（系统 cron/launchd 每分钟调，或调试注入时间）
python -m poetry run winnow scheduler run --skip-preflight                   # 跳过环境预检强跑（默认 critical 缺失会中止）
python -m poetry run winnow scheduler run --fix-deps                         # 预检失败时一键拉起 start_all.sh --no-web 再复检
python -m poetry run winnow scheduler doctor [--fix]                        # 环境体检（Docker/四容器/Qdrant collection/memos/数据源/LLM/DB）
python -m poetry run winnow scheduler next [--count 5]                       # 各群未来触发时间
# 保活：ECS 用 systemd 或系统 cron 每分钟 `scheduler run --once`；macOS 用 launchd/tmux/nohup。
# 关键：groups.daily_schedule_cron 现在被真实消费；幂等真源=report_versions(group_id,date)。

# MemOS 服务管理（Docker — 需先启动才能运行 pipeline）
# memos-api: Qdrant(向量) + Redis(scheduler)；Neo4j 容器必须启动（depends_on 健康检查），但 GeneralTextMemory 模式不依赖它存数据（仅 init_server() 后端占位）
cd deployments && docker compose --env-file ../.env up redis qdrant neo4j memos-api -d  # 四件套
cd deployments && docker compose ps                              # 查看服务状态
cd deployments && docker compose logs memos-api --tail 20        # 查看 API 日志
cd deployments && docker compose down                            # 停止所有服务
curl -s http://127.0.0.1:8000/docs                               # MemOS API 文档（验证可用）
# MemOS 依赖: Redis (6379) + Qdrant (6333) + memos-api (8000)
# 启动必须 --env-file ../.env，否则容器内 API key 为空
# MOS_CHAT_MODEL 不带 provider 前缀: "deepseek-v4-flash" ✓, "openai/deepseek-v4-flash" ✗
```

---

## 架构

### 平台全景（信息降噪压缩：本地生产 → L3 核心层 → ECS 常驻 → MCP 消费）

```
群聊 → [生产流水线] → L3 核心知识层 ──sync push──▶ ECS(ro 快照 + rw inbox)
                        ↑                                │ MCP 接口(mcp.example.com)
                        │                                ▼
                 MemOS 语义记忆                   外部 Agent / Claude
                        ↑                                │ submit_feedback
                  └── 反馈回路(pull inbox → 版本化重生成) ◄─┘
```

- **L3 = 核心知识层**：议题/资源/工程/日报**并列产物** + 版本管理（非"日报的最终产出"）
- **MCP 接口**是对外知识消费入口（`mcp.example.com`，key→成员+群组白名单）；MemOS 留本地不暴露
- **ECS 常驻** 7×24（双库：l3_snapshot ro + feedback_inbox rw）；本地周期启停不影响公网
- **反馈回路**：ECS 收反馈 → 本地 pull → 版本化重生成（守 L3 不可变红线）

### 图流水线（LangGraph StateGraph）

单 agent 直线流水线（Wave 12 将原来的 4 路并行 fan-out 合并为单一 `unified_reporter`）：

```
START → data_fetch → content_enrich → orchestrator → unified_reporter
    → output_composer → END

飞书推送不在主图，由 Web UI 通过 lark-cli 独立调用。
```

- `unified_reporter`：单次 LLM 调用完成日报 + 资源 + 工程问题 + 议题追踪 + 生命周期分类
- `output_composer`：L3 JSON 持久化 + `topic_summaries` / `report_versions` 写库 + MemOS 同步入队
- 每阶段各自写入存储层：data_fetch→L1, content_enrich→L2, output_composer→L3
- Markdown 渲染延迟到 Phase H（`export_markdown()` 手动触发），不在主图中

**入口**：`src/z_winnow/graph/builder.py` — `build_graph()` 返回编译后的 `CompiledStateGraph`。5 个主线节点（data_fetch、content_enrich、orchestrator、unified_reporter、output_composer），主图到 `output_composer → END` 直连终止。飞书推送由 Web UI 独立调用，非图节点。

### 状态 Schema

- **`OverallState`**（`src/z_winnow/state.py`）— 主图状态 TypedDict，按生命周期分阶段（Phase 0 输入 → Phase 1 数据抓取 → Phase 1.5 内容增强 → Phase 2 统一报告 → Phase 4 输出 → Phase 5 持久化）。`errors` 字段使用 `Annotated[list, operator.add]` reducer。

### 源码结构（`src/z_winnow/`）

| 模块 | 用途 |
|--------|---------|
| `graph/` | LangGraph StateGraph 构建器（`builder.py` 主图）、节点辅助（`graph/nodes/`：recovery） |
| `subagents/` | `unified_reporter/`（日报/资源/议题 + 自定义表统一 LLM agent）、`output_composer/`（L3 JSON 组装 + Jinja2 渲染）、`contracts`（子 agent I/O schema）、`incremental_prompt` |
| `pipeline/` | CipherTalk / WeFlow 客户端、SQLite 3 层存储、上下文组装、溯源、沙箱、Layer 3 JSON、反馈消费、群组配置、SQL 迁移 |
| `mcp_server/` | MCP 网关（FastMCP v3, 6 工具 + key-based 鉴权 + contextvars 注入 + 群组白名单） |
| `sync/` | 本地↔ECS 数据同步（push L3 快照 / pull 反馈 inbox / status 比对）+ transport（ssh/rsync） |
| `scheduler/` | 定时日报调度（T-SCHED，独立于 web）：`engine.DailyScheduler`（tick/backfill/run_forever/run_group_day，幂等真源=report_versions，目标=前一天，宕机跨多天补跑）+ `preflight`（复用 probe_connectivity + Docker/容器/Qdrant/DB 探活，critical 缺失拒绝启动）+ `views`/`interactive`（Rich 看板 + 设定向导）+ `status`（CLI 与 Web 共用数据层）+ `cli_dispatch`（`winnow scheduler` 子命令组）。复用 `orchestrate()`+`auto_push_after_run()`；幂等真源 `report_versions(group_id,date)`，⚠️ 勿用 `pipeline_runs.group_id`（存 display_name） |
| `web/` | FastAPI 纯 API 后端（`/api/v1` 前缀，16 路由 + 16 服务层 + 16 schema）+ `static/` 静态 HTML 前端（Open Design 设计，vanilla JS + Tailwind，挂载于 `/ui/`，git 跟踪）。前端架构见 `docs/web-frontend-architecture.md`；远程开发无需 OD（HTML 已入 git，`clone` + `winnow web` 即 `/ui` 预览） |
| `content_enrich/` | XML 解析、Vision API 图片分析、链接预览、卡片解析、媒体落盘 |
| `custom_tables/` | 自定义表框架（`TableDef` registry + Skill 提取 prompt；engineering/world_models 内置，YAML 加表即扩展） |
| `outputs/` | 日报配图生成(DMX Gemini 原生 API)、Markdown 报告写出 |
| `config/` | pydantic-settings（`Settings` 类）、模型工厂、日志配置 |
| `templates/` | Jinja2 模板（日报/工程/资源/议题/飞书日报）+ renderer |
| `rl/` | ⚠️ **RL 已废弃（遗留）** — 仅 `correction_loader` 服务 prompt 纠正消费（读 group_experiences/feedback_events），其余不再发展 |
| `memory/` | MemOS 适配器（3 模式：real/mock/disabled）、同步队列 worker、生命周期管理、feedback 同步 |
| `orchestrator/` | 任务编排入口 `orchestrate()`（Web API `runs`/`batch` 调用，非主图节点；deepagents 依赖已声明但未实际 import） |
| `observability/` | LangSmith 追踪、structlog 指标 |

### MCP Server（FastMCP v3，6 工具 + key-based 鉴权）

`mcp_server/server.py` — 公网 MCP 网关 `mcp.example.com` 的核心：
- **6 个 MCP 工具**: `list_groups` / `search_topics` / `get_topic` / `get_daily_report` / `list_resources` / `submit_feedback`（读 L3 + 写 feedback Inbox）
- **key-based 鉴权**: middleware（contextvars `_current_member`）校验 key → `resolve_member` 查 YAML → set `MemberInfo`；6 工具按群组白名单过滤（admin 全权）；ECS http 无 key 拒绝；stdio 本地 admin 兜底
- **ECS 双库路由**: `get_l3_db()`（ro snapshot, mtime 懒重连）+ `get_inbox_db()`（rw inbox，ECS 写；本地 pull merge 并清）；按 `deployment_target` 切换
- **key 注册表**: `mcp_keys.py`（`MemberInfo` + `load_keys`/`resolve_member`/`save_keys` + mtime 热重载），`config/mcp_keys.yaml`（gitignored）+ CLI `winnow mcp-key`

本地↔ECS 同步：`sync/push.py`（wal_checkpoint → backup 快照 → rsync tmp → ssh 原子 mv + processed + mcp_keys.yaml）+ `sync/pull.py`（checkpoint ECS WAL → rsync inbox → ATTACH+INSERT OR IGNORE → docker exec DELETE 清；两阶段防丢反馈）+ `sync/status.py`（行数比对）

### Web API（约 79 端点）

`app.py` 创建 FastAPI 应用，通过 `routes/__init__.py` 聚合 16 个路由模块到 `/api/v1` 前缀：

| 路由模块 | 端点 | 说明 |
|----------|------|------|
| `health` | 1 | `GET /api/v1/health` |
| `overview` | 1 | `GET /api/v1/overview` |
| `groups` | 8 | CRUD + list（分页/筛选）+ 飞书配置；DELETE 级联清理本地数据+磁盘 L3+MemOS 记忆 |
| `core_topics` | 4 | CRUD（按 group_id 筛选） |
| `judge` | 2 | POST 触发评估 + GET 状态查询 |
| `system` | 5 | info + config（脱敏）+ lark-cli 工具就绪检测 |
| `data` | 4 | 按层浏览（l1/l2/l3）、统计、溯源、L1 详情 |
| `data_preview` | 2 | 数据预检（source-check 直查 CipherTalk API + 本地 SQLite 快照） |
| `reports` | 14 | 列表、详情、版本、diff、regenerate、**版本回滚**、export、Feishu 推送、配图生成、**GET /regenerate/active**（运行中任务） |
| `memos` | 11 | cubes CRUD、search、rebuild、vacuum、memory 详情/删除、flush、DELETE /memos 全清（开发调试） |
| `key_people` | 5 | 列表、创建、动态字段更新（PUT）、软删除 |
| `runs` | 7 | 创建、列表、详情、SSE 流、取消 |
| `batch` | 4 | 批量任务调度（群选择 + 日期范围 + 实时进度）+ 取消 |
| `rl` | 2 | POST 异步导出 + GET 状态（⚠️ RL 已废弃，遗留保留） |
| `feedback` | 6 | 列表、创建、GET/{id}、**GET /{id}/provenance（溯源四元组）**、POST consume、POST rollback |
| `auth` | 2 | 登录态检测 + API key cookie 设置 |

+ `GET /` → 307 重定向到 `/ui/`

### 数据存储

**3 层 SQLite —— L1/L2 当日快照,L3 升级为核心知识层**(议题/资源/工程/日报并列产物 + 版本管理;MCP 接口与 ECS 只读快照均读此层)：

| 层 | 表 | 写入节点 | 内容 |
|-------|-----|---------|------|
| L1 | `raw_messages` | data_fetch | CipherTalk 原始消息（serverID 溯源），不可变 |
| L2 | `parsed_contexts` | content_enrich | Token 边界内的上下文块（含 Vision 图片描述/链接预取富化），不可变 |
| L3 | `topic_summaries` + `data/processed/{group_id}/{date}/v{n}/*.json` | output_composer | 议题摘要、日报/资源/工程 JSON。**M4 起按版本写 `v{version_number}/` 目录**；重跑产新版本（INSERT OR REPLACE + 确定性 summary_id） |

**MemOS 长期语义记忆（GeneralTextMemory 2.0，Qdrant + Redis）——M4 按内容类型细拆 cube**：

| Cube ID | 内容 | 反馈 target_type |
|---------|------|-----------------|
| `winnow:{gid}:topics` | 议题节点（一议题一节点） | topic |
| `winnow:{gid}:resources` | 资源节点 | resource |
| `winnow:{gid}:daily` | overview/trend/highlights/notice 节点 | report, trend |
| `winnow:{gid}:{table_id}` | 自定义表记录节点（registry 驱动，按群开关激活） | `{table_id}`（engineering/world_models/…） |
| `{gid}:empty_days` | 空日信号 | — |

> cube scope 真源：`memos_service.cube_scopes_for_group(config)` = 固定 `{topics,resources,daily}` ∪ 群激活的自定义表。加新自定义表 = YAML 加一条，cube 自动出现。旧 `winnow:{gid}:feedback` cube **逐步收敛中**：POST /feedback 仍走 `feedback_sync` 写旧 cube，新的 `feedback_memory`（MemOS 2.0 原生 `/product/feedback`，按 target_type 纠正对应 cube）与之过渡期并存。

### 反馈与版本管理（M4）

每个反馈事件 = **可溯源、可回滚**的版本化事件，记录四元组：反馈内容 + 反馈对（内容 + 被反馈版本/议题索引）+ 介入后产出新版本 + 对应 MemOS 节点。

- **闭环**：提交反馈（仅入 SQLite）→ 点「根据反馈重生成」（弹窗预览将注入的反馈 → 确认）→ regenerate 产新版本 → `_finalize_regeneration` 回填 `produced_version_id` + `mark_consumed` + 派生 `group_experiences` + `feedback_memory` 纠正对应 cube。
- **溯源**：`GET /feedback/{id}/provenance` → 四元组（反馈本体 + target 版本议题内容 + produced 版本 + MemOS 双节点）。
- **回滚**：`POST /reports/{rid}/versions/{vid}/rollback` → `report_versions.is_active` 重指（active≠latest）；其后版本的 feedback 标 `rolled_back`、经验归档。
- **regenerate**：去重（同报告运行中→复用 task）+ 超时护栏（ainvoke 480s，`_finalize` 无条件执行）+ LangSmith tracing + **regen 时从 L2 还原富化内容**（图片描述不丢，`[图片]` 不残留）。
- **经验消费**：`correction_loader` 主源 `group_experiences`（active），feedback_events 为 fallback；注入 unified_reporter `<prior_corrections>`。反馈提交时自动解析 `original_text`（被反馈目标在 target 版本的原内容）。
- ⚠️ **feedback_events.date 存 YYYY-MM-DD**（前端 normDate 后），`report_versions.date` 存 YYYYMMDD——`get_unconsumed_feedback` 已容忍两种格式。

详见 `docs/mcp-platform-checkpoint.md`（平台架构 + 反馈闭环 + 鉴权模型）。

### 数据目录地图（测试前清空数据必读）

**清空全部数据**（测试隔离）：`poetry run python scripts/clear_all.py --all-dates --groups "群A" "群B"`

#### SQLite 数据库

**文件**：`data/winnow.db`（+ `-wal` / `-shm` WAL 日志）

| 表 | 用途 | 写入节点 | 清空策略 |
|---|------|---------|---------|
| `groups` | 注册群组（**不清，永久保留**） | Web UI / CLI | 不删 |
| `core_topics` | 用户定义的核心议题 | Web UI | 不删 |
| `group_members` | 群成员缓存 | content_enrich | 不删 |
| `raw_messages` | L1 原始消息，`group_id`=内部 UUID | data_fetch | 按 group_id + date 删 |
| `parsed_contexts` | L2 上下文块，`group_id`=内部 UUID | content_enrich | 按 group_id + date 删 |
| `topic_summaries` | L3 议题摘要，`group_id`=内部 UUID | output_composer | 按 group_id + date 删 |
| `report_versions` | 报告版本记录，**M4: `is_active` 列**（当前生效版本，回滚后≠最新） | output_composer | 按 group_id + date 删 |
| `pipeline_runs` | 运行历史，⚠️ **group_id 列存 display_name** | pipeline | 按 display_name + date 删 |
| `memos_sync_queue` | MemOS 同步队列，cube_id=`winnow:{group_id}:{scope}`（topics/resources/daily/自定义表） | sync_worker | 按 cube_id 删 |
| `feedback_events` | 反馈事件，**M4: +9 溯源列**（target_version_id/target_topic_id/produced_version_id/memos_cube_id/memos_node_id/archived_memos_id/status/rolled_back_*） | Web UI | 按 group_id + date 删 |
| `group_experiences` | **M4: 派生可召回经验**（群绑定、可编辑、跨天，L3 不进 MemOS），correction_loader 主源 | _finalize_regeneration | 按 group_id 删 |
| `async_tasks` | 后台异步任务 | Web API | 不删（历史记录） |
| `batch_jobs` / `batch_job_items` | 批量任务 | Web API | 不删 |

> **关键坑**：`pipeline_runs.group_id` 存的是 **display_name**（如"阿壳测试群"），不是内部 UUID（如 `g_9bbb910567af`）。`raw_messages`/`parsed_contexts`/`topic_summaries`/`report_versions` 的 `group_id` 列存的是内部 UUID。清空时两者的匹配方式不同。

#### 文件系统

| 路径 | 用途 | 写入节点 |
|------|------|---------|
| `data/processed/{group_id}/{date}/v{n}/` | **M4: 版本化 L3 JSON**（daily/topics/resources/{custom}.json，每版本一目录） | output_composer |
| `data/processed/{group_id}/{date}/` | 扁平路径（旧数据 + 回退读取，`resolve_l3_dir` 优先 v{n}/） | 旧版 output_composer |
| `data/tmp/chat_context_*.md` | Chat Context Markdown 临时文件 | content_enrich |
| `data/rl/` | RL 训练数据导出 JSONL（⚠️ RL 已废弃，遗留） | CLI export |

#### MemOS 语义记忆（Qdrant + Redis）

| 资源 | 地址 | 说明 |
|------|------|------|
| MemOS API | `http://localhost:8000` | Docker 容器 |
| Qdrant | `http://127.0.0.1:6333` | 向量数据库 |
| Redis | `127.0.0.1:6379` | 同步队列 |

| Cube ID 模式 | 内容 | 写入方式 |
|-------------|------|---------|
| `winnow:{group_id}:topics` | 议题节点（一议题一节点） | sync_worker 异步写入 |
| `winnow:{group_id}:resources` | 资源节点 | sync_worker |
| `winnow:{group_id}:daily` | overview/trend/highlights/notice 节点 | sync_worker |
| `winnow:{group_id}:{table_id}` | 自定义表记录节点（engineering/world_models/…，registry 驱动） | sync_worker |
| `{group_id}:empty_days` | 空日跟踪 | lifecycle 管理 |

**Qdrant collection**：`neo4j_vec_db`（唯一 collection，所有群共享，dim=3072 Cosine，对齐 text-embedding-3-large）

> ⚠️ **collection 不自动重建（2026-07-18 实测）**：MemOS 写入时若 collection 不存在，向量插入直接 404，节点落 `vector_sync: failed`，**search 召回不到**。症状：`memos-api` 日志狂刷 `Collection neo4j_vec_db doesn't exist`。修复——手动建：
> ```bash
> curl -X PUT http://127.0.0.1:6333/collections/neo4j_vec_db \
>   -H 'Content-Type: application/json' \
>   -d '{"vectors":{"size":3072,"distance":"Cosine"}}'
> ```
> 重建后跑 `winnow memos rebuild` 补历史节点的向量（failed 的旧节点不会自动补）。

> **关键坑**：MemOS `delete_memory()` 只删树节点，**不清底层 Qdrant 向量**。残留向量会让 Scheduler RAG 在下次写入时注入旧数据（如 wxid_ 标识符）。彻底清空必须删 Qdrant collection（`clear_all.py` 自动处理）。

> **MemReader 行为（2026-07-18 实测，服务端 API 1.0.1）**：`/product/add` **不接受外部 metadata**——传 tags/entities/key/memory_type 会被静默丢弃，metadata 由 fine MemReader LLM 自动生成。`memory_content` 会被 LLM 改写成自然语言陈述句，**参与人/议题词自动提取进 `tags`**（实测：马毅/瞿炜/IBN_Blank/世界模型/费曼学习法 都进 tags）。`memory_type` 按 role 判（灌 `memory_content` 走 fine 提取成 LongTermMemory/WorkingMemory；旧 `messages role=user` 灌法会落 UserMemory 且 tags 空——已迁 `add_structured_memory`/`memory_content`）。结论：**参与人可检索由 fine tags 自动达成**，无需用户控制 entities；用户控制 metadata 需要 winnow 进程内嵌入式 MemCube（大重构，当前不走）。`/product/search` 支持 `mode`（fast/fine/mixture，默认 fine，adapter 已默认 fine；fine 偶发空结果时可传 mixture）。

#### 快速诊断命令

```bash
# 群组列表
poetry run winnow group list

# MemOS 状态（cube 节点数 + sync queue）
poetry run winnow memos status

# SQLite 各表行数
poetry run python -c "
import aiosqlite, asyncio
async def main():
    async with aiosqlite.connect('data/winnow.db') as db:
        for t in ['raw_messages','parsed_contexts','topic_summaries','pipeline_runs','report_versions','memos_sync_queue','feedback_events']:
            c = (await (await db.execute(f'SELECT COUNT(*) FROM {t}')).fetchone())[0]
            print(f'  {t}: {c}')
asyncio.run(main())
"

# L3 JSON 文件分布
find data/processed -name "*.json" -type f | head -20

# Qdrant 向量数
curl -s http://127.0.0.1:6333/collections/neo4j_vec_db | python -m json.tool
```

### 配置

- `.env` — 所有配置项见 `.env.example`（每项有注释：API key / 模型 / 数据源 / ECS / MCP）；pydantic-settings 加载
- `Settings` 类（`config/settings.py`）— `WINNOW_*` 环境变量覆盖标准名，线程安全单例 `get_settings()`；默认 provider=deepseek（`deepseek-v4-flash`）
- `data/config_overrides.json` — onboarding 向导「保存并重启」写入，**最高优先级**（init kwargs 注入，盖过 env）；测试 autouse fixture `_neutralize_config_overrides` 中和它
- Mock 模式：`WINNOW_REAL_LLM=false` 禁用 LLM 调用（测试自动启用）
- 配置优先级：`Field 默认值 < .env < 标准环境变量 < WINNOW_* < data/config_overrides.json < CLI 参数`（⚠️ `config/defaults.yaml` 是参考文档，**不被代码加载**）

### 测试设施

- `tests/conftest.py` 注册 pytest 标记（slow/integration/e2e）+ 一个共享 autouse fixture `_neutralize_config_overrides`：中和 onboarding 向导写的 `data/config_overrides.json`（其作为 `get_settings()` 的 init kwargs 会盖过环境变量，把 `db_path` 等钉死到真实路径，导致测试无法隔离）。该 fixture 全套件生效，让测试由环境变量驱动隔离
- 每个测试文件自行构造数据（内联 dict 或文件内 `_make_*`/`_seed_*` 辅助函数），无集中式数据工厂
- pytest 配置：`asyncio_mode = "auto"`、`pythonpath = ["src"]`、`--strict-markers`
- 70 个测试文件，全部在 mock 模式下运行 — 无需 API key
- 标记：`slow`、`integration`、`e2e`（可用 `-m "not slow"` 跳过）

### 代码质量

- **Ruff**：lint + format，配置在 `pyproject.toml`（Python 3.12，行宽 100）
- **MyPy**：基础类型检查（非阻断）
- **Bandit**：安全扫描（`-ll` 级别，跳过 B101）
- **Pre-commit**：YAML/TOML 检查 + end-of-file-fixer + trailing-whitespace + ruff lint/format + mypy + bandit + 大文件/私钥检测

---

## 开发团队（qr-* Agents）

Box0 DAG 工作流编排 5 个 agent：

| Agent | Box0 名称 | 阶段 |
|-------|-----------|-------|
| **qr-planner** | `qr-planner` | PLAN — 架构 + 规格 + 任务卡片 |
| **qr-dispatcher** | `qr-dispatcher` | CONTRACT — 扫描卡片、协商、创建工作流运行 |
| **qr-builder** | `qr-builder` | BUILD — 按卡片规格实现 |
| **qr-evaluator** | `qr-evaluator` | EVALUATE — 对抗性审查（简单任务可条件跳过） |
| **qr-curator** | `qr-curator` | EVOLVE — 捕获模式/反模式/经验教训 |

循环：`PLAN → CONTRACT → BUILD → EVALUATE → EVOLVE`（失败时 fix_task 重试）。

### Sprint 契约

任务卡片位于 `plans/active/cards/`（YAML frontmatter + Markdown），包含三部分：
1. **Spec**（qr-planner）— 目标、文件、约束、接口
2. **Acceptance Criteria**（qr-evaluator）— 二元 PASS/FAIL 可测试标准
3. **Experience Context**（qr-curator）— 需应用的模式、需避免的反模式

Track 文件位于 `plans/active/tracks/`，是链接到卡片的轻量索引（当前：W7、W8、W9、W11、W13、W14、W15、W16）。

### 派发方式

```bash
b0 run qr-dispatcher "单次激活协议"
```

或手动操作：读取 `plans/progress.json` → 找到待处理任务 → 遵循 CONTRACT 阶段。

---

## 经验系统

- 模式：`docs/experiences/patterns/P###-*.md`（P001–P095）
- 反模式：`docs/experiences/anti-patterns/A###-*.md`（A001–A034）
- 教训：`docs/experiences/lessons/L###-*.md`（L001–L117）
- 规划：`docs/experiences/planning/P###-*.md`（P001–P027，consumer=planner）

流程：CONTRACT 阶段 **注入** → EVALUATE 阶段 **交叉引用** → EVOLVE 阶段 **捕获**。

---

## 关键文件

| 文件 | 用途 |
|------|---------|
| `plans/progress.json` | 项目状态唯一可信源（FSM） |
| `plans/active/cards/` | 活跃任务卡片 |
| `plans/active/tracks/` | 活跃 track 索引 |
| `docs/experiences/SPRINT_CONTRACT.md` | 契约格式规范 |
| `docs/experiences/INJECTION_PROTOCOL.md` | 经验注入格式 |
| `docs/experiences/index.md` | 经验目录（含全部模式/反模式/教训索引） |
| `schemas/` | 全部报告类型的 JSON Schema（v1） |
| `pyproject.toml` | Poetry 依赖 + Ruff/MyPy/Pytest/Coverage/Bandit 配置 |
| `.pre-commit-config.yaml` | Pre-commit hooks |

## 技术栈

| 层级 | 技术选型 |
|-------|--------|
| 编排 | LangGraph（StateGraph —— 有状态图编排运行时；主流水线单 agent 直线，未用 interrupt） |
| MCP | FastMCP v3（6 工具 + middleware contextvars 鉴权 + 双库路由）|
| Web 编排入口 | `orchestrator/orchestrate`（POST /runs、batch scheduler 调用；任务拆解 + 调度 graph；deepagents 依赖已声明但未实际 import） |
| 模型 | 环境变量配置（`WINNOW_*_MODEL`），默认 DeepSeek `deepseek-v4-flash`；per-subagent 可覆盖（unified_reporter/output_composer/topic_tracker） |
| 存储 | SQLite（3 层 + serverID 溯源）+ Layer 3 JSON |
| 向量记忆 | MemOS（Qdrant + Redis，GeneralTextMemory） |
| Web | FastAPI 纯 API 后端 + 静态 HTML 前端（Open Design 设计，vanilla JS + Tailwind） |
| 可观测性 | LangSmith |
| 包管理 | Poetry |
| CI | GitHub Actions（lint → test → security） |

## 参考

- 原始 winnow：`G:\code_library\winnow`（只读）
- 原始 winnow skill：`G:\code_library\my-skills\winnow`
- Box0 服务器：`http://127.0.0.1:8080`
- LangChain Context7 ID：`/websites/langchain_oss_python_langchain`
- LangGraph Context7 ID：`/websites/langchain_oss_python_langgraph`
