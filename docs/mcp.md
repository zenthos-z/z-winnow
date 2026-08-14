# MCP 接口指南

winnow 通过 **MCP（Model Context Protocol）** 对外提供知识消费接口：外部 Agent（Claude Desktop / Cursor / 任意 MCP 客户端）可以查询群聊沉淀的议题、日报、资源，并提交反馈驱动知识库迭代。

**6 个工具**：

| 工具 | 场景 | 关键参数 |
|------|------|---------|
| `list_groups` | 浏览可访问群组 | —（按 key 白名单过滤） |
| `search_topics` | 模糊检索议题 | `query`, `group_id?`, `date_from?`, `date_to?`, `limit?` |
| `get_topic` | 议题详情 + 演化时间线 + 相关反馈 | `summary_id` |
| `get_daily_report` | 日报回看 | `group_id`, `date`, `version?` |
| `list_resources` | 资源列表（含附件预签名直链） | `group_id`, `date`, `version?` |
| `submit_feedback` | 提反馈（reporter 由 key 绑定，不可伪造） | `group_id`, `date`, `target_type`, `signal`, `content` |

---

## 场景 A：本地 stdio（最简路径，无需服务器）

适合个人使用 / 开发调试。直接把 MCP server 挂进 Claude Desktop、Cursor 等：

**Claude Desktop** 配置（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "winnow": {
      "command": "poetry",
      "args": ["run", "winnow", "mcp"],
      "cwd": "/path/to/z-winnow"
    }
  }
}
```

**Cursor**（`.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "winnow": {
      "command": "poetry",
      "args": ["run", "winnow", "mcp"],
      "cwd": "/path/to/z-winnow"
    }
  }
}
```

stdio 本地模式下自动以 **admin 兜底身份**运行（开发者全权），无需配 key。数据读本地 SQLite L3。

---

## 场景 B：key-based 多人服务（http 部署）

适合让**多位群成员**用自己的 AI 助理查群知识。架构：

```
本地（生产节点）── sync push ──▶ 服务器（L3 只读快照 + 反馈收件箱 rw）
        ▲                                        │ MCP http 接口 + key 鉴权
        └──────── sync pull（回收反馈）◀── 外部 Agent submit_feedback
```

### 1. 生成与分发 API key

key 绑定「成员 + 可访问群组白名单」，注册表 `config/mcp_keys.yaml`（**不入库**，模板见 `config/mcp_keys.yaml.example`）：

```bash
poetry run winnow mcp-key list                                        # 列 key（脱敏）
poetry run winnow mcp-key add --member alice --name "Alice" --groups g1,g2   # 生成并绑权限
poetry run winnow mcp-key add --member admin --admin                   # 管理员 key（全权）
poetry run winnow mcp-key allow --key wn_xxx --groups g3               # 追加群组
poetry run winnow mcp-key revoke --key wn_xxx                          # 撤销
```

鉴权模型：key → `MemberInfo`（member_id + 群组白名单 + is_admin）注入 contextvars；6 个工具按白名单过滤，`submit_feedback.reporter` 由 key 绑定，调用方无法伪造署名。key 文件 mtime 热重载，改完即生效。

### 2. 服务器侧启动

```bash
docker build -t z-winnow:latest .
docker run -d --name z-winnow \
  -e WINNOW_DEPLOYMENT_TARGET=ecs \
  -e WINNOW_MCP_API_KEY=<server-admin-key> \
  -v /srv/winnow/data:/app/data \
  -p 127.0.0.1:8101:8000 \
  z-winnow:latest
```

`WINNOW_DEPLOYMENT_TARGET=ecs` 启用双库路由：读 `data/l3_snapshot.db`（只读快照），写 `data/feedback_inbox.db`（反馈收件箱）。

建议绑定 `127.0.0.1` 端口，由前置 Nginx / Caddy / OpenResty 做 HTTPS 反代（MCP SSE 需**关闭 proxy buffering**）。

### 3. 本地 ↔ 服务器同步

`.env` 配置 SSH 访问（`WINNOW_ECS_SSH_HOST` / `WINNOW_ECS_SSH_KEY` / `WINNOW_ECS_DATA_DIR` 等），然后：

```bash
poetry run winnow sync push            # 推 L3 快照 + processed JSON + mcp_keys.yaml（原子替换，零中断）
poetry run winnow sync pull            # 拉反馈收件箱 → merge 本地 → 清空远端（三阶段防丢）
poetry run winnow sync status          # 双侧行数对比 + 待 pull 计数
```

本地周期启停不影响公网服务；反馈 pull 回来后在 Web UI 里走「根据反馈重生成」闭环。

### 4. 消费者接入

各客户端（Claude Desktop / Codex CLI / curl / Python 等）的完整配置示例与连接自检脚本见 [`.claude/skills/winnow-mcp/`](../.claude/skills/winnow-mcp/)：

- `INSTALL.md` — 拿 key → 配客户端 → 连接自检
- `references/client-configs.md` — 各 MCP 客户端配置片段
- `references/feedback-format.md` — `submit_feedback` payload 权威规格
- `scripts/validate_feedback.py` — 提交前本地校验脚本（零依赖）

---

## 反馈闭环（submit_feedback 之后发生什么）

1. 服务端 schema 校验（`signal` ∈ correction/supplement/approval/stale/quality；`target_type` ∈ topic/report/trend/highlights/resource/section ∪ 自定义表 id；`date` 为真实日历日期）——不合法直接拒绝，不写库
2. 合法反馈入收件箱 → 本地 `sync pull` merge
3. Web UI「根据反馈重生成」→ 产出新版本 L3 → 回填溯源四元组（反馈 + 目标版本 + 新版本 + MemOS 节点）
4. 派生群经验注入后续分析提示词，记忆节点按 `target_type` 纠正

全程版本化、可溯源（`GET /api/v1/feedback/{id}/provenance`）、可回滚（`POST /api/v1/reports/{rid}/versions/{vid}/rollback`）。
