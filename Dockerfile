# syntax=docker/dockerfile:1
# ============================================================
# winnow MCP server 部署镜像（仅 MCP http，不含数据/密钥/MemOS）
# 阶段二 ECS 常驻层 — docs/mcp-platform-checkpoint.md §4.3
# ============================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

# --- 运行时依赖（从 poetry.lock 精确导出，pip 装比 poetry install 快且稳）---
COPY requirements-deploy.txt ./
RUN pip install -r requirements-deploy.txt

# --- 项目源码 + 打包元数据 + 运行时配置 ---
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY config/ ./config/
COPY schemas/ ./schemas/
# 消费方接入文档（公开）—— MCP server 通过 GET /install、/feedback-format 对外提供，
# 让消费方无需 GitHub（部分地区需代理）也能取到文档。
# ecs-deploy.sh 把 .claude/skills/winnow-mcp/ rsync 到顶层 skill_public/（避开
# .dockerignore 对 .claude/ 的排除），这里 COPY 进镜像 /app/skill。
COPY skill_public/ /app/skill/
# templates/ 在 src/z_winnow/templates/（包内），已随 COPY src/ 包含

# 装项目本身（生成 winnow CLI 入口；依赖已装，--no-deps）
RUN pip install -e . --no-deps

# --- 数据目录（空壳；运行时由 sync 模块填充，或挂载 volume）---
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 8000

# 默认 mock 模式（不调真实 LLM；MCP 只读 L3 + 写 feedback，不需 LLM）
# sqlite 路径指向挂载的数据目录
ENV WINNOW_REAL_LLM=false \
    WINNOW_SQLITE_DB_PATH=/app/data/winnow.db \
    WINNOW_LAYER3_OUTPUT_DIR=/app/data/processed \
    WINNOW_DEPLOYMENT_TARGET=ecs \
    WINNOW_L3_SNAPSHOT_PATH=/app/data/l3_snapshot.db \
    WINNOW_FEEDBACK_INBOX_PATH=/app/data/feedback_inbox.db \
    WINNOW_MCP_KEYS_PATH=/app/data/mcp_keys.yaml

# MCP http server — host 必须显式 0.0.0.0（FastMCP 默认 127.0.0.1 容器外不可达）
CMD ["winnow", "mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
