"""Report service -- report versions, L3 content, and diffs.

Wraps existing ``report_version`` functions into
typed async methods returning Pydantic models.

# P022: Pure data retrieval -- zero LLM calls.
# P014: Every function wraps I/O in try/except with graceful empty returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict

from z_winnow.pipeline import report_version
from z_winnow.web.services import PaginatedResult

# L070: Conditional imports
try:
    from z_winnow.web.schemas.reports import ReportDiffOut, ReportVersionOut
except ImportError:

    class ReportVersionOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class ReportDiffOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)


logger = logging.getLogger(__name__)

# Bound concurrent background Feishu uploads so a large batch back-fill doesn't
# spawn dozens of lark-cli subprocesses / trip Feishu API rate limits at once.
# Lazy-initialized to avoid binding the Semaphore to the wrong event loop across
# tests/processes.
_feishu_push_semaphore: asyncio.Semaphore | None = None


def _get_feishu_push_semaphore() -> asyncio.Semaphore:
    global _feishu_push_semaphore
    if _feishu_push_semaphore is None:
        _feishu_push_semaphore = asyncio.Semaphore(4)
    return _feishu_push_semaphore


class ReportContent(BaseModel):
    """Parsed L3 report JSON content.

    Not in schemas package -- this model wraps the raw JSON dict
    read from L3 ``{group_id}/{date}/daily.json`` files.
    """

    model_config = ConfigDict(from_attributes=True)

    report_type: str
    group_id: str
    date: str
    data: dict[str, Any] = {}


async def list_report_versions(
    db: aiosqlite.Connection,
    *,
    group_id: str | None = None,
    date: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedResult:
    """List report versions with optional filtering and pagination.

    Wraps ``report_version.find_versions`` and adds pagination.

    Args:
        db: aiosqlite database connection.
        group_id: Optional group filter.
        date: Optional date filter (YYYYMMDD).
        page: Page number (1-based).
        page_size: Items per page.

    Returns:
        PaginatedResult of ReportVersionOut items.
    """
    # A008: explicit initialization
    result: PaginatedResult = PaginatedResult(items=[], total=0, page=page, page_size=page_size)
    try:
        all_versions = await report_version.find_versions(
            db,
            date=date,
            group_id=group_id,
        )

        total = len(all_versions)
        offset = (page - 1) * page_size
        page_items = all_versions[offset : offset + page_size]

        items = [
            ReportVersionOut(
                version_id=v.version_id,
                report_id=v.report_id,
                group_id=v.group_id,
                date=v.date,
                version_number=v.version_number,
                content=v.content,
                content_changed=1 if v.content_changed else 0,
                source=v.source,
                build_duration_s=v.build_duration_s,
                is_active=1 if v.is_active else 0,
                created_at=v.created_at,
            )
            for v in page_items
        ]
        result = PaginatedResult(items=items, total=total, page=page, page_size=page_size)
    except Exception:
        # P014: log and return empty result
        logger.exception("list_report_versions failed")
        result = PaginatedResult(items=[], total=0, page=page, page_size=page_size)
    return result


async def get_report_version(
    db: aiosqlite.Connection,
    version_id: str,
) -> ReportVersionOut | None:
    """Get a single report version by version_id.

    Args:
        db: aiosqlite database connection.
        version_id: Version identifier, format ``{report_id}-v{n}``.

    Returns:
        ReportVersionOut or None if not found.
    """
    # A008: explicit initialization
    result: ReportVersionOut | None = None
    try:
        v = await report_version.get_version(db, version_id)
        if v is not None:
            result = ReportVersionOut(
                version_id=v.version_id,
                report_id=v.report_id,
                group_id=v.group_id,
                date=v.date,
                version_number=v.version_number,
                content=v.content,
                content_changed=1 if v.content_changed else 0,
                source=v.source,
                build_duration_s=v.build_duration_s,
                is_active=1 if v.is_active else 0,
                created_at=v.created_at,
            )
    except Exception:
        logger.exception("get_report_version failed for version_id=%s", version_id)
        result = None
    return result


async def get_report_content(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
    *,
    report_type: str = "daily",
    output_dir: str | None = None,
    created_at: str | None = None,
    version_number: int | None = None,
) -> ReportContent | None:
    """Read L3 report JSON content from disk.

    Reads ``{output_dir}/{group_id}/{date}/{report_type}.json`` and
    returns parsed content as a ReportContent model.

    When ``report_type="daily"`` (the default), the content is **merged**
    from the L3 JSON files so the frontend receives a single payload:
    ``daily.json`` as the base, with ``resources`` merged from
    ``resources.json``, ``topics`` from ``topics.json``, and ``custom_tables``
    (one entry per enabled table — engineering / world_models / …) merged from
    each ``{kind}.json``. ``generation_time`` comes from the report version record.

    Args:
        db: aiosqlite database connection (used for fallback queries).
        group_id: Group identifier.
        date: Date string YYYYMMDD.
        report_type: Report file type (``daily``, ``resources``, ``engineering``, ``topics``).
        output_dir: L3 output root directory. If None, reads from Settings.layer3_output_dir.
        created_at: ISO-8601 timestamp from the report version record, used as
            ``generation_time`` in the merged daily response.

    Returns:
        ReportContent or None if file not found / parse error.
    """
    # A008: explicit initialization
    result: ReportContent | None = None
    try:
        if output_dir is None:
            from z_winnow.config.settings import get_settings

            output_dir = get_settings().layer3_output_dir

        # M4: 版本化目录——优先 v{version_number}/，回退最新 v{n}/，再回退扁平（旧数据）。
        from z_winnow.pipeline.l3_paths import resolve_l3_dir

        base_dir = resolve_l3_dir(output_dir, group_id, date, version_number=version_number)
        file_path = base_dir / f"{report_type}.json"
        if not file_path.exists():
            return None

        raw = file_path.read_text(encoding="utf-8")
        if not raw.strip():
            return None

        data = json.loads(raw)
        if not isinstance(data, dict):
            return None

        # ── merge other L3 files when serving daily ──────────────
        if report_type == "daily":
            # resources.json → data.resources
            _merge_json_field(base_dir, "resources.json", data, "resources", "resources")

            # custom_tables → data.custom_tables (generic, one entry per enabled table)
            # engineering.json / world_models.json / … are attached under
            # data["custom_tables"][<kind>] only when that table is enabled for the
            # group. A disabled table is never surfaced (legacy on-disk file ignored).
            # model_used is backfilled from whichever table file carries it.
            from z_winnow.custom_tables import registry as ct_registry

            ct_out: dict[str, Any] = {}
            _have_model_used = "model_used" in data
            for tdef in ct_registry.get_all_tables():
                if not await _custom_table_enabled_for_group(db, group_id, tdef.id):
                    continue
                tbl = _read_json_file(base_dir / f"{tdef.id}.json")
                if isinstance(tbl, dict):
                    ct_out[tdef.id] = tbl
                    if not _have_model_used and "model_used" in tbl:
                        data["model_used"] = tbl["model_used"]
                        _have_model_used = True
            data["custom_tables"] = ct_out

            # topics.json → data.topics (fallback: daily.json 自带 topics 时优先用)
            _merge_json_field(base_dir, "topics.json", data, "topics", "topics")

            # active_members — 汇总所有议题的 participants 去重
            active_set: set[str] = set()
            for t in data.get("topics", []) or []:
                if isinstance(t, dict):
                    for p in t.get("participants") or []:
                        if isinstance(p, str) and p.strip():
                            active_set.add(p.strip())
            data["active_members"] = sorted(active_set)

            # generation_time from report version record
            if created_at:
                data["generation_time"] = created_at

            # cover_generated / judge_result / feishu_pushed_at from report_versions
            try:
                cur = await db.execute(
                    "SELECT cover_generated, judge_result, feishu_pushed_at "
                    "FROM report_versions "
                    "WHERE group_id = ? AND date = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (group_id, date),
                )
                rv_row = await cur.fetchone()
                if rv_row:
                    data["cover_generated"] = bool(rv_row[0])
                    if rv_row[1]:
                        try:
                            data["judge_result"] = json.loads(rv_row[1])
                        except Exception:
                            data["judge_result"] = None
                    data["feishu_pushed_at"] = rv_row[2] or None
            except Exception:
                logger.debug(
                    "get_report_content: failed to read cover/judge/feishu from report_versions"
                )

        result = ReportContent(
            report_type=report_type,
            group_id=group_id,
            date=date,
            data=data,
        )
    except (json.JSONDecodeError, OSError):
        # P014: graceful return None on parse/read failure
        logger.debug(
            "get_report_content: failed to read %s/%s/%s.json", group_id, date, report_type
        )
        result = None
    except Exception:
        logger.exception("get_report_content failed")
        result = None
    return result


def _djb2_base36(s: str) -> str:
    """复刻前端 shortHash（djb2 + base36），供 topic/highlights 的 target_id 匹配。"""
    h = 5381
    for ch in str(s or ""):
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if h == 0:
        return "0"
    out = ""
    while h > 0:
        out = chars[h % 36] + out
        h //= 36
    return out


async def resolve_original_text_for_feedback(
    db: aiosqlite.Connection,
    target_type: str | None,
    target_id: str | None,
    target_version_id: str | None,
) -> str | None:
    """M4: 读取被反馈目标在 target_version 的原始内容，填入 feedback.original_text。

    让 prompt <correction_example><original> 不再为空——LLM 能看到被纠正的原内容
    （议题结论/资源/概要等），配合 <should_be>(评论) 形成「原内容→应改为」对照。
    匹配失败返回 None（不阻断反馈提交）。
    """
    if not target_version_id or not target_type:
        return None
    try:
        v = await report_version.get_version(db, target_version_id)
        if v is None:
            return None
        from z_winnow.config.settings import get_settings
        from z_winnow.pipeline.l3_paths import read_l3_json

        root = get_settings().layer3_output_dir
        vn = v.version_number
        gid, dt = v.group_id, v.date
        tid = str(target_id or "")
        daily = read_l3_json(root, gid, dt, "daily", version_number=vn) or {}

        if target_type == "report":
            return (daily.get("overview") or "")[:600] or None
        if target_type == "trend":
            return (daily.get("trend_analysis") or "")[:600] or None
        if target_type == "highlights":
            for h in daily.get("highlights", []) or []:
                if tid == "hl_" + _djb2_base36(h):
                    return str(h)[:600]
            return None
        if target_type == "topic":
            topics = read_l3_json(root, gid, dt, "topics", version_number=vn) or {}
            for t in topics.get("topics", []) or []:
                kid = (t.get("topic_id") or "") or ("topic_" + _djb2_base36(t.get("topic_name", "")))
                if kid == tid:
                    parts = [t.get("conclusion"), t.get("background")]
                    return " / ".join(p for p in parts if p)[:600] or None
            return None
        if target_type == "resource":
            res = read_l3_json(root, gid, dt, "resources", version_number=vn) or {}
            for r in res.get("resources", []) or []:
                if (r.get("resource_title") or "") == tid:
                    return f"{r.get('resource_title','')} — {r.get('summary','')}"[:600] or None
            return None
        # 自定义表（engineering/world_models/...）：按 {kind}_id 匹配
        tbl = read_l3_json(root, gid, dt, target_type, version_number=vn) or {}
        from z_winnow.custom_tables import registry as _ct_reg

        tdef = _ct_reg.get_table(target_type)
        rkey = tdef.records_key if tdef else "items"
        for rec in tbl.get(rkey, []) or []:
            if rec.get(f"{target_type}_id") == tid:
                desc = rec.get("description") or rec.get("topic") or rec.get("conclusion")
                return str(desc)[:600] if desc else None
        return None
    except Exception:
        logger.debug("resolve_original_text_for_feedback failed", exc_info=True)
        return None


def _read_json_file(filepath: Path) -> dict[str, Any] | None:
    """Read and parse a JSON file; return None on any failure."""
    try:
        if not filepath.exists():
            return None
        raw = filepath.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _merge_json_field(
    base_dir: Path,
    filename: str,
    target: dict[str, Any],
    source_key: str,
    target_key: str | None = None,
) -> None:
    """Merge a single field from a sibling L3 JSON file into *target* in-place.

    Reads ``base_dir / filename`` and copies ``source_key`` into ``target``
    under ``target_key`` (defaults to ``source_key``).  Graceful no-op when
    the file is missing or unparseable.
    """
    data = _read_json_file(base_dir / filename)
    if isinstance(data, dict) and source_key in data:
        target[target_key or source_key] = data[source_key]


async def _custom_table_enabled_for_group(
    db: aiosqlite.Connection, group_id: str, kind: str
) -> bool:
    """Resolve whether a custom table ``kind`` is enabled for a group.

    Uses the generic resolver ``kind_enabled_for_report`` (custom_tables >
    feishu_tables > deprecated engineering_enabled column [engineering only] >
    default off). Defaults to True for ``engineering`` on lookup failure (so a
    transient DB error never hides legacy engineering data), False otherwise.
    """
    try:
        from z_winnow.pipeline.feishu import schema as feishu_schema

        cur = await db.execute(
            "SELECT feishu_tables, custom_tables, engineering_enabled "
            "FROM groups WHERE group_id = ?",
            (group_id,),
        )
        row = await cur.fetchone()
        if not row:
            return kind == "engineering"

        def _blob(raw: object) -> dict[str, Any] | None:
            if not raw:
                return None
            try:
                parsed = json.loads(raw)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        legacy = row[2] if kind == "engineering" else False
        return feishu_schema.kind_enabled_for_report(kind, _blob(row[1]), _blob(row[0]), legacy)
    except Exception:
        logger.debug("_custom_table_enabled_for_group: lookup failed for %s/%s", kind, group_id)
        return kind == "engineering"


async def get_report_diff(
    db: aiosqlite.Connection,
    report_id: str,
) -> ReportDiffOut | None:
    """Get diff between the two latest versions of a report.

    Args:
        db: aiosqlite database connection.
        report_id: Report identifier.

    Returns:
        ReportDiffOut or None if fewer than 2 versions exist.
    """
    # A008: explicit initialization
    result: ReportDiffOut | None = None
    try:
        versions = await report_version.list_versions(db, report_id)
        if len(versions) < 2:
            return None

        # list_versions returns ascending by version_number
        old = versions[-2]
        new = versions[-1]

        content_changed = old.content != new.content if (old.content and new.content) else False

        result = ReportDiffOut(
            report_id=report_id,
            group_id=new.group_id,
            date=new.date,
            old_version=old.version_number,
            new_version=new.version_number,
            old_content=old.content,
            new_content=new.content,
            content_changed=content_changed,
        )
    except Exception:
        logger.exception("get_report_diff failed for report_id=%s", report_id)
        result = None
    return result


async def get_report_versions(
    db: aiosqlite.Connection,
    report_id: str,
) -> list[ReportVersionOut]:
    """List all versions for a specific report_id, ordered by version_number ASC.

    Wraps ``report_version.list_versions`` which queries the report_versions table.

    # P022: Pure data retrieval — zero LLM calls.
    # A008: Explicit initialization before try block.

    Args:
        db: aiosqlite database connection.
        report_id: Report identifier.

    Returns:
        List of ReportVersionOut, empty list if none found or on error.
    """
    # A008: explicit initialization
    versions: list[ReportVersionOut] = []
    try:
        rows = await report_version.list_versions(db, report_id)
        versions = [
            ReportVersionOut(
                version_id=v.version_id,
                report_id=v.report_id,
                group_id=v.group_id,
                date=v.date,
                version_number=v.version_number,
                content=v.content,
                content_changed=1 if v.content_changed else 0,
                source=v.source,
                build_duration_s=v.build_duration_s,
                is_active=1 if v.is_active else 0,
                created_at=v.created_at,
            )
            for v in rows
        ]
    except Exception:
        # P014: log and return empty list
        logger.exception("get_report_versions failed for report_id=%s", report_id)
        versions = []
    return versions


async def _feishu_delete_only_coro(
    report_id: str,
    group_id: str,
    date: str,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Background coroutine: delete existing Feishu records for this date only.

    Shares the group config resolution logic with ``_feishu_push_coro`` (the first
    half — load group → ensure framework → resolve base_token/tables_config) but
    only runs the delete step, skipping the upload.
    """
    import aiosqlite

    from z_winnow.config.settings import get_settings
    from z_winnow.pipeline.feishu import uploader as feishu_uploader
    from z_winnow.web.services.group_service import (
        get_group_detail,
        tables_config_from_group,
    )

    settings = get_settings()
    resolved_db = db_path or settings.db_path

    async with aiosqlite.connect(resolved_db) as db:
        group = await get_group_detail(db, group_id)
        if not group:
            return {"status": "failed", "reason": f"group {group_id} not found"}
        if not group.feishu_enabled:
            return {"status": "skipped", "reason": "feishu not enabled for group"}

        base_token = group.feishu_base_token or ""
        tables_config = tables_config_from_group(group)

        errors = await feishu_uploader.delete_existing_records_for_date(
            base_token=base_token,
            tables_config=tables_config,
            date=date,
        )
        if errors:
            return {"status": "partial", "errors": errors}
        return {"status": "deleted", "date": date, "group_id": group_id}


