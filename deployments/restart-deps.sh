#!/usr/bin/env bash
# restart-deps.sh — 重启 MemOS 依赖容器，让 infra .env 完全生效。
#
# 由「初始化引导 · 保存并重启」在写入 infra 变量（REDIS_PASSWORD 等）到项目 .env
# 之后异步（detached）触发；与 app 进程的 os.execv 重启并行。
#
# 为什么需要它：app 端配置（memos_api_url / mos_chat_model / ...）由 app 重启即可生效；
# 但 Qdrant / Redis / memos-api 是独立容器，docker compose restart 不会重新读取 .env
# 插值变量——必须 `up -d --force-recreate` 才能用上新值（如 REDIS_PASSWORD）。
#
# 也可手动执行：bash deployments/restart-deps.sh
# 日志：data/restart-deps.log
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/data"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/restart-deps.log"

cd "$SCRIPT_DIR" || { echo "无法进入 $SCRIPT_DIR" >> "$LOG"; exit 1; }

{
  echo "=== $(date '+%F %T') restart-deps start (cwd=$SCRIPT_DIR) ==="

  if ! command -v docker >/dev/null 2>&1; then
    echo "docker 未安装/不在 PATH，跳过容器重启（app 端配置仍已生效）。"
    echo "=== done (skipped: no docker) ==="
    exit 0
  fi

  ENV_FILE="../.env"
  if [ ! -f "$ENV_FILE" ]; then
    echo "未找到 $ENV_FILE，使用 compose 默认。"
  fi

  # 优先 docker compose（v2 plugin），回退 docker-compose（v1）
  if docker compose version >/dev/null 2>&1; then
    DC=(docker compose --env-file "$ENV_FILE")
  elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose --env-file "$ENV_FILE")
  else
    echo "未找到 docker compose / docker-compose，跳过。"
    echo "=== done (skipped: no compose) ==="
    exit 0
  fi

  if [ "${RESTART_DEPS_DRY:-0}" = "1" ]; then
    echo "DRY RUN（RESTART_DEPS_DRY=1）：跳过容器重建（${DC[*]} up -d --force-recreate qdrant redis memos-api）。"
    echo "=== $(date '+%F %T') restart-deps done (dry run) ==="
    exit 0
  fi

  echo "执行: ${DC[*]} up -d --force-recreate qdrant redis memos-api"
  "${DC[@]}" up -d --force-recreate qdrant redis memos-api 2>&1
  rc=$?
  echo "exit=$rc"
  echo "=== $(date '+%F %T') restart-deps done ==="
} >> "$LOG" 2>&1
