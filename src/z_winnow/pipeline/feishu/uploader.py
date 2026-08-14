"""Feishu Bitable uploader — framework init + daily record upload.

Two-phase integration with lark-cli (user identity):

1. ``ensure_framework`` — for a group whose Base target is empty/uninitialized,
   create the Base + the active tables from :data:`schema.TABLE_CATALOG`
   (mandatory spine + the group's enabled optional kinds). For a group already
   carrying a full set of table IDs, it's a no-op.
2. ``upload_group_day`` — read that group/day's Layer-3 JSON, map to rows per
   active table via its catalog-declared mapper, and batch-create records
   (plus any declared attachment hooks).

All Feishu I/O flows through :mod:`lark_cli` (subprocess + user identity + auto
token refresh). The functions are storage-free: they take config
(``base_token`` + per-group ``tables_config`` blob) and return results + the
updated blob to persist; the caller (group_service / report_service) loads the
group row beforehand and writes ``feishu_*`` updates back afterward.
"""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from z_winnow.pipeline.feishu import lark_cli, schema

logger = logging.getLogger(__name__)


# Large attachments use lark-cli's multipart upload (drive medias upload_prepare /
# upload_part / upload_finish). A 131MB book over a slow/proxied link takes many
# minutes; run()'s default 90s timeout kills it mid-upload — the root cause of
# large resource files (e.g. book PDFs) never pushing. Scale the per-file timeout
# by size at a conservative throughput, floored/capped. Safe to be generous: this
# runs inside the non-blocking background push task, so a long timeout only
# extends the wait for a genuinely-hung process; it never blocks generation.
_ATTACHMENT_TIMEOUT_RATE_BPS = 100 * 1024  # ~100 KB/s conservative (slow/proxied) throughput
_ATTACHMENT_TIMEOUT_OVERHEAD_S = 90.0  # prepare / per-part / finish call overhead
_ATTACHMENT_TIMEOUT_FLOOR_S = 180.0  # bounds hang-detection wait for small files
_ATTACHMENT_TIMEOUT_CAP_S = 3600.0  # 60 min hard cap


def _attachment_upload_timeout(file_path: Path) -> float:
    """Size-aware lark-cli attachment upload timeout (seconds).

    Returns a timeout generous enough for ``file_path``'s multipart upload at a
    conservative worst-case rate, floored for small files and capped to bound
    genuinely-hung processes. ``file_path`` not existing/stat-able → floor.
    """
    try:
        size = file_path.stat().st_size
    except OSError:
        size = 0
    return min(
        _ATTACHMENT_TIMEOUT_CAP_S,
        max(_ATTACHMENT_TIMEOUT_FLOOR_S, size / _ATTACHMENT_TIMEOUT_RATE_BPS + _ATTACHMENT_TIMEOUT_OVERHEAD_S),
    )


# ============================================================
# Response parsing helpers
# ============================================================


def _extract(res: dict[str, Any], *path: str, default: Any = None) -> Any:
    """Walk a nested dict by keys; return default if any step is missing."""
    cur: Any = res
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _app_token_of(base_res: dict[str, Any]) -> str:
    # Real lark-cli shape: data.base.base_token
    return str(
        _extract(base_res, "data", "base", "base_token", default="")
        or _extract(base_res, "data", "app_token", default="")
        or _extract(base_res, "data", "appToken", default="")
        or ""
    )


def _first_table_id_of(base_res: dict[str, Any]) -> str:
    # Real lark-cli shape: data.table.id
    return str(
        _extract(base_res, "data", "table", "id", default="")
        or _extract(base_res, "data", "table_id", default="")
        or ""
    )


def _table_id_of(table_res: dict[str, Any]) -> str:
    # Real lark-cli shape: data.table.id
    return str(
        _extract(table_res, "data", "table", "id", default="")
        or _extract(table_res, "data", "table_id", default="")
        or ""
    )


# ============================================================
# Framework init
# ============================================================


def _framework_complete(
    *, base_token: str, tables_config: dict[str, Any], active: list[str]
) -> bool:
    """True if a base + every active kind's table_id is already configured."""
    if not base_token:
        return False
    return all(schema.table_cfg(tables_config, k)["table_id"] for k in active)


