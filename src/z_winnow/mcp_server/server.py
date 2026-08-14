"""FastMCP server exposing L3 knowledge layer + feedback Inbox.

Architecture role (docs/mcp-platform-checkpoint.md §3.1, §4.1):
- **Read tools** query L3 (topic_summaries / report_versions / L3 JSON) only.
  MemOS is NOT exposed — semantic vector recall stays internal to the pipeline.
  Scene A (fuzzy retrieval) uses LIKE over topic_summaries text fields.
- **Write tool** (submit_feedback) appends to feedback_events Inbox; no immediate
  processing — feedback waits for the local maintenance cycle to consume.

DB access: single process-wide aiosqlite connection (lazy), WAL mode, schema
auto-initialized on first use (migrations are idempotent).
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from z_winnow.mcp_server.feedback_schema import (
    FeedbackSignal,
    validate_feedback_payload,
)
from z_winnow.mcp_server.mcp_keys import MemberInfo, resolve_member

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("winnow")


# ============================================================
# API key 鉴权 + 成员身份注入（contextvars；http 校验 key，stdio 本地信任）
# ============================================================


# 当前调用者身份（middleware set / tool get）。stdio 无 header → admin 兜底。
_current_member: contextvars.ContextVar[MemberInfo | None] = contextvars.ContextVar(
    "winnow_current_member", default=None
)


def _extract_api_key(headers: dict[str, str]) -> str:
    """从 header 提取 API key（``x-api-key`` 优先，其次 ``Authorization: Bearer``）。"""
    api_key = headers.get("x-api-key") or ""
    if not api_key:
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            api_key = auth[7:].strip()
    return api_key


class _ApiKeyAuth(Middleware):
    """校验 API key 并注入当前调用者身份（``MemberInfo``）到 contextvar。

    - **http transport**：提取 key → :func:`resolve_member` 查 YAML → set contextvar。
      key 未注册 → ``ToolError`` 拒绝。
    - **stdio 本地**：无 HTTP header。ECS 模式拒绝裸连；local 模式 admin 兜底
      （开发者全权）。
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[no-untyped-def]
        headers = get_http_headers() or {}
        if not headers:
            # 无 HTTP 上下文 = stdio 本地集成
            if _is_ecs():
                raise ToolError("API key required (x-api-key or Authorization: Bearer)")
            # local stdio → admin 兜底（开发者全权）
            token = _current_member.set(
                MemberInfo("local", "本地", is_admin=True, allowed_groups=set())
            )
            try:
                return await call_next(context)
            finally:
                _current_member.reset(token)

        api_key = _extract_api_key(headers)
        if not api_key:
            raise ToolError("API key required (x-api-key or Authorization: Bearer)")
        from z_winnow.config.settings import get_settings

        try:
            member = resolve_member(api_key, get_settings().mcp_keys_path)
        except KeyError:
            raise ToolError("Invalid or unknown API key") from None
        token = _current_member.set(member)
        try:
            return await call_next(context)
        finally:
            _current_member.reset(token)


# 始终注册 middleware。YAML 无 key 时 http 调用 resolve_member 报 KeyError → 拒绝；
# stdio 本地仍 admin 兜底可用。
mcp.add_middleware(_ApiKeyAuth())
logger.info("MCP auth middleware registered (key→member via mcp_keys.yaml)")


def _get_current_member() -> MemberInfo:
    """当前调用者身份。

    middleware 未注入（测试直接 await 工具 / 异常路径）→ admin 兜底（全权）。
    """
    m = _current_member.get()
    if m is None:
        return MemberInfo("local", "本地", is_admin=True, allowed_groups=set())
    return m


def _check_group_access(group_id: str) -> None:
    """校验当前调用者是否有权访问 group_id，无权 raise ``ToolError``。"""
    member = _get_current_member()
    if not member.can_access(group_id):
        raise ToolError(f"无权访问群组 {group_id}（成员 {member.member_id or 'unknown'} 未授权）")


# ============================================================
# DB connection — process-wide singleton (lazy)
# ============================================================

_db_conn: aiosqlite.Connection | None = None

# ECS 双库模式（阶段 2.3）：l3 只读快照 + feedback 读写 inbox。
# 仅当 settings.deployment_target == "ecs" 时启用；本地模式三者都回退到 get_db()。
_l3_conn: aiosqlite.Connection | None = None
_inbox_conn: aiosqlite.Connection | None = None
_l3_mtime: float = 0.0  # 上次打开 l3_snapshot 的文件 mtime；push 后文件被原子替换 → 懒重连


