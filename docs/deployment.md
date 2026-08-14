# 部署与环境配置指南

本文档覆盖从零搭建 winnow 的完整路径。按需阅读——**不是所有组件都要装**：

| 你想达到的效果 | 需要的组件 |
|----------------|-----------|
| 跑通全部测试 / 浏览代码 | 只要 Python + Poetry（Mock 模式，零 API key） |
| 生成真实日报 | + LLM API key + 群聊数据源 API |
| 长期群记忆（跨日议题演化） | + Docker（MemOS 四件套） |
| 对外提供 MCP 知识接口 | + 服务器部署（见 [mcp.md](./mcp.md)） |

---

## 1. 基础环境

**要求**：Python 3.12+、[Poetry](https://python-poetry.org/)、Git。

```bash
git clone https://github.com/zenthos-z/z-winnow.git && cd z-winnow
poetry install --with dev
cp .env.example .env        # 先复制，后面逐步填
```

**验证安装**（Mock 模式，不需要任何 API key）：

```bash
poetry run pytest tests/ -v --tb=short    # 应全绿
poetry run winnow web                     # 打开 http://127.0.0.1:8100/ui/
```

> Mock 模式由 `WINNOW_REAL_LLM=false` 控制（测试自动启用），它禁用 LLM 调用但**不替代数据源**——没有数据源 API 时只能跑测试，不能产真实报告。

---

## 2. LLM 配置

默认 provider 为 **DeepSeek**（`deepseek-v4-flash`），也支持任意 OpenAI 兼容端点（OpenRouter / SiliconFlow / 中转 API 等）与 Anthropic。

`.env` 最小配置：

```bash
WINNOW_DEEPSEEK_API_KEY=sk-...        # 必填（默认 provider）
# 可选：换模型
WINNOW_DEEPSEEK_MODEL=deepseek-v4-flash
```

**用 OpenAI 兼容中转**（复用同一个 key 的场景）：

```bash
WINNOW_OPENAI_API_KEY=sk-...
WINNOW_OPENAI_BASE_URL=https://your-proxy.example.com/v1
WINNOW_MODEL=your-model-name
WINNOW_ORCHESTRATOR_PROVIDER=openai
```

**用 Anthropic**：

```bash
WINNOW_ANTHROPIC_API_KEY=sk-ant-...
# 走第三方中转时再配 base_url（OpenAI 兼容协议）
# WINNOW_ANTHROPIC_BASE_URL=https://your-proxy.example.com
```

**每个子 agent 可独立换模型**（能力分级 / 省钱）：`WINNOW_UNIFIED_REPORTER_MODEL`、`WINNOW_OUTPUT_COMPOSER_MODEL`、`WINNOW_TOPIC_TRACKER_MODEL`、`WINNOW_VISION_MODEL` 等，见 `.env.example` 注释。

> 配置优先级：`CLI 参数 > WINNOW_* 环境变量 > 标准环境变量（如 ANTHROPIC_API_KEY）> .env > 默认值`。
> Web UI 的 onboarding 向导写入的 `data/config_overrides.json` 优先级最高。

---

## 3. 群聊数据源（必须自备）

**winnow 不包含消息采集能力**——它消费「群聊消息 HTTP API」，你需要自己运行其中一种数据源：

| 数据源 | 说明 | 配置 |
|--------|------|------|
| **CipherTalk** | 默认。`/v1/` 协议 | `WINNOW_CIPHERTALK_BASE_URL` + `WINNOW_CIPHERTALK_TOKEN` |
| **WeFlow** | legacy `/api/v1/` 协议 | `WINNOW_DATA_SOURCE=weflow` + `WINNOW_WEFLOW_BASE_URL` + `WINNOW_WEFLOW_TOKEN` |

两者默认地址均为 `http://127.0.0.1:5031`（本机部署的数据源服务）。

**注册你的群**（首次必做，幂等）：

```bash
# chatroom_id 形如 12345678@chatroom，从你的数据源侧获取
poetry run winnow group add --chatroom-id 12345678@chatroom --display-name "我的群"
poetry run winnow group list
```

> 群组注册后，`groups` 表永久保留；重置数据时不会被清掉。

---

## 4. MemOS 语义记忆（可选，推荐）

不需要跨日议题演化 / 群记忆时，设 `WINNOW_MEMOS_ENABLED=false` 即可跳过本节，核心流水线零 Docker 依赖。

启用则需 Docker 四件套（Redis + Qdrant + Neo4j + memos-api）：

```bash
cd deployments
docker compose --env-file ../.env up redis qdrant neo4j memos-api -d
docker compose ps                                  # 四个容器应 healthy/running
curl -s http://127.0.0.1:8000/docs | head -5       # MemOS API 文档可达 = OK
```

`.env` 中 MemOS 相关项：

```bash
WINNOW_MEMOS_ENABLED=true
WINNOW_MEMOS_API_URL=http://127.0.0.1:8000
WINNOW_MOS_CHAT_MODEL=deepseek-v4-flash      # ⚠️ 不带 provider 前缀（✓ deepseek-v4-flash，✗ openai/deepseek-v4-flash）
WINNOW_MOS_EMBEDDER_MODEL=text-embedding-3-large
WINNOW_MOS_EMBEDDER_BACKEND=openai
```

### 三个已知的坑（省你半天）

1. **启动必须带 `--env-file ../.env`**——否则容器内 API key 为空，memos-api 起来但不工作。
2. **Qdrant collection 不会自动创建**。MemOS 写入时若 collection 不存在，向量插入直接 404，节点落 `vector_sync: failed` 且 search 召回不到（日志刷 `Collection neo4j_vec_db doesn't exist`）。手动建：
   ```bash
   curl -X PUT http://127.0.0.1:6333/collections/neo4j_vec_db \
     -H 'Content-Type: application/json' \
     -d '{"vectors":{"size":3072,"distance":"Cosine"}}'
   # 之后跑 poetry run winnow memos rebuild 补历史节点向量
   ```
   向量维度须与 embedder 模型对齐（text-embedding-3-large = 3072，可用 `WINNOW_EMBEDDING_DIMENSION` 调整）。
3. **MemOS 首次启动需要 gpt2 tokenizer**（容器内 HuggingFace 下载，国内网络易卡死）。本仓库 `deployments/` 已带预缓存，若自行构建镜像请保留该缓存卷。

### MemOS 运维命令速查

```bash
poetry run winnow memos status                        # 健康状态 + cube 节点数
poetry run winnow memos rebuild --group <gid> --from sqlite   # 从 SQLite 重建记忆
poetry run winnow memos search --group <gid> --query "..."    # 检索调试
poetry run winnow memos flush                         # 强制处理 pending 同步任务
```

---

## 5. 首次跑通

```bash
# 1. 注册群（见上节）
# 2. 单日入库 + 生成
poetry run winnow ingest --date 20260814 --group "我的群"
# 或：打开 Web UI（推荐，有向导）
poetry run winnow web    # http://127.0.0.1:8100/ui/
```

Web UI 支持：数据预检（不写库先看数据源里有多少消息）、单次运行、批量任务（多群 × 日期范围）、报告浏览 / 版本对比 / 回滚、反馈提交与重生成、飞书推送、调度配置。

产物落点：

- SQLite：`data/winnow.db`（L1 `raw_messages` / L2 `parsed_contexts` / L3 `topic_summaries` + `report_versions`）
- L3 JSON：`data/processed/{group_id}/{date}/v{n}/`（daily / topics / resources / 自定义表）
- Markdown：`data/reports/`（`export_markdown` 触发）

---

## 6. 定时日报调度（可选）

独立于 Web 的调度器，按每群 `daily_schedule_cron` 自动跑「前一天」的日报，宕机跨多天自动补跑：

```bash
poetry run winnow scheduler            # 交互菜单（看板 + 设定向导）
poetry run winnow scheduler run        # 常驻守护（tmux/nohup/launchd/systemd 保活）
poetry run winnow scheduler doctor     # 环境体检（Docker/容器/Qdrant/DB/LLM 全探活）
```

轻量方案：系统 cron 每分钟调 `winnow scheduler run --once`（自带触发判断，未到点直接退出）。

幂等保证：以 `report_versions(group_id, date)` 为真源，同一天重复触发不会产出重复版本。

---

## 7. 可选组件

### 飞书多维表格推送

产物一键推送飞书 Bitable。需要自建飞书应用 + `lark-cli`，Web UI「系统设置」里有就绪检测与配置向导。每群可选开启：议题 / 资源 / 日报汇总（必选）+ 工程问题等自定义表。

### 日报配图

`poetry run winnow gen-image --record-id <report_id>`（DMX API，Gemini 生图）。

### 对象存储（Cloudflare R2 / S3 兼容）

附件（图片 / PDF）上传私有桶，MCP 消费侧按需生成短期预签名 URL。`.env` 配 S3 兼容凭证后：

```bash
poetry run winnow r2 status     # 就绪检查
poetry run winnow r2 upload     # 扫描 resources.json 全量回填
```

### 公网 MCP 服务

把 L3 知识暴露为 MCP 接口供外部 Agent 消费（含鉴权、反馈回收），见 [mcp.md](./mcp.md)。

---

## 8. 数据重置

```bash
poetry run python scripts/clear_all.py --all-dates --groups "群A"   # 按群清
poetry run python scripts/clear_all.py --help                       # 全量清（SQLite+MemOS+Qdrant+文件）
```

> `groups` 注册表、`core_topics`、运行历史不清；MemOS 彻底清空需删 Qdrant collection（脚本已处理）。

---

## 常见问题

| 症状 | 原因 / 解法 |
|------|------------|
| 测试全绿但 ingest 报错 | 数据源 API 不可达或群未注册；先 `winnow group list` + Web UI 数据预检 |
| memos-api 日志刷 `Collection ... doesn't exist` | 见 §4 坑 2，手动建 collection |
| 记忆 search 召回不到旧节点 | collection 曾被删过，`winnow memos rebuild` 补向量 |
| 报告里图片只剩 `[图片]` | Vision 未配置或超时；配 `WINNOW_VISION_API_KEY`，重生成时系统会从 L2 还原富化内容 |
| 想换 LLM 但部分调用还是旧模型 | 子 agent 有独立模型配置（§2 末），逐个检查 `WINNOW_*_MODEL` |
