"""T-W10-D-a: report_versions 表 CRUD — 记录报告生成/重生成版本.

P022: 纯存储层 — CRUD 函数仅做 SQL 操作返回 ReportVersion dataclass，
零 LLM import，零格式化逻辑。DDL 在 database.py 中。

P050: Fabrication-Proof CRUD — frozenset _ALLOWED_FIELDS 校验字段，
parameterized SQL 防注入。

Usage:
    import aiosqlite
    from z_winnow.pipeline.report_version import create_version, get_latest_version

    async with aiosqlite.connect("data/winnow.db") as db:
        vid = await create_version(db, "rpt-001", "grp-1", "20260516", "...", "daily_run", 12.5)
        latest = await get_latest_version(db, "rpt-001")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

# ============================================================
# P050: Fabrication-Proof CRUD — frozenset whitelist
# ============================================================

_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "version_id",
        "report_id",
        "group_id",
        "date",
        "version_number",
        "content",
        "content_changed",
        "source",
        "build_duration_s",
        "is_active",
        "created_at",
    }
)


# ============================================================
# ReportVersion dataclass
# ============================================================


@dataclass(frozen=True)
class ReportVersion:
    """report_versions 表的不可变数据对象.

    Attributes:
        version_id: "{report_id}-v{n}" 格式的主键
        report_id: 报告 ID
        group_id: 群组 ID
        date: 日期 YYYYMMDD
        version_number: 同一 report_id 内自增版本号 (1, 2, 3...)
        content: 完整报告 markdown
        content_changed: 相比上一版有变更
        source: "daily_run" | "regenerate" | "manual"
        build_duration_s: 构建耗时 (秒)，可为 None
        created_at: ISO8601 创建时间
    """

    version_id: str
    report_id: str
    group_id: str
    date: str
    version_number: int
    content: str | None  # P022: NULL until stage H export writes rendered Markdown
    content_changed: bool = False
    source: str = "daily_run"
    build_duration_s: float | None = None
    is_active: bool = True  # M4: 当前生效版本（回滚=重指）；每 report 仅一行 True
    created_at: str = ""


def _row_to_report_version(row: aiosqlite.Row) -> ReportVersion:
    """将 aiosqlite.Row 转换为 ReportVersion dataclass."""
    return ReportVersion(
        version_id=row["version_id"],
        report_id=row["report_id"],
        group_id=row["group_id"],
        date=row["date"],
        version_number=row["version_number"],
        content=row["content"],
        content_changed=bool(row["content_changed"]),
        source=row["source"],
        build_duration_s=row["build_duration_s"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


# ============================================================
# CRUD Functions
# ============================================================


async def peek_next_version_number(
    db: aiosqlite.Connection,
    report_id: str,
) -> int:
    """读取下一个版本号（MAX(version_number)+1），不写库.

    M4: 供 output_composer 在写版本化 L3 目录 ``v{n}/`` 前预知版本号，
    保证目录名与随后 create_version 写的 DB 行一致。

    Args:
        db: aiosqlite 数据库连接
        report_id: 报告 ID

    Returns:
        下一个版本号（无历史版本时返回 1）
    """
    cursor = await db.execute(
        "SELECT COALESCE(MAX(version_number), 0) FROM report_versions WHERE report_id = ?",
        (report_id,),
    )
    row = await cursor.fetchone()
    if row is None or row[0] is None:
        return 1
    return int(row[0]) + 1


async def create_version(
    db: aiosqlite.Connection,
    report_id: str,
    group_id: str,
    date: str,
    content: str | None,  # P022: NULL until stage H export
    source: str = "daily_run",
    build_duration_s: float | None = None,
    content_changed: bool = False,
    version_number: int | None = None,
) -> str:
    """创建新的报告版本记录，version_number 自动递增（或用显式值）.

    P050: 使用参数化 SQL + frozenset _ALLOWED_FIELDS 校验字段名.

    P022: content=NULL in Phase E (main flow), content=markdown in Phase H (export).

    M4: ``version_number`` 可显式传入——当调用方需在写 L3 版本化目录
    (``v{n}/``) 前就知道版本号时，先 ``peek_next_version_number`` 取号，
    再把同一号码传给本函数与目录写入，保证目录名与 DB 行一致。
    传 None 则内部自增（旧行为）。

    Args:
        db: aiosqlite 数据库连接
        report_id: 报告 ID
        group_id: 群组 ID
        date: 日期 YYYYMMDD
        content: 完整报告 markdown (None if not yet rendered)
        source: 来源 ("daily_run" | "regenerate" | "manual")
        build_duration_s: 构建耗时 (秒)，可选
        content_changed: 相比上一版是否有内容变更，默认 False
        version_number: 显式版本号（可选）；None=内部自增

    Returns:
        version_id: "{report_id}-v{n}" 格式的版本标识符

    Raises:
        ValueError: report_id 为空时
        aiosqlite.Error: SQL 执行失败时
    """
    if not report_id or not report_id.strip():
        raise ValueError("report_id must be non-empty")

    # P050: 参数化 SQL — 使用 ? 占位符防注入
    # 计算自增 version_number（除非调用方显式传入）
    if version_number is None:
        version_number = await peek_next_version_number(db, report_id)

    version_id = f"{report_id}-v{version_number}"

    # M4: 新版本成为 active；先把同 report 的旧 active 版本降级（每 report 仅一行 active）
    await db.execute(
        "UPDATE report_versions SET is_active = 0 WHERE report_id = ? AND is_active = 1",
        (report_id,),
    )
    await db.execute(
        """INSERT INTO report_versions
           (version_id, report_id, group_id, date, version_number, content,
            content_changed, source, build_duration_s, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            version_id,
            report_id,
            group_id,
            date,
            version_number,
            content,
            1 if content_changed else 0,
            source,
            build_duration_s,
        ),
    )
    await db.commit()

    logger.debug(
        "Created report version: %s (v%d, source=%s)",
        version_id,
        version_number,
        source,
    )
    return version_id