async def delete_existing_records_for_date(
    *,
    base_token: str,
    tables_config: dict[str, Any],
    date: str,
    custom_tables_config: dict[str, Any] | None = None,
    mock: bool | None = None,
) -> list[str]:
    """Delete all existing records whose 日期 field matches ``date``, per active table.

    Uses lark-cli's ``--filter-json`` (confirmed working with ``ExactDate()`` syntax).
    Parses the real JSON response structure:
    ``{data: {data: [[row...]], fields: [...], record_id_list: [...]}}``
    where ``record_id_list`` is aligned with the row array.

    Returns a list of error messages for any tables that failed.
    """
    errors: list[str] = []
    from z_winnow.pipeline.feishu.schema import _norm_date

    norm_date = _norm_date(date)  # YYYY-MM-DD

    for kind in schema.active_kinds(tables_config, custom_tables_config):
        table_id = schema.table_cfg(tables_config, kind)["table_id"]
        if not table_id:
            continue

        # Each table kind has its own date field name:
        #   summary/topics/engineering → "日期"
        #   resources → "发布日期"
        tdef = schema.TABLE_CATALOG.get(kind)
        date_field = (tdef.fields[0]["name"]) if (tdef and tdef.fields) else "日期"
        kind_filter = json.dumps({
            "logic": "and",
            "conditions": [[date_field, "==", f"ExactDate({norm_date})"]],
        })

        try:
            list_res = await lark_cli.record_list(
                base_token, table_id,
                filter_json=kind_filter,
                limit=200,
                mock=mock,
            )
            # Real response shape (verified with lark-cli v452734f):
            #   data.record_id_list  → aligned with data.data rows
            #   data.data            → [[field_values...], ...]
            #   data.fields          → ["日期", "议题数", ...]
            data_envelope = list_res.get("data") or {}
            rec_ids: list[str] = data_envelope.get("record_id_list") or []

            if rec_ids:
                await lark_cli.record_delete(base_token, table_id, rec_ids, mock=mock)
                logger.info(
                    "feishu overwrite: deleted %d records from %s (date=%s)",
                    len(rec_ids), kind, norm_date,
                )
            else:
                logger.debug(
                    "feishu overwrite: no existing records in %s for date=%s",
                    kind, norm_date,
                )
        except lark_cli.LarkCliError as exc:
            errors.append(f"{kind} delete-existing: {exc}")
            logger.warning("feishu overwrite: delete failed for %s: %s", kind, exc)
        except Exception as exc:
            errors.append(f"{kind} delete-existing: {exc}")
            logger.warning("feishu overwrite: delete failed for %s: %s", kind, exc)

    return errors


# ============================================================
# Upload
# ============================================================