async def get_db() -> aiosqlite.Connection:
    """Return the process-wide aiosqlite connection, initializing schema on first use.

    MCP server is single-process; a single connection (WAL + busy_timeout) is
    sufficient and matches the rest of the codebase (web app uses the same pattern).
    """
    global _db_conn
    if _db_conn is None:
        from z_winnow.config.settings import get_settings
        from z_winnow.pipeline.database import init_database_in_conn

        settings = get_settings()
        db_path = Path(settings.sqlite_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db_conn = await aiosqlite.connect(str(db_path))
        _db_conn.row_factory = aiosqlite.Row
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA busy_timeout=5000")
        # Idempotent — safe on every cold start (stdio spawns a fresh process).
        await init_database_in_conn(_db_conn)
        logger.info("MCP db connection initialized: %s", db_path)
    return _db_conn


def _is_ecs() -> bool:
    """是否启用 ECS 双库路由（``deployment_target == "ecs"``）。"""
    from z_winnow.config.settings import get_settings

    return get_settings().deployment_target == "ecs"


async def get_l3_db() -> aiosqlite.Connection:
    """读工具用的 L3 连接（阶段 2.3 双库路由）。

    - **ECS 模式**：``l3_snapshot.db`` 以 ``mode=ro&immutable=1`` 打开（只读，
      杜绝 ECS 误写主库快照）；sync push 时文件被原子替换 → 靠 mtime 检测懒重连，
      零中断（无需重启容器）。
    - **本地模式**：回退到 :func:`get_db`（单库，stdio / 本地集成）。
    """
    if not _is_ecs():
        return await get_db()
    global _l3_conn, _l3_mtime
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    l3_path = Path(settings.l3_snapshot_path)
    if not l3_path.exists():
        raise ToolError(
            f"L3 snapshot not found at {l3_path} — run 'winnow sync push' "
            "from local first to populate the ECS read replica."
        )
    mtime = l3_path.stat().st_mtime
    if _l3_conn is None or mtime != _l3_mtime:
        if _l3_conn is not None:
            try:
                await _l3_conn.close()
            except Exception:  # 关旧连接失败不阻塞重连（重连优先于清理）
                logger.warning("failed to close stale l3 connection", exc_info=True)
        # immutable=1: 快照由 sync push 原子替换，ECS 端绝不就地写 → 声明不可变，
        # 跳过 journal 检查，ro 打开更快且杜绝任何写意图。
        uri = f"file:{l3_path.resolve()}?mode=ro&immutable=1"
        _l3_conn = await aiosqlite.connect(uri, uri=True)
        _l3_conn.row_factory = aiosqlite.Row
        _l3_mtime = mtime
        logger.info("L3 snapshot connection (re)opened: %s (mtime=%s)", l3_path, mtime)
    return _l3_conn


async def get_inbox_db() -> aiosqlite.Connection:
    """submit_feedback 用的反馈 Inbox 连接（阶段 2.3 双库路由）。

    - **ECS 模式**：``feedback_inbox.db`` 读写（WAL），首次打开幂等 init
      feedback_events schema；sync pull 拉回本地后清空。
    - **本地模式**：回退到 :func:`get_db`（反馈直接进主库）。
    """
    if not _is_ecs():
        return await get_db()
    global _inbox_conn
    from z_winnow.config.settings import get_settings
    from z_winnow.pipeline.database import init_database_in_conn

    settings = get_settings()
    inbox_path = Path(settings.feedback_inbox_path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if _inbox_conn is None:
        _inbox_conn = await aiosqlite.connect(str(inbox_path))
        _inbox_conn.row_factory = aiosqlite.Row
        await _inbox_conn.execute("PRAGMA journal_mode=WAL")
        await _inbox_conn.execute("PRAGMA busy_timeout=5000")
        await init_database_in_conn(_inbox_conn)  # 幂等建 feedback_events 等表
        logger.info("Feedback inbox connection initialized: %s", inbox_path)
    return _inbox_conn


# ============================================================
# Read tools
# ============================================================


@mcp.tool
async def list_groups() -> list[dict[str, Any]]:
    """列出所有已注册群组。返回 group_id / display_name / chatroom_id。

    其他工具的 group_id 参数请用此工具查到的 group_id（内部 UUID，非群名）。
    """
    db = await get_l3_db()
    member = _get_current_member()
    if member.is_admin:
        cur = await db.execute(
            "SELECT group_id, display_name, chatroom_id "
            "FROM groups WHERE is_active = 1 ORDER BY display_name"
        )
    else:
        if not member.allowed_groups:
            return []
        placeholders = ",".join("?" * len(member.allowed_groups))
        cur = await db.execute(
            f"SELECT group_id, display_name, chatroom_id FROM groups "
            f"WHERE is_active = 1 AND group_id IN ({placeholders}) ORDER BY display_name",
            tuple(sorted(member.allowed_groups)),
        )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


@mcp.tool
async def search_topics(
    query: str,
    group_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """模糊检索议题（场景 A：用户"记得聊过 X"）。

    基于 L3 topic_summaries 的 LIKE 文本匹配，不依赖 MemOS 语义向量。
    检索字段：topic_name / summary_text / conclusion / background / participants。
    SQLite LIKE 默认对 ASCII 大小写不敏感、对中文中性，故中英文术语/人名均可命中。

    Args:
        query: 检索关键词（中英文均可）。
        group_id: 可选，限定群（用 list_groups 查）。
        date_from / date_to: 可选，日期范围 YYYYMMDD。
        limit: 返回上限（默认 20）。

    Returns:
        议题摘要列表，每项含 summary_id（供 get_topic 深入）。
    """
    member = _get_current_member()
    if group_id:
        _check_group_access(group_id)  # 显式 group_id 越权拒绝
    db = await get_l3_db()
    pat = f"%{query}%"
    like_conds = [
        "topic_name LIKE ?",
        "summary_text LIKE ?",
        "conclusion LIKE ?",
        "background LIKE ?",
        "participants LIKE ?",
    ]
    params: list[Any] = [pat] * len(like_conds)
    extra: list[str] = []
    # 非 admin 强制限定 allowed_groups（admin 全权）
    if not member.is_admin:
        if not member.allowed_groups:
            return []
        placeholders = ",".join("?" * len(member.allowed_groups))
        extra.append(f"group_id IN ({placeholders})")
        params.extend(sorted(member.allowed_groups))
    if group_id:
        extra.append("group_id = ?")
        params.append(group_id)
    if date_from:
        extra.append("date >= ?")
        params.append(date_from)
    if date_to:
        extra.append("date <= ?")
        params.append(date_to)

    sql = (
        "SELECT summary_id, group_id, date, topic_name, summary_text, "
        "participants, lifecycle, conclusion "
        "FROM topic_summaries "
        "WHERE (" + " OR ".join(like_conds) + ")"
    )
    if extra:
        sql += " AND " + " AND ".join(extra)
    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    cur = await db.execute(sql, tuple(params))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


@mcp.tool
async def get_topic(summary_id: str) -> dict[str, Any]:
    """获取议题详情 + 同名议题跨天演化时间线 + 相关反馈（场景 B：话题确认/成熟度判断）。

    成熟度信号：
    - detail.participants / detail.conclusion：参与人与是否形成结论
    - timeline：同 group 同 topic_name 在不同日期的记录（讨论持续情况）
    - feedback：该议题已收到的反馈

    Args:
        summary_id: 议题摘要 ID（来自 search_topics）。
    """
    db = await get_l3_db()
    cur = await db.execute("SELECT * FROM topic_summaries WHERE summary_id = ?", (summary_id,))
    row = await cur.fetchone()
    if row is None:
        return {"error": "topic not found", "summary_id": summary_id}
    detail = dict(row)
    _check_group_access(detail.get("group_id", ""))  # 越权拒绝

    # 同 group 同 topic_name 的跨天记录 — 演化时间线
    cur = await db.execute(
        "SELECT date, summary_id, lifecycle, participants FROM topic_summaries "
        "WHERE group_id = ? AND topic_name = ? AND summary_id != ? "
        "ORDER BY date DESC LIMIT 30",
        (detail.get("group_id", ""), detail.get("topic_name", ""), summary_id),
    )
    timeline = [dict(r) for r in await cur.fetchall()]

    # 相关反馈 — 按 target_topic_id 或 target_id 匹配
    cur = await db.execute(
        "SELECT feedback_id, date, signal, severity, corrected_text, "
        "correction_note, status, created_at "
        "FROM feedback_events "
        "WHERE target_topic_id = ? OR target_id = ? "
        "ORDER BY created_at DESC LIMIT 20",
        (summary_id, summary_id),
    )
    feedback = [dict(r) for r in await cur.fetchall()]

    return {"detail": detail, "timeline": timeline, "feedback": feedback}


@mcp.tool
async def get_daily_report(
    group_id: str,
    date: str,
    version: int | None = None,
) -> dict[str, Any]:
    """读取某群某日的日报（场景 C：日报回看）。

    version 省略时取当前生效版本（report_versions.is_active = 1）。
    返回合并后的 L3 内容（overview / topics / resources / trend / highlights 等）。

    Args:
        group_id: 群 ID（用 list_groups 查）。
        date: 日期 YYYYMMDD。
        version: 可选，版本号；省略取生效版本。
    """
    _check_group_access(group_id)
    from z_winnow.web.services import report_service

    db = await get_l3_db()
    version_number = version
    if version_number is None:
        cur = await db.execute(
            "SELECT version_number FROM report_versions "
            "WHERE group_id = ? AND date = ? AND is_active = 1",
            (group_id, date),
        )
        r = await cur.fetchone()
        if r is not None:
            version_number = r["version_number"]

    rc = await report_service.get_report_content(db, group_id, date, version_number=version_number)
    if rc is None:
        return {
            "error": "report not found",
            "group_id": group_id,
            "date": date,
            "version": version_number,
        }
    # 私有桶：合并内容里的 resources 按 cloud_key 生成短期预签名 cloud_url
    _merged_resources = rc.data.get("resources") if isinstance(rc.data, dict) else None
    if isinstance(_merged_resources, list):
        try:
            from z_winnow.object_storage.r2 import presign_resource_urls

            presign_resource_urls(_merged_resources)
        except Exception:
            pass
    return {
        "group_id": rc.group_id,
        "date": rc.date,
        "version": version_number,
        "content": rc.data,
    }


@mcp.tool
async def list_resources(
    group_id: str,
    date: str,
    version: int | None = None,
) -> dict[str, Any]:
    """读取某群某日的资源列表（L3 resources.json）。

    Args:
        group_id: 群 ID。
        date: 日期 YYYYMMDD。
        version: 可选版本号；省略取最新版本目录。

    文件下载：resource 若有 ``cloud_key``，返回里会带短期预签名 ``cloud_url``
    （私有 R2 桶直链，默认 1h 失效），用它下图片/PDF/文件。
    """
    _check_group_access(group_id)
    from z_winnow.web.services import report_service

    db = await get_l3_db()
    rc = await report_service.get_report_content(
        db, group_id, date, report_type="resources", version_number=version
    )
    if rc is None:
        return {"error": "resources not found", "group_id": group_id, "date": date}
    resources = rc.data.get("resources", [])
    # 私有桶：按 cloud_key 生成短期预签名 cloud_url（每次调用新生成，不存盘 → 不怕过期）
    try:
        from z_winnow.object_storage.r2 import presign_resource_urls

        presign_resource_urls(resources)
    except Exception:
        pass
    return {
        "group_id": rc.group_id,
        "date": rc.date,
        "resources": resources,
        "count": rc.data.get("total_count", 0),
    }


# ============================================================
# Write tool — feedback Inbox
# ============================================================


@mcp.tool
async def submit_feedback(
    group_id: str,
    date: str,
    target_type: str,
    signal: str,
    content: str,
    target_id: str | None = None,
    target_version_id: str | None = None,
    target_topic_id: str | None = None,
    original_text: str | None = None,
) -> dict[str, Any]:
    """提交反馈到 feedback_events Inbox（不触发即时处理，等本地周期消费）。

    日期锚点 date 必填 — 用于解决"数据时间错位"和后续版本关联
    (docs/mcp-platform-checkpoint.md §4.1 安全模型)。

    **格式校验**：入参先过 ``feedback_schema.validate_feedback_payload``——不符合
    schema 的请求会 raise ``ToolError``（HTTP 400）**且不会写库**。合法取值：

    - ``signal`` ∈ {correction, supplement, approval, stale, quality}
    - ``target_type`` ∈ {report, trend, highlights, topic, resource, section} ∪
      custom_tables registry 已注册表 id（engineering / world_models / …）
    - ``date`` ∈ YYYYMMDD 或 YYYY-MM-DD（且为真实日历日期）

    reporter 由调用方 API key 绑定的 ``member_id`` 自动确定（见 mcp_keys.yaml），
    调用方无法伪造；群组访问受 key 的 ``allowed_groups`` 约束（admin 全权）。

    Args:
        group_id: 群 ID（须在 key 的 allowed_groups 内，admin 全权）。
        date: 日期锚点，YYYYMMDD 或 YYYY-MM-DD。
        target_type: 反馈对象类型（见上合法取值）。
        signal: 反馈意图（见上合法取值）。
        content: 反馈正文。correction/supplement 时作为"正确/补充文本"存入 corrected_text；
            其他 signal 时作为说明存入 correction_note。
        target_id: 可选，被反馈对象 ID。
        target_version_id: 推荐，被反馈的日报版本 ID（{report_id}-v{n}），精确定位溯源。
        target_topic_id: 推荐，议题级反馈时的议题 ID。
        original_text: 可选，被反馈的原内容（便于后续 regenerate 对照）。

    Returns:
        {feedback_id, accepted}。accepted=False 表示入库失败（查日志）。
        格式非法时不返回而是 raise ToolError。
    """
    _check_group_access(group_id)
    sub = validate_feedback_payload(
        group_id=group_id,
        date=date,
        target_type=target_type,
        signal=signal,
        content=content,
        target_id=target_id,
        target_version_id=target_version_id,
        target_topic_id=target_topic_id,
        original_text=original_text,
    )
    from z_winnow.web.services import feedback_service

    db = await get_inbox_db()
    feedback_id = str(uuid.uuid4())
    is_correction_like = sub.signal in (FeedbackSignal.CORRECTION, FeedbackSignal.SUPPLEMENT)
    reporter = _get_current_member().member_id

    ok = await feedback_service.create_feedback(
        db,
        feedback_id=feedback_id,
        group_id=sub.group_id,
        date=sub.date,
        target_type=sub.target_type,
        signal=sub.signal.value,
        target_id=sub.target_id,
        target_version_id=sub.target_version_id,
        target_topic_id=sub.target_topic_id,
        correction_mode="free_text" if is_correction_like else None,
        original_text=sub.original_text,
        corrected_text=sub.content if is_correction_like else None,
        correction_note=None if is_correction_like else sub.content,
        reporter=reporter,
    )
    return {"feedback_id": feedback_id, "accepted": ok}


# ============================================================
# Public doc routes — 让消费方无需 GitHub 也能取到接入文档
# ============================================================
# GitHub 对部分网络需代理；把消费方文档直接挂在公网 MCP 上：
#   GET /install           → INSTALL.md（自包含接入指南）
#   GET /feedback-format   → feedback-format.md（反馈 payload 权威规格）
# 这些路由是纯文档（无密钥），不走 MCP 鉴权 middleware（on_call_tool 只拦工具调用），
# 任意人可读。文件缺失返回 404，不阻断启动。


def _skill_docs_dir() -> Path:
    """技能文档目录：容器 ``/app/skill``（Dockerfile COPY）；本地开发回退到仓库
    ``.claude/skills/winnow-mcp``。"""
    candidates = [
        Path("/app/skill"),
        Path(__file__).resolve().parents[3] / ".claude" / "skills" / "winnow-mcp",
    ]
    for c in candidates:
        if (c / "INSTALL.md").is_file():
            return c
    return candidates[0]  # 容器默认；文件不在时路由返回 404


def _serve_doc(filename: str) -> Response:
    path = _skill_docs_dir() / filename
    if not path.is_file():
        return PlainTextResponse(f"{filename} not found", status_code=404)
    return PlainTextResponse(
        path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8"
    )


@mcp.custom_route("/install", methods=["GET"])
async def install_doc(_request: Request) -> Response:
    """消费方接入指南（INSTALL.md）—— 公开，无需 key。"""
    return _serve_doc("INSTALL.md")


@mcp.custom_route("/feedback-format", methods=["GET"])
async def feedback_format_doc(_request: Request) -> Response:
    """反馈格式权威规格—— 公开，无需 key。"""
    return _serve_doc("references/feedback-format.md")


# ============================================================
# Entry point
# ============================================================


def run(transport: str = "stdio", host: str = "0.0.0.0", port: int = 8000) -> None:
    """启动 MCP server。

    Args:
        transport: ``stdio``（默认，本地集成 — Claude Desktop / Cursor）或
            ``http``（远程 — ECS 部署，streamable-http）。
            注：FastMCP v3 起 SSE 已废弃，远程统一用 http。
        host: http transport 绑定地址。默认 ``0.0.0.0``（容器/远程可达）；
            本地调试可改 ``127.0.0.1``。FastMCP 默认绑 127.0.0.1，
            容器部署必须显式 0.0.0.0 否则宿主机/反代访问不到。
        port: http transport 监听端口。
    """
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host=host, port=port)