async def get_latest_version(
    db: aiosqlite.Connection,
    report_id: str,
) -> ReportVersion | None:
    """获取指定 report_id 的最新版本（版本号最大）.

    P050: 参数化 SQL，仅查询白名单字段.

    Args:
        db: aiosqlite 数据库连接
        report_id: 报告 ID

    Returns:
        ReportVersion 或 None（报告无任何版本时）
    """
    # A008: 显式初始化
    result: ReportVersion | None = None

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """SELECT version_id, report_id, group_id, date, version_number,
                      content, content_changed, source, build_duration_s, is_active, created_at
               FROM report_versions
               WHERE report_id = ?
               ORDER BY version_number DESC
               LIMIT 1""",
            (report_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            result = _row_to_report_version(row)
    except aiosqlite.Error:
        logger.exception("Failed to get latest version for report_id=%s", report_id)
        result = None
    finally:
        db.row_factory = original_factory

    return result


async def get_active_version(
    db: aiosqlite.Connection,
    report_id: str,
) -> ReportVersion | None:
    """获取指定 report_id 的当前生效版本（is_active=1）.

    M4: 与 get_latest_version 区分——latest 是版本号最大（最新产出），
    active 是当前生效（回滚后可能不是最新）。Web 导出/飞书推送/前端"当前报告"
    应读 active 而非 latest。

    Args:
        db: aiosqlite 数据库连接
        report_id: 报告 ID

    Returns:
        ReportVersion 或 None（无 active 版本时）
    """
    # A008: 显式初始化
    result: ReportVersion | None = None

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """SELECT version_id, report_id, group_id, date, version_number,
                      content, content_changed, source, build_duration_s, is_active, created_at
               FROM report_versions
               WHERE report_id = ? AND is_active = 1
               LIMIT 1""",
            (report_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            result = _row_to_report_version(row)
    except aiosqlite.Error:
        logger.exception("Failed to get active version for report_id=%s", report_id)
        result = None
    finally:
        db.row_factory = original_factory

    return result


async def set_active_version(
    db: aiosqlite.Connection,
    version_id: str,
) -> str | None:
    """把指定版本设为当前生效（回滚主操作）。

    M4: 同 report_id 下，目标 version_id 置 is_active=1，其余全部置 0。
    幂等。返回受影响 report_id（版本不存在返回 None）。

    Args:
        db: aiosqlite 数据库连接
        version_id: 目标版本 ID "{report_id}-v{n}"

    Returns:
        受影响的 report_id，或 None（版本不存在）
    """
    # A008: 显式初始化
    report_id: str | None = None
    try:
        cursor = await db.execute(
            "SELECT report_id FROM report_versions WHERE version_id = ?",
            (version_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        report_id = row[0]

        await db.execute(
            "UPDATE report_versions SET is_active = 0 WHERE report_id = ?",
            (report_id,),
        )
        await db.execute(
            "UPDATE report_versions SET is_active = 1 WHERE version_id = ?",
            (version_id,),
        )
        await db.commit()
        logger.info("set_active_version: %s now active for report_id=%s", version_id, report_id)
    except aiosqlite.Error:
        logger.exception("Failed to set active version_id=%s", version_id)
        report_id = None
    return report_id


async def list_versions(
    db: aiosqlite.Connection,
    report_id: str,
) -> list[ReportVersion]:
    """列出指定 report_id 的全部版本，按 version_number 升序.

    P050: 参数化 SQL.

    Args:
        db: aiosqlite 数据库连接
        report_id: 报告 ID

    Returns:
        ReportVersion 列表（无版本时返回空列表）
    """
    # A008: 显式初始化
    results: list[ReportVersion] = []

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """SELECT version_id, report_id, group_id, date, version_number,
                      content, content_changed, source, build_duration_s, is_active, created_at
               FROM report_versions
               WHERE report_id = ?
               ORDER BY version_number ASC""",
            (report_id,),
        )
        rows = await cursor.fetchall()
        results = [_row_to_report_version(row) for row in rows]
    except aiosqlite.Error:
        logger.exception("Failed to list versions for report_id=%s", report_id)
        results = []
    finally:
        db.row_factory = original_factory

    return results


async def get_version(
    db: aiosqlite.Connection,
    version_id: str,
) -> ReportVersion | None:
    """按 version_id 精确查找单个版本.

    P050: 参数化 SQL.

    Args:
        db: aiosqlite 数据库连接
        version_id: 版本 ID，格式 "{report_id}-v{n}"

    Returns:
        ReportVersion 或 None（版本不存在时）
    """
    # A008: 显式初始化
    result: ReportVersion | None = None

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """SELECT version_id, report_id, group_id, date, version_number,
                      content, content_changed, source, build_duration_s, is_active, created_at
               FROM report_versions
               WHERE version_id = ?""",
            (version_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            result = _row_to_report_version(row)
    except aiosqlite.Error:
        logger.exception("Failed to get version_id=%s", version_id)
        result = None
    finally:
        db.row_factory = original_factory

    return result


async def find_versions(
    db: aiosqlite.Connection,
    *,
    date: str | None = None,
    group_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = None,
) -> list[ReportVersion]:
    """按日期/群组查询报告版本列表，支持日期范围和数量限制.

    P050: 参数化 SQL + frozenset _ALLOWED_FIELDS 校验字段名.
    P009: 所有参数 default=None，可选透传.

    Args:
        db: aiosqlite 数据库连接
        date: 精确日期 YYYYMMDD (可选)
        group_id: 群组 ID (可选)
        from_date: 日期范围起始 YYYYMMDD (可选)
        to_date: 日期范围结束 YYYYMMDD (可选)
        limit: 最大返回数量 (可选, None=不限制).

    Returns:
        ReportVersion 列表，按 date DESC + version_number DESC 排序.
        无匹配时返回空列表.
    """
    # A008: 显式初始化
    results: list[ReportVersion] = []

    clauses: list[str] = []
    params: list[str | int] = []

    if date is not None:
        clauses.append("date = ?")
        params.append(date)
    else:
        # 日期范围 (仅在没有精确 date 时生效)
        if from_date is not None:
            clauses.append("date >= ?")
            params.append(from_date)
        if to_date is not None:
            clauses.append("date <= ?")
            params.append(to_date)

    if group_id is not None:
        clauses.append("group_id = ?")
        params.append(group_id)

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)

    sql = f"""SELECT version_id, report_id, group_id, date, version_number,
                     content, content_changed, source, build_duration_s, is_active, created_at
              FROM report_versions
              {where_sql}
              ORDER BY date DESC, version_number DESC
              {limit_sql}"""

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        results = [_row_to_report_version(row) for row in rows]
    except aiosqlite.Error:
        logger.exception("Failed to find versions: date=%s group=%s", date, group_id)
        results = []
    finally:
        db.row_factory = original_factory

    return results


async def delete_report(
    db: aiosqlite.Connection,
    report_id: str,
) -> int:
    """Delete every version of a report (whole report_id).

    report_id == ``{group_id}-{date}``，故本函数等价于删除某群某天的整份报告
    （含其全部历史版本）。仅做 SQL 删除，不动 L3 磁盘文件——磁盘清理由上层
    service 负责（需要 group_id/date 解析 + 路径安全校验）。

    P022: 纯存储层 — 单条参数化 DELETE，零副作用。
    P050: 参数化 SQL 防注入。

    Args:
        db: aiosqlite 数据库连接
        report_id: 报告 ID（``{group_id}-{date}``）

    Returns:
        实际删除的版本行数（0 表示该 report_id 不存在）。
    """
    # A008: 显式初始化
    deleted: int = 0
    try:
        cursor = await db.execute(
            "DELETE FROM report_versions WHERE report_id = ?",
            (report_id,),
        )
        await db.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info("delete_report: removed %d version(s) for report_id=%s", deleted, report_id)
    except aiosqlite.Error:
        logger.exception("Failed to delete report_id=%s", report_id)
        deleted = 0
    return deleted


# ============================================================
# T-W12-10: update_version_content — Phase H export writes content
# ============================================================


async def update_version_content(
    db: aiosqlite.Connection,
    version_id: str,
    content: str,
) -> bool:
    """Update the content of an existing report version (Phase H export).

    P022: Storage/Formatting Layer Separation — Phase E creates version
    with content=NULL, Phase H (export_markdown) updates content with
    rendered Markdown.

    Args:
        db: aiosqlite database connection.
        version_id: Version ID to update, format "{report_id}-v{n}".
        content: Rendered Markdown content to store.

    Returns:
        True if a row was updated, False if version_id not found.
    """
    try:
        cursor = await db.execute(
            "UPDATE report_versions SET content = ? WHERE version_id = ?",
            (content, version_id),
        )
        await db.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(
                "update_version_content: updated %s (%d chars)",
                version_id,
                len(content),
            )
        else:
            logger.warning(
                "update_version_content: version_id=%s not found",
                version_id,
            )
        return bool(updated)

    except aiosqlite.Error:
        logger.exception("Failed to update content for version_id=%s", version_id)
        return False