async def ensure_framework(
    *,
    base_name: str,
    base_token: str = "",
    tables_config: dict[str, Any] | None = None,
    custom_tables_config: dict[str, Any] | None = None,
    mock: bool | None = None,
) -> dict[str, Any]:
    """Ensure the group's Base has the table framework for its active kinds.

    Active kinds = mandatory kinds ∪ kinds the group enabled in ``tables_config``
    (see :func:`schema.active_kinds`). For each active kind missing a table_id,
    create the table — the first one inline with ``base_create`` if the Base
    itself is missing, the rest via ``table_create``.

    Args:
        base_name: Base name (usually the group display name).
        base_token: existing Base app_token, if any (empty ⇒ create a new Base).
        tables_config: per-group blob ``{kind: {enabled, table_id}}``. May be None
            (treated as empty — mandatory kinds are still created).
        custom_tables_config: per-group blob ``{kind: {enabled, config}}`` for custom tables.
            When provided, engineering table is controlled by custom_tables_config.engineering.enabled.
        mock: force lark-cli mock mode.

    Returns:
        ``{"base_token": str, "tables_config": {kind: {enabled, table_id}},
        "created": bool, "status": "ok"|"skipped"|"failed", "reason": str|None}``
    """
    tables_config = {
        k: dict(v) if isinstance(v, dict) else {} for k, v in (tables_config or {}).items()
    }
    active = schema.active_kinds(tables_config, custom_tables_config)
    # Ensure every active kind has an entry with enabled=True + its table_id (or "").
    for kind in active:
        tables_config[kind] = {
            "enabled": True,
            "table_id": schema.table_cfg(tables_config, kind)["table_id"],
        }

    if _framework_complete(base_token=base_token, tables_config=tables_config, active=active):
        return {
            "base_token": base_token,
            "tables_config": tables_config,
            "created": False,
            "status": "skipped",
            "reason": "framework already configured",
        }

    created = False
    try:
        if not base_token:
            # Create Base with the first active table inline.
            first = active[0]
            tdef = schema.TABLE_CATALOG[first]
            res = await lark_cli.base_create(base_name, tdef.display_name, tdef.fields, mock=mock)
            base_token = _app_token_of(res)
            tables_config[first]["table_id"] = _first_table_id_of(res)
            logger.info(
                "feishu: created Base %s (table %s=%s)",
                base_name,
                first,
                tables_config[first]["table_id"],
            )
            created = True
            remaining = active[1:]
        else:
            # Base exists but some table IDs missing — add any missing active tables.
            remaining = [k for k in active if not tables_config[k]["table_id"]]

        for kind in remaining:
            tdef = schema.TABLE_CATALOG[kind]
            t_res = await lark_cli.table_create(
                base_token, tdef.display_name, tdef.fields, mock=mock
            )
            tables_config[kind]["table_id"] = _table_id_of(t_res)
            logger.info(
                "feishu: added table %s (%s=%s)",
                tdef.display_name,
                kind,
                tables_config[kind]["table_id"],
            )
            created = True
    except lark_cli.LarkCliError as exc:
        return {
            "base_token": base_token,
            "tables_config": tables_config,
            "created": False,
            "status": "failed",
            "reason": f"framework init failed: {exc}",
        }

    return {
        "base_token": base_token,
        "tables_config": tables_config,
        "created": created,
        "status": "ok",
        "reason": None,
    }


# ============================================================
# Upload
# ============================================================