async def _feishu_push_coro(
    report_id: str,
    group_id: str,
    date: str,
    doc_title: str | None = None,
    overwrite: bool = True,
    db_path: str | None = None,
    version_number: int | None = None,
) -> dict[str, Any]:
    """Background coroutine: read L3 JSON, render Feishu template, upload.

    Uploads via ``pipeline.feishu.uploader`` (lark-cli, user identity), triggered
    from the web API (P067 async task pattern).

    Args:
        report_id: Report identifier.
        group_id: Group identifier for L3 path lookup.
        date: Date string YYYYMMDD for L3 path lookup.
        doc_title: Optional custom document title.
        overwrite: If True, delete existing records for the same date before uploading.
        db_path: SQLite path for config lookup.

    Returns:
        Dict with upload result: {"status": "uploaded", "rows_count": N}
        or {"status": "skipped"/"failed", "reason": "..."}
    """
    import aiosqlite

    from z_winnow.config.settings import get_settings
    from z_winnow.pipeline.feishu import schema as feishu_schema
    from z_winnow.pipeline.feishu import uploader as feishu_uploader
    from z_winnow.web.services.group_service import (
        feishu_update_from_blob,
        get_group_detail,
        tables_config_from_group,
        update_group,
    )

    settings = get_settings()
    resolved_db = db_path or settings.db_path

    # 1) Load group config; bail if Feishu not enabled for this group.
    async with aiosqlite.connect(resolved_db) as db:
        group = await get_group_detail(db, group_id)
        if not group:
            return {"status": "failed", "reason": f"group {group_id} not found"}
        if not group.feishu_enabled:
            return {"status": "skipped", "reason": "feishu not enabled for group"}

        base_token = group.feishu_base_token or ""
        base_name = group.display_name or group_id
        tables_config = tables_config_from_group(group)
        # custom_tables is the authoritative per-group table config (single source
        # of truth). Without passing it, any table toggled via custom_tables
        # (engineering, world_models, …) would be invisible to the feishu push.
        custom_tables_config = group.custom_tables or None
        l3_root = (
            Path(group.output_dir)
            if group.output_dir
            else Path(settings.layer3_output_dir) / group_id
        )

        # 2) Ensure the Base framework exists; persist new tokens if we created it.
        active = feishu_schema.active_kinds(tables_config, custom_tables_config)
        initialized = group.feishu_framework_initialized == 1
        complete = bool(
            initialized
            and base_token
            and all(feishu_schema.table_cfg(tables_config, k)["table_id"] for k in active)
        )
        if not complete:
            fw = await feishu_uploader.ensure_framework(
                base_name=base_name,
                base_token=base_token,
                tables_config=tables_config,
                custom_tables_config=custom_tables_config,
            )
            if fw["status"] == "failed":
                return {"status": "failed", "reason": fw["reason"]}
            base_token = fw["base_token"]
            tables_config = fw["tables_config"]
            await update_group(db, group_id, feishu_update_from_blob(tables_config, base_token))

    # 3) Load Layer-3 JSON for the group/day and upload.（M4: 读指定版本，默认 active）
    l3_data = feishu_schema.load_l3(l3_root, date, version_number=version_number)
    if not l3_data:
        return {"status": "failed", "reason": f"L3 JSON not found under {l3_root} for {date}"}

    async with _get_feishu_push_semaphore():
        result = await feishu_uploader.upload_group_day(
            base_token=base_token,
            tables_config=tables_config,
            custom_tables_config=custom_tables_config,
            l3_data=l3_data,
            date=date,
            group_id=group_id,
            overwrite=overwrite,
            engineering_enabled=bool(group.engineering_enabled),
        )
    logger.info(
        "Feishu push: report_id=%s status=%s counts=%s errors=%s",
        report_id,
        result["status"],
        result["counts"],
        result["errors"],
    )
    # Map to the legacy return contract expected by the task queue / frontend.
    if result["status"] in ("ok", "partial"):
        # Persist push timestamp so the frontend can show "已推送" across refreshes.
        try:
            from datetime import datetime, timezone

            pushed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017
            async with aiosqlite.connect(resolved_db) as db2:
                await db2.execute(
                    "UPDATE report_versions SET feishu_pushed_at = ? "
                    "WHERE group_id = ? AND date = ?",
                    (pushed_at, group_id, date),
                )
                await db2.commit()
        except Exception:
            logger.exception("_feishu_push_coro: failed to persist feishu_pushed_at")

        return {
            "status": "uploaded",
            "rows_count": result["rows_total"],
            "counts": result["counts"],
            "errors": result["errors"],
        }
    if result["status"] == "no_content":
        return {
            "status": "skipped",
            "reason": "no L3 content to upload",
            "counts": result["counts"],
        }
    return {"status": "failed", "reason": "; ".join(result["errors"]) or "upload failed"}