async def upload_group_day(
    *,
    base_token: str,
    tables_config: dict[str, Any],
    l3_data: dict[str, dict[str, Any]],
    date: str,
    group_id: str = "",
    overwrite: bool = True,
    engineering_enabled: bool = True,
    custom_tables_config: dict[str, Any] | None = None,
    mock: bool | None = None,
) -> dict[str, Any]:
    """Upload one group/day's Layer-3 data into the active framework tables.

    Iterates the group's active kinds (mandatory ∪ enabled, in catalog order);
    for each, maps L3 rows via the kind's :attr:`TableDef.source` mapper,
    batch-creates them, then runs the kind's declared attachment hooks
    (``daily_md`` / ``cover`` / ``resource_files``).

    When ``overwrite=True`` (default), existing records for the same date are
    deleted before new records are created, so each push replaces rather than
    appends.

    Args:
        base_token: Base app_token (must already exist).
        tables_config: per-group blob ``{kind: {enabled, table_id}}``.
        l3_data: ``{kind: json_dict}`` from :func:`schema.load_l3`.
        date: report date string.
        group_id: group identifier — used to locate a pre-generated cover image
            (``{layer3_output_dir}/{group_id}/{date}/cover.png``). Only attached
            when ``settings.image_gen_enabled`` is True and the file exists.
        overwrite: if True, delete existing records matching ``date`` before
            creating new ones (default True).
        engineering_enabled: if False, skip the engineering kind even if the
            tables_config says it's active (#7.1). Deprecated in favor of
            custom_tables_config.engineering.enabled.
        custom_tables_config: per-group blob ``{kind: {enabled, config}}`` for custom tables.
            When provided, engineering table is controlled by custom_tables_config.
        mock: force lark-cli mock mode.

    Returns:
        ``{"status": "ok"|"partial"|"failed"|"no_content", "counts": {kind: N},
        "errors": [str], "rows_total": int}``
    """
    counts: dict[str, int] = {}
    errors: list[str] = []
    rows_total = 0

    # Overwrite mode: delete existing records for this date first.
    if overwrite:
        del_errors = await delete_existing_records_for_date(
            base_token=base_token,
            tables_config=tables_config,
            date=date,
            custom_tables_config=custom_tables_config,
            mock=mock,
        )
        errors.extend(del_errors)

    # CT-5: Determine effective engineering_enabled from custom_tables_config if provided
    effective_engineering_enabled = engineering_enabled
    if custom_tables_config and isinstance(custom_tables_config, dict):
        eng_cfg = custom_tables_config.get("engineering")
        if isinstance(eng_cfg, dict) and "enabled" in eng_cfg:
            effective_engineering_enabled = bool(eng_cfg.get("enabled"))
            logger.info(
                "feishu: custom_tables overrides engineering_enabled=%s",
                effective_engineering_enabled,
            )

    for kind in schema.active_kinds(tables_config, custom_tables_config):
        # #7.1: Skip engineering if the independent toggle is off.
        if kind == "engineering" and not effective_engineering_enabled:
            logger.info("feishu: engineering disabled for this group, skipping kind=engineering")
            counts[kind] = 0
            continue
        tdef = schema.TABLE_CATALOG[kind]
        table_id = schema.table_cfg(tables_config, kind)["table_id"]
        if not table_id:
            errors.append(f"{kind}: no table_id configured, skipped")
            continue
        data = l3_data.get(tdef.source.l3_key)
        if not data:
            counts[kind] = 0
            continue

        columns, rows = tdef.source.mapper(data, date)
        if not rows:
            counts[kind] = 0
            continue

        try:
            create_res = await lark_cli.record_batch_create(
                base_token, table_id, columns, rows, mock=mock
            )
            counts[kind] = len(rows)
            rows_total += len(rows)
            logger.info("feishu: wrote %d rows to %s (%s)", len(rows), kind, table_id)

            # Attachment hooks declared on the TableDef (kind-agnostic dispatch).
            # Real lark-cli JSON envelope: data.record_id_list.
            # Mock puts record_id_list at top level for compat.
            data_env = create_res.get("data") or {}
            record_ids = (
                create_res.get("record_id_list")
                or data_env.get("record_id_list")
                or create_res.get("record_ids")
                or []
            )
            for hook in tdef.attachments:
                if not record_ids:
                    break
                if hook == "daily_md":
                    await _attach_daily_markdown(
                        base_token=base_token,
                        table_id=table_id,
                        record_id=record_ids[0],
                        daily_data=data,
                        date=date,
                        mock=mock,
                        errors=errors,
                    )
                elif hook == "cover":
                    # #9.2: gen-image 预生成的配图挂「图片」(best-effort，无图跳过)。
                    await _attach_cover_image(
                        base_token=base_token,
                        table_id=table_id,
                        record_id=record_ids[0],
                        group_id=group_id,
                        date=date,
                        mock=mock,
                        errors=errors,
                    )
                elif hook == "resource_files":
                    # #9.3: 带 local_path 的资源行上传「附件」(两阶段)。
                    items = [r for r in (data.get("resources") or []) if isinstance(r, dict)]
                    if items:
                        await _attach_resource_files(
                            base_token=base_token,
                            table_id=table_id,
                            record_ids=record_ids,
                            resource_items=items,
                            mock=mock,
                            errors=errors,
                        )
        except lark_cli.LarkCliError as exc:
            counts[kind] = 0
            errors.append(f"{kind}: {exc}")

    if errors and rows_total == 0:
        status = "failed"
    elif errors:
        status = "partial"
    elif rows_total == 0:
        status = "no_content"
    else:
        status = "ok"

    return {
        "status": status,
        "counts": counts,
        "errors": errors,
        "rows_total": rows_total,
    }


__all__ = [
    "delete_existing_records_for_date",
    "ensure_framework",
    "upload_group_day",
]