async def push_report_to_feishu(
    db: aiosqlite.Connection,
    report_id: str,
    *,
    doc_title: str | None = None,
    overwrite: bool = True,
    db_path: str | None = None,
) -> str | None:
    """Enqueue an async Feishu push task for a report. Returns task_id (UUID).

    # P067: Uses start_task for 202 async pattern.
    # P054: Service function — zero FastAPI dependency.

    Args:
        db: aiosqlite database connection.
        report_id: Report identifier.
        doc_title: Optional custom Feishu document title.
        overwrite: If True, delete existing records for same date before uploading.
        db_path: Optional SQLite path override.

    Returns:
        task_id UUID string, or None if the report has no versions / error.
    """
    # A008: explicit initialization
    result: str | None = None
    try:
        # 兼容两种入参：report_id（{group_id}-{date}）或 version_id（{report_id}-v{n}）。
        # Index 仪表盘传 version_id（g.report_version_id），Report 详情页传 report_id。
        # M4: 优先取 active 版本（回滚后推 active 而非 latest）；无 active 回退 latest。
        active = await report_version.get_active_version(db, report_id)
        if active is None:
            versions = await report_version.list_versions(db, report_id)
            active = versions[-1] if versions else await report_version.get_version(db, report_id)
        if active is None:
            return None
        group_id = active.group_id
        date = active.date
        push_version_number = active.version_number

        from z_winnow.config.settings import get_settings
        from z_winnow.web.services.task_queue import start_task

        resolved_db = db_path or get_settings().db_path

        def _coro_factory() -> Any:
            return _feishu_push_coro(
                report_id=report_id,
                group_id=group_id,
                date=date,
                doc_title=doc_title,
                overwrite=overwrite,
                db_path=resolved_db,
                version_number=push_version_number,
            )

        task_id = await start_task(
            task_type="feishu_push",
            resource_id=report_id,
            coro_factory=_coro_factory,
            db_path=resolved_db,
        )
        result = task_id
    except Exception:
        # P014: log and return None
        logger.exception("push_report_to_feishu failed for report_id=%s", report_id)
        result = None
    return result


async def auto_push_after_run(
    group_id: str, date: str, *, db_path: str, run_id: str | None = None
) -> str | None:
    """Auto-push a day's report to Feishu after a successful pipeline run.

    Gated by the group's ``feishu_enabled`` toggle: returns ``None`` (no task row
    created) when the group is missing or Feishu is disabled. Non-blocking — the
    real upload runs in a background ``asyncio`` task via ``push_report_to_feishu``
    (which itself calls ``start_task``), so this returns within milliseconds and
    never delays the next date's generation. Never raises: an auto-push failure
    must not propagate to the pipeline caller.

    ``run_id`` is purely a log-correlation handle (run → push task_id); it is NOT
    stored on any row, keeping the push decoupled from the pipeline_runs domain
    (the push has its own global-concurrent lifecycle in ``async_tasks``).

    Args:
        group_id: Internal group id (``g_xxx``).
        date: Report date, ``YYYY-MM-DD`` or ``YYYYMMDD`` (normalized internally —
            the callers pass hyphenated form but ``report_versions`` stores YYYYMMDD).
        db_path: SQLite database path.

    Returns:
        ``task_id`` (UUID) if a background push was scheduled, else ``None``.
    """
    try:
        date_yyyymmdd = date.replace("-", "")
        async with aiosqlite.connect(db_path) as db:
            from z_winnow.web.services.group_service import get_group_detail

            group = await get_group_detail(db, group_id)
            if not group or not group.feishu_enabled:
                return None
            report_id = f"{group_id}-{date_yyyymmdd}"
            task_id = await push_report_to_feishu(
                db, report_id=report_id, overwrite=True, db_path=db_path
            )
        logger.info(
            "auto_push_after_run: run_id=%s group=%s date=%s task_id=%s",
            run_id,
            group_id,
            date_yyyymmdd,
            task_id,
        )
        return task_id
    except Exception:
        logger.exception(
            "auto_push_after_run failed: run_id=%s group=%s date=%s", run_id, group_id, date
        )
        return None