async def _attach_daily_markdown(
    *,
    base_token: str,
    table_id: str,
    record_id: str,
    daily_data: dict[str, Any],
    date: str,
    mock: bool | None,
    errors: list[str],
) -> None:
    """Render the daily report to Markdown and upload it to the 日报文档 field.

    Best-effort: a failure here is logged/appended to ``errors`` but does not
    undo the row data already written. Skips silently if the template renderer
    is unavailable or yields empty content.
    """
    try:
        from z_winnow.templates import render_feishu_daily

        md = render_feishu_daily(daily_data)
    except Exception as exc:  # ImportError or render error
        logger.warning("feishu: render_feishu_daily unavailable, skipping MD attachment: %s", exc)
        return

    if not (md or "").strip():
        return

    # lark-cli refuses absolute --file paths (must be relative to cwd), so write
    # the MD into a temp directory and run with cwd=that dir + a bare filename.
    import shutil

    tmp_dir = tempfile.mkdtemp(prefix="feishu_md_")
    file_name = f"daily_{date}.md"
    file_path = Path(tmp_dir) / file_name
    try:
        file_path.write_text(md, encoding="utf-8")
        await lark_cli.record_upload_attachment(
            base_token,
            table_id,
            record_id,
            "日报文档",
            file_name,
            cwd=tmp_dir,
            mock=mock,
            timeout=_attachment_upload_timeout(file_path),
        )
        logger.info("feishu: attached daily MD (%d chars) to record %s", len(md), record_id)
    except lark_cli.LarkCliError as exc:
        errors.append(f"日报文档 attachment: {exc}")
    except Exception as exc:
        errors.append(f"日报文档 attachment: {exc}")
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _attach_cover_image(
    *,
    base_token: str,
    table_id: str,
    record_id: str,
    group_id: str,
    date: str,
    mock: bool | None,
    errors: list[str],
) -> None:
    """把 gen-image 预生成的日报配图挂到「图片」字段（#9.2）。

    图片源 = ``{layer3_output_dir}/{group_id}/{date}/cover.png``（由 ``gen-image`` CLI
    或 Web UI 配图生成提前生成）。只要文件存在就挂——不依赖 image_gen_enabled 开关
    （该开关仅控制生成入口，不应阻断已生成图片的上传）。best-effort：失败 append
    errors 不阻断主上传。
    """
    if not group_id:
        return
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    from z_winnow.pipeline.l3_paths import resolve_l3_dir

    cover_path = resolve_l3_dir(settings.layer3_output_dir, group_id, date) / "cover.png"
    if not cover_path.is_file():
        logger.debug("feishu: cover image not found, skip 图片 attachment: %s", cover_path)
        return
    try:
        await lark_cli.record_upload_attachment(
            base_token,
            table_id,
            record_id,
            "图片",
            cover_path.name,
            cwd=str(cover_path.parent),
            mock=mock,
            timeout=_attachment_upload_timeout(cover_path),
        )
        logger.info("feishu: attached cover image %s to record %s", cover_path.name, record_id)
    except lark_cli.LarkCliError as exc:
        errors.append(f"图片 attachment: {exc}")
    except Exception as exc:
        errors.append(f"图片 attachment: {exc}")


async def _attach_resource_files(
    *,
    base_token: str,
    table_id: str,
    record_ids: list[str],
    resource_items: list[dict[str, Any]],
    mock: bool | None,
    errors: list[str],
) -> None:
    """把带 local_path 的资源行, 上传本地文件到飞书资源表「附件」字段。

    两阶段附件上传: record_batch_create 已写文本列并返回 record_ids(与 items 等序,
    lark_cli.record_batch_create 保证), 这里对有 local_path 且文件存在的行调
    record_upload_attachment。best-effort — 单个失败 append errors 不阻断其他。
    """
    for idx, (rid, item) in enumerate(zip(record_ids, resource_items, strict=False)):
        local_path = item.get("local_path")
        if not local_path or not isinstance(local_path, str):
            continue
        p = Path(local_path)
        if not p.is_file():
            logger.warning("feishu: resource attachment file not found: %s", local_path)
            continue
        label = item.get("resource_title") or item.get("title") or str(idx)
        try:
            await lark_cli.record_upload_attachment(
                base_token,
                table_id,
                rid,
                "附件",
                p.name,
                cwd=str(p.parent),
                mock=mock,
                timeout=_attachment_upload_timeout(p),
            )
            logger.info("feishu: attached resource file %s to record %s", p.name, rid)
        except lark_cli.LarkCliError as exc:
            errors.append(f"资源附件[{label}]: {exc}")
        except Exception as exc:
            errors.append(f"资源附件[{label}]: {exc}")