async def delete_feishu_records_for_report(
    db: aiosqlite.Connection,
    report_id: str,
    *,
    db_path: str | None = None,
) -> str | None:
    """Enqueue an async task to delete Feishu records for a report's date.

    Only deletes old records — does NOT upload new content. This is the standalone
    counterpart to ``push_report_to_feishu(overwrite=True)`` for callers who want
    to control the delete step separately.

    Args:
        db: aiosqlite database connection.
        report_id: Report identifier.
        db_path: Optional SQLite path override.

    Returns:
        task_id UUID string, or None if the report has no versions / error.
    """
    # A008: explicit initialization
    result: str | None = None
    try:
        versions = await report_version.list_versions(db, report_id)
        if versions:
            latest = versions[-1]
        else:
            latest = await report_version.get_version(db, report_id)
            if latest is None:
                return None
        group_id = latest.group_id
        date = latest.date

        from z_winnow.config.settings import get_settings
        from z_winnow.web.services.task_queue import start_task

        resolved_db = db_path or get_settings().db_path

        async def _coro() -> dict[str, Any]:
            return await _feishu_delete_only_coro(
                report_id=report_id,
                group_id=group_id,
                date=date,
                db_path=resolved_db,
            )

        task_id = await start_task(
            task_type="feishu_delete_records",
            resource_id=report_id,
            coro_factory=lambda: _coro(),
            db_path=resolved_db,
        )
        result = task_id
    except Exception:
        logger.exception("delete_feishu_records_for_report failed for report_id=%s", report_id)
        result = None
    return result


# ============================================================
# #9.2 Web API: 日报配图生成（本地，不挂飞书）
# ============================================================


async def _cover_gen_coro(
    report_id: str,
    group_id: str,
    date: str,
    *,
    count: int | None = None,
    ratio: str | None = None,
    size: str | None = None,
) -> dict[str, Any]:
    """Background coroutine: generate cover image via DMX (local only, no feishu).

    Returns ``{"status": "done", "files": [path...]}``. 异常向上抛，由
    ``task_queue._spawn_background`` 写入 task 的 error 列。

    On success, writes ``cover_generated=1`` to the matching ``report_versions``
    row(s) so the flag survives restarts and is visible via the report API.
    """
    import aiosqlite

    from z_winnow.config.settings import get_settings
    from z_winnow.outputs.image_gen import generate_cover

    paths = await generate_cover(group_id, date, count=count, ratio=ratio, size=size)

    # Persist cover_generated flag on all report versions for this group+date.
    try:
        settings = get_settings()
        async with aiosqlite.connect(settings.db_path) as db:
            await db.execute(
                "UPDATE report_versions SET cover_generated = 1 WHERE group_id = ? AND date = ?",
                (group_id, date),
            )
            await db.commit()
    except Exception:
        logger.exception(
            "_cover_gen_coro: failed to persist cover_generated for %s/%s", group_id, date
        )

    return {"status": "done", "files": [str(p) for p in paths]}


async def generate_report_cover(
    db: aiosqlite.Connection,
    report_id: str,
    *,
    count: int | None = None,
    ratio: str | None = None,
    size: str | None = None,
    db_path: str | None = None,
) -> str | None:
    """Enqueue an async cover-generation task. Returns task_id (UUID) or None.

    # P067: Uses start_task for 202 async pattern. 本地生成（不挂飞书——挂飞书
    # 走 push_report_to_feishu + image_gen_enabled）。
    入参同 push_report_to_feishu：report_id 或 version_id 均可。
    """
    # A008: explicit initialization
    result: str | None = None
    try:
        versions = await report_version.list_versions(db, report_id)
        if versions:
            latest = versions[-1]
        else:
            latest = await report_version.get_version(db, report_id)
            if latest is None:
                return None
        group_id = latest.group_id
        date = latest.date

        from z_winnow.config.settings import get_settings
        from z_winnow.web.services.task_queue import start_task

        resolved_db = db_path or get_settings().db_path

        def _coro_factory() -> Any:
            return _cover_gen_coro(
                report_id=report_id,
                group_id=group_id,
                date=date,
                count=count,
                ratio=ratio,
                size=size,
            )

        task_id = await start_task(
            task_type="cover_gen",
            resource_id=report_id,
            coro_factory=_coro_factory,
            db_path=resolved_db,
        )
        result = task_id
    except Exception:
        logger.exception("generate_report_cover failed for report_id=%s", report_id)
        result = None
    return result


async def get_cover_image(db: aiosqlite.Connection, report_id: str) -> Path | None:
    """返回报告已生成的 cover.png 路径，无则 None。

    解析 group_id+date 的方式同 push_report_to_feishu；仅当文件真实存在时返回。
    """
    try:
        versions = await report_version.list_versions(db, report_id)
        if versions:
            latest = versions[-1]
        else:
            latest = await report_version.get_version(db, report_id)
            if latest is None:
                return None
        from z_winnow.config.settings import get_settings
        from z_winnow.pipeline.l3_paths import resolve_l3_dir

        l3_dir = resolve_l3_dir(get_settings().layer3_output_dir, latest.group_id, latest.date)
        # cover.png (单张) 优先；多张时 fallback 到 cover_01.png
        for candidate in ("cover.png", "cover_01.png"):
            cover_path = l3_dir / candidate
            if cover_path.is_file():
                return cover_path
        return None
    except Exception:
        logger.exception("get_cover_image failed for report_id=%s", report_id)
        return None


# ============================================================
# W15-P0-REPORTS: regenerate_report + export_report
# ============================================================


async def _finalize_regeneration(
    db_path: str,
    group_id: str,
    date: str,
    feedback_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """M4: regenerate 成功后回填溯源四元组 + mark_consumed + 经验派生 + MemOS 纠正。

    幂等可重入：已 consumed 的 mark_consumed 返回 False（无害）；已有 memos_node_id
    的 correct_memory_for_feedback 跳过。每步 best-effort + 独立 try，单条失败不阻断。
    """
    import aiosqlite as _aio

    from z_winnow.memory.factory import create_memos_adapter
    from z_winnow.memory.feedback_corrector import (
        correct_memory_for_feedback,
        derive_lesson,
    )
    from z_winnow.pipeline.database import (
        init_database_in_conn,
        update_feedback_provenance,
    )
    from z_winnow.pipeline.feedback_consumer import mark_consumed_batch
    from z_winnow.pipeline.group_experiences import create_experience
    from z_winnow.pipeline.report_version import get_latest_version

    report_id = f"{group_id}-{date}"
    n_exp = 0
    new_vid: str | None = None
    try:
        async with _aio.connect(db_path) as db:
            await init_database_in_conn(db)
            new_ver = await get_latest_version(db, report_id)
            new_vid = new_ver.version_id if new_ver else None

            # MemOS 纠正器（best-effort：MemOS disabled/degraded 时各调用自降级）
            adapter: Any = None
            try:
                adapter = create_memos_adapter()
            except Exception as exc:
                logger.warning("regenerate finalize: MemOS adapter unavailable — %s", exc)

            for fb in feedback_rows:
                fid = fb.get("feedback_id")
                if not fid:
                    continue
                # ① 派生经验（有 corrected_text 才写 group_experiences）
                if fb.get("corrected_text"):
                    try:
                        await create_experience(
                            db,
                            group_id,
                            derive_lesson(fb),
                            topic_name=(
                                fb.get("target_id")
                                if fb.get("target_type") == "topic"
                                else None
                            ),
                            target_type=fb.get("target_type"),
                            origin_feedback_id=fid,
                            origin_version_id=new_vid,
                        )
                        n_exp += 1
                    except Exception as exc:
                        logger.warning(
                            "regenerate finalize: experience failed fb=%s — %s", fid, exc
                        )
                # ② MemOS 记忆纠正（归档旧 node + 写纠正 node，回填 memos_node_id 等）
                if adapter is not None:
                    try:
                        await correct_memory_for_feedback(adapter, db, fb)
                    except Exception as exc:
                        logger.warning(
                            "regenerate finalize: memos correct failed fb=%s — %s", fid, exc
                        )
                # ③ 回填 produced_version_id
                try:
                    await update_feedback_provenance(db, fid, produced_version_id=new_vid)
                except Exception as exc:
                    logger.warning(
                        "regenerate finalize: provenance failed fb=%s — %s", fid, exc
                    )

            # ④ 批量 mark_consumed（consumed_by = 新版本 id）
            await mark_consumed_batch(
                db,
                [fb["feedback_id"] for fb in feedback_rows if fb.get("feedback_id")],
                new_vid or "manual_regen",
            )
    except Exception:
        logger.exception(
            "regenerate finalize: failed for group=%s date=%s", group_id, date
        )

    return {
        "produced_version_id": new_vid,
        "feedback_count": len(feedback_rows),
        "experiences_created": n_exp,
    }


async def rollback_to_version(
    db: aiosqlite.Connection,
    version_id: str,
) -> dict[str, Any] | None:
    """M4: 回滚日报到指定版本 —— is_active 重指 + feedback/experience 联动。

    回滚单元 = 日报版本（与用户确认一致）：
      1. set_active_version：目标版本 is_active=1，同 report 其余=0。
      2. 该 report 下 version_number > 目标 的版本（被回滚掉的较新版本）：
         - 其 produced feedback 批量 status='rolled_back'（效果随版本撤回）。
         - group_experiences 中 origin_version_id ∈ 这些版本的 经验 → archived。
      3. 反向"再前进"不自动恢复（需手动 regenerate）。

    Returns:
        汇总 dict，或 None（版本不存在）。
    """
    from z_winnow.pipeline.database import update_feedback_provenance
    from z_winnow.pipeline.group_experiences import set_status_by_origin_version
    from z_winnow.pipeline.report_version import (
        get_version,
        list_versions,
        set_active_version,
    )

    v = await get_version(db, version_id)
    if v is None:
        return None
    report_id = v.report_id
    target_n = v.version_number

    # ① is_active 重指
    await set_active_version(db, version_id)

    # ② 较新版本（> target）的 feedback/experience 联动归档
    versions = await list_versions(db, report_id)
    newer_vids = [x.version_id for x in versions if x.version_number > target_n]
    n_fb = 0
    n_exp = 0
    for nv in newer_vids:
        cur = await db.execute(
            "SELECT feedback_id FROM feedback_events WHERE produced_version_id = ?",
            (nv,),
        )
        fids = [r[0] for r in await cur.fetchall()]
        for fid in fids:
            await update_feedback_provenance(db, fid, status="rolled_back")
            n_fb += 1
        n_exp += await set_status_by_origin_version(db, nv, "archived")

    logger.info(
        "rollback_to_version: %s active (n=%d) — %d newer versions, %d feedback, %d experiences archived",
        version_id,
        target_n,
        len(newer_vids),
        n_fb,
        n_exp,
    )
    return {
        "rolled_back_to": version_id,
        "report_id": report_id,
        "active_version_number": target_n,
        "deactivated_versions": newer_vids,
        "feedback_rolled_back": n_fb,
        "experiences_archived": n_exp,
    }


async def list_active_regenerate_tasks(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """M4: 列出所有运行中的 regenerate 任务（queued/running/pending）。

    供前端在页面加载时恢复"重生成中"按钮状态（刷新不丢进度）。从 resource_id
    （= version_id ``{report_id}-v{n}``）反推 report_id，前端按 report_id 匹配按钮。
    """
    results: list[dict[str, Any]] = []
    try:
        import re

        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT task_id, resource_id, status FROM async_tasks "
            "WHERE task_type='regenerate' AND status IN ('queued','running','pending') "
            "ORDER BY created_at DESC"
        )
        for r in await cur.fetchall():
            version_id = r["resource_id"] or ""
            report_id = re.sub(r"-v\d+$", "", version_id)
            results.append(
                {
                    "task_id": r["task_id"],
                    "report_id": report_id,
                    "version_id": version_id,
                    "status": r["status"],
                }
            )
    except Exception:
        logger.exception("list_active_regenerate_tasks failed")
    return results


async def regenerate_report(
    db: aiosqlite.Connection,
    version_id: str,
    *,
    group_id: str | None = None,
    date: str | None = None,
) -> str | None:
    """Enqueue a report regeneration as an async task via P067 start_task.

    Looks up the report version by ``version_id``, resolves group_id and
    date (overrides take precedence), then spawns a background coroutine
    that re-runs the pipeline graph for the report's group+date.

    # P067: Async via start_task — returns task_id, 202 pattern.
    # P022: Zero LLM calls in the enqueue path (coroutine may use LLM).
    # P094: Pure async service function — no FastAPI imports.
    # A008: Explicit initialization before try block.

    Args:
        db: aiosqlite database connection.
        version_id: Report version identifier, format ``{report_id}-v{n}``.
        group_id: Optional override for group_id.
        date: Optional override for date (YYYYMMDD).

    Returns:
        task_id UUID string if enqueued, None if version not found or on error.
    """
    # A008: explicit initialization
    result: str | None = None
    try:
        v = await report_version.get_version(db, version_id)
        if v is None:
            return None

        resolved_group_id = group_id or v.group_id
        resolved_date = date or v.date

        from z_winnow.config.settings import get_settings
        from z_winnow.web.services.task_queue import start_task

        resolved_db = get_settings().db_path

        # M4: 防雪崩——同报告已有运行中 regenerate 任务则直接复用（用户连点 / 前端重试
        # 不再堆积成上百个并发 pipeline 把 web 事件循环压垮）。resource_id=version_id
        # 形如 "{report_id}-v{n}"，按 report_id 前缀匹配同报告的任意版本。
        _dedupe_report_id = f"{resolved_group_id}-{resolved_date}"
        try:
            async with aiosqlite.connect(resolved_db) as _ck:
                _cur = await _ck.execute(
                    "SELECT task_id FROM async_tasks WHERE task_type='regenerate' "
                    "AND status IN ('queued','running','pending') AND resource_id LIKE ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (f"{_dedupe_report_id}-%",),
                )
                _existing = await _cur.fetchone()
            if _existing:
                logger.info(
                    "regenerate_report: reuse active task %s for report %s",
                    _existing[0],
                    _dedupe_report_id,
                )
                result = _existing[0]
                return result
        except Exception:
            logger.debug("regenerate_report: dedupe check skipped", exc_info=True)

        # P067: Coroutine factory — returns a coroutine that the
        # background executor will await. Lazy imports inside to
        # avoid circular deps at module load time.
        def _coro_factory() -> Any:
            """Re-run the pipeline graph (M4 regenerate mode) for the resolved group+date.

            短路 data_fetch/content_enrich（复用 L1 缓存 messages）；unified_reporter 自动
            加载该群该日 unconsumed feedback 作 hints；产出新版本后由 _finalize_regeneration
            回填溯源四元组、mark_consumed、写 group_experiences、调 feedback_memory 纠正
            MemOS 记忆。回填仅在新版本 ainvoke 成功后执行。
            """

            async def _run() -> dict[str, Any]:
                import aiosqlite as _aio

                from z_winnow.graph.builder import get_graph
                from z_winnow.pipeline.database import (
                    get_raw_messages_by_date,
                    get_unconsumed_feedback,
                    init_database_in_conn,
                )

                report_id = f"{resolved_group_id}-{resolved_date}"

                # ── 预加载 L1 messages（解析 raw_json 恢复原形状）+ unconsumed feedback + group_name ──
                async with _aio.connect(resolved_db) as _pdb:
                    await init_database_in_conn(_pdb)
                    _l1_rows = await get_raw_messages_by_date(
                        _pdb, resolved_date, group_id=resolved_group_id
                    )
                    _fb_rows = await get_unconsumed_feedback(
                        _pdb, resolved_group_id, resolved_date
                    )
                    _cur = await _pdb.execute(
                        "SELECT display_name FROM groups WHERE group_id = ?",
                        (resolved_group_id,),
                    )
                    _grow = await _cur.fetchone()
                    _group_name = (_grow[0] if _grow else "") or resolved_group_id

                # 预填 L1 messages：传 raw_messages **行**（含 raw_json 字符串键），与
                # data_fetch 产出的内部格式一致——content_enrich.parse_raw_messages 经
                # _to_api_format 从 raw_json 还原 API 格式再清洗。若塞 json.loads(raw_json)
                # 得到的纯 API dict（无 raw_json 键），_to_api_format 走 fallback 用
                # server_id/msg_type 构造空消息 → 全被过滤 → regen 空报告。
                cached_messages: list[dict[str, Any]] = [
                    dict(_row)
                    for _row in _l1_rows
                    if isinstance(_row, dict) and _row.get("raw_json")
                ]

                # ── 重跑图（regenerate 短路）──
                # M4: 用 get_graph()（已 compile 的 CompiledStateGraph），
                # build_graph() 返回未编译 StateGraph，调 .ainvoke 会报
                # "'StateGraph' object has no attribute 'ainvoke'"。
                from z_winnow.observability.langsmith_setup import init_langsmith
                from z_winnow.observability.tracing import TracedGraphConfig

                # M4: 启用 LangSmith tracing（regenerate 之前缺这步 → 重生成在 LangSmith 无踪）。
                init_langsmith()  # 幂等
                trace_cfg = TracedGraphConfig(
                    trace_name=f"winnow-regen-{_group_name}-{resolved_date}",
                    date=resolved_date,
                    group_name=_group_name,
                    tags=["winnow", "regenerate"],
                )
                config: dict[str, Any] = trace_cfg.to_runnable_config()
                config.setdefault("configurable", {})
                config["configurable"]["group_id"] = resolved_group_id
                config["configurable"]["date"] = resolved_date

                graph = get_graph()
                input_state: dict[str, Any] = {
                    "group_id": resolved_group_id,
                    "date": resolved_date,
                    "group_name": _group_name,
                    "report_id": report_id,
                    "messages": cached_messages,  # 预填 L1 → data_fetch 短路
                    "regenerate": True,  # 短路 data_fetch + content_enrich
                    "source": "manual_regen",
                }

                # M4: 超时护栏——真实 LLM/MemOS 偶发挂起时曾把整个 web 事件循环压垮
                # （health 饿死、堆积任务永不启动）。注意：超时只包 ainvoke（pipeline），
                # _finalize 移到超时外无条件执行——否则 output_composer 写 MemOS 慢时
                # ainvoke 超 300s 会让 _core 整体取消、_finalize 没机会跑（反馈不消费、
                # 经验不派生、记忆不纠正）。ainvoke 提到 480s 给 MemOS 写入留余量。
                try:
                    await asyncio.wait_for(graph.ainvoke(input_state, config), timeout=480)
                except TimeoutError:
                    logger.error(
                        "regenerate ainvoke timed out (480s) for report=%s group=%s",
                        report_id,
                        resolved_group_id,
                    )
                    return {
                        "status": "failed",
                        "error": "regenerate timed out (pipeline 480s)",
                        "report_id": report_id,
                    }

                # 回填闭环：ainvoke 成功后无条件运行（内部每步 best-effort + 独立 try/except）。
                backfill = await _finalize_regeneration(
                    resolved_db, resolved_group_id, resolved_date, _fb_rows
                )

                return {
                    "status": "completed",
                    "group_id": resolved_group_id,
                    "date": resolved_date,
                    "report_id": report_id,
                    "backfill": backfill,
                }

            return _run()

        task_id_val = await start_task(
            task_type="regenerate",
            resource_id=version_id,
            coro_factory=_coro_factory,
            db_path=resolved_db,
        )
        result = task_id_val
    except Exception:
        # P014: log and return None
        logger.exception("regenerate_report failed for version_id=%s", version_id)
        result = None
    return result


async def export_report(
    db: aiosqlite.Connection,
    version_id: str,
    *,
    group_id: str | None = None,
    date: str | None = None,
) -> str | None:
    """Read L3 JSON from disk and render Markdown synchronously.

    Looks up the report version by ``version_id``, resolves group_id and
    date (overrides take precedence), reads the 4 L3 JSON files from
    ``data/processed/{group_id}/{date}/``, renders via Jinja2, and
    returns the raw Markdown text.

    # P022: Pure data retrieval from L3 JSON — zero LLM calls.
    # P094: Pure async service function — no FastAPI imports.
    # A008: Explicit initialization before try block.

    Args:
        db: aiosqlite database connection.
        version_id: Report version identifier, format ``{report_id}-v{n}``.
        group_id: Optional override for group_id.
        date: Optional override for date (YYYYMMDD).

    Returns:
        Rendered Markdown text, or None if version / L3 JSON not found.
    """
    # A008: explicit initialization
    result: str | None = None
    try:
        v = await report_version.get_version(db, version_id)
        if v is None:
            return None

        resolved_group_id = group_id or v.group_id
        resolved_date = date or v.date

        from z_winnow.config.settings import get_settings

        settings = get_settings()
        from z_winnow.pipeline.l3_paths import resolve_l3_dir

        json_dir = resolve_l3_dir(
            settings.layer3_output_dir,
            resolved_group_id,
            resolved_date,
            version_number=v.version_number,
        )

        if not json_dir.exists():
            logger.debug("export_report: L3 JSON directory not found: %s", json_dir)
            return None

        # P022: Read L3 JSON → Jinja2 render → return text (zero LLM)
        from z_winnow.subagents.output_composer import render_markdown

        md_path = render_markdown(json_dir=json_dir)
        result = md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("export_report: L3 JSON missing for version_id=%s", version_id)
        result = None
    except Exception:
        # P014: log and return None
        logger.exception("export_report failed for version_id=%s", version_id)
        result = None
    return result


async def delete_report(
    db: aiosqlite.Connection,
    report_id: str,
) -> bool:
    """Delete a report entirely: all its versions (DB) + the on-disk L3 JSON.

    report_id == ``{group_id}-{date}``，故删除等价于「删除某群某天的整份报告」。
    流程：先取任一版本解析出 group_id/date → 删除该 report_id 全部版本行 →
    再删除磁盘上的 L3 JSON 目录 ``{layer3_output_dir}/{group_id}/{date}/``。

    安全：磁盘路径用 resolve() 校验必须位于 layer3_output_dir 之内，防穿越。
    作用域：仅删 report_versions 行 + L3 报告 JSON 文件；不动 topic_summaries
    （独立数据层，数据浏览器用）与 feedback（用户批注，独立资源）。

    # P022: 删除路径零 LLM 调用。
    # P094: 纯 async service，无 FastAPI 依赖。
    # A008: 显式初始化。

    Args:
        db: aiosqlite database connection.
        report_id: Report identifier (``{group_id}-{date}``).

    Returns:
        True if at least one version row was deleted, False if report not found / error.
    """
    # A008: explicit initialization
    result = False
    try:
        versions = await report_version.list_versions(db, report_id)
        if not versions:
            return False

        first = versions[0]
        group_id = first.group_id
        date = first.date

        deleted_rows = await report_version.delete_report(db, report_id)
        if not deleted_rows:
            return False

        # Best-effort L3 JSON disk cleanup — failure here is non-fatal (DB rows already gone).
        try:
            from z_winnow.config.settings import get_settings

            l3_root = Path(get_settings().layer3_output_dir).resolve()
            l3_dir = (l3_root / group_id / date).resolve()
            # Path-safety: must be inside the configured L3 output root.
            if l3_root == l3_dir or l3_root not in l3_dir.parents:
                logger.warning("delete_report: refusing to delete path outside L3 root: %s", l3_dir)
            elif l3_dir.exists():
                import shutil

                shutil.rmtree(l3_dir)
                logger.info("delete_report: removed L3 dir %s for report_id=%s", l3_dir, report_id)
        except Exception:
            # P014: disk cleanup is best-effort; DB deletion already succeeded.
            logger.exception("delete_report: L3 disk cleanup failed for report_id=%s", report_id)

        result = True
    except Exception:
        # P014: log and return False
        logger.exception("delete_report failed for report_id=%s", report_id)
        result = False
    return result
