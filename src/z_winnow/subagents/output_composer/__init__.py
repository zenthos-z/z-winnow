"""output_composer subagent — compose L3 JSON, optionally render Markdown.

Receives output from unified_reporter (single dict). Two-phase design:

  Phase E (compose_json): unified_reporter output → 4 L3 JSON files.
    Called by node_output_composer in the main graph flow.
    Does NOT render Markdown — adheres to S1 (immediate persist) and
    S4 (review before export).

  Phase H (render_markdown): L3 JSON files → Jinja2 Markdown report.
    NOT called in the main flow. Intended for manual/user-triggered export.

W16-A2: ``_dict_to_composed`` consumes unified_reporter's strongly-typed
Topic/Resource/EngineeringIssue models (dict→model→model_dump→dict) with
per-item fault isolation (L037). The legacy monolithic Markdown-composition
entry point, its private I/O model classes, and the degraded-rendering
module (former self-loop with this package) were removed as dead code.

Architecture reference: docs/architecture-detail.md §2.4
Track reference: plans/tracks/track-e4.md
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from z_winnow.subagents.output_composer.merger import ComposedData
from z_winnow.subagents.output_composer.quality import quality_check
from z_winnow.subagents.output_composer.renderer import (
    check_markdown_syntax,
    render_composed,
)
from z_winnow.subagents.unified_reporter.models import (
    EngineeringIssue,
    Resource,
    Topic,
)

logger = logging.getLogger(__name__)


# ============================================================
# Phase E: compose_json — write 4 L3 JSON files (T-W12-3)
# ============================================================
# S1: Immediate persist — output_composer writes L3 JSON at Phase E.
# S4: Review before export — Markdown is NOT generated here.


async def compose_json(
    unified_report_output: dict[str, Any] | None,
    output_dir: str | Path,
    *,
    date: str = "",
    custom_tables_config: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Phase E: Write unified_reporter output to L3 JSON files.

    CT-3: Dynamic file writing based on custom_tables_config.
    Mandatory tables (daily, resources, topics) are always written.
    engineering.json is written unless explicitly disabled in custom_tables_config.

    When custom_tables_config is None (not provided) or empty, all 4 files
    are written for backward compatibility.

    Produces:
      - daily.json: overview + topics + highlights + trend_analysis + trend_summary
      - resources.json: resource list with type counts
      - engineering.json: engineering issues + group_summary (conditional)
      - topics.json: unified topic data with lifecycle classification

    Does NOT render Markdown. Markdown generation is Phase H (render_markdown),
    which is not in the main graph flow.

    Args:
        unified_report_output: Unified reporter output dict (None if failed).
        output_dir: Directory to write JSON files (e.g. "data/processed/{date}").
        date: Target date in YYYYMMDD format.
        custom_tables_config: Custom table configurations dict, or None.
            Format: {"engineering": {"enabled": True/False, "config": {...}}, ...}

    Returns:
        Dict mapping file names to their written Path objects.
        e.g. {"daily": Path("data/processed/20260520/daily.json"), ...}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    # CT-3 (generalized): resolve which custom-table kinds to write.
    # None/empty config → backward-compat default ["engineering"].
    enabled_kinds = _resolve_enabled_table_kinds(custom_tables_config)

    # M4: 为自定义表记录注入确定性 {kind}_id（in-place）—— JSON 写入 + MemOS enqueue 共用。
    inject_custom_record_ids(unified_report_output, enabled_kinds)

    if unified_report_output is None:
        # Write empty placeholder files for mandatory tables + enabled custom tables.
        _placeholder_names = ["daily", "resources", "topics", *enabled_kinds]
        for name in _placeholder_names:
            p = output_dir / f"{name}.json"
            _safe_write_json(p, {"date": date, "placeholder": True})
            written[name] = p
        logger.warning(
            "compose_json: unified_report_output is None — wrote %d placeholders",
            len(written),
        )
        return written

    # --- daily.json (mandatory) ---
    daily_data = _extract_daily_data(unified_report_output, date)
    daily_path = output_dir / "daily.json"
    _safe_write_json(daily_path, daily_data)
    written["daily"] = daily_path

    # --- resources.json (mandatory) ---
    resources_data = _extract_resources_data(unified_report_output, date)
    resources_path = output_dir / "resources.json"
    _safe_write_json(resources_path, resources_data)
    written["resources"] = resources_path

    # --- custom tables (generic: one {kind}.json per enabled table) ---
    for kind in enabled_kinds:
        table_data = _extract_table_data(unified_report_output, kind, date)
        table_path = output_dir / f"{kind}.json"
        _safe_write_json(table_path, table_data)
        written[kind] = table_path
    if enabled_kinds:
        logger.info("compose_json: wrote custom tables %s", enabled_kinds)

    # --- topics.json (mandatory) ---
    topics_data = _extract_topics_data(unified_report_output, date)
    topics_path = output_dir / "topics.json"
    _safe_write_json(topics_path, topics_data)
    written["topics"] = topics_path

    # Quality check on unified data (non-blocking)
    try:
        composed = _dict_to_composed(unified_report_output, date, custom_tables_config)
        score, warnings_list = quality_check(composed)
        logger.info(
            "compose_json: quality score=%.0f, %d warnings, %d files written",
            score,
            len(warnings_list),
            len(written),
        )
    except Exception as exc:
        logger.debug("compose_json: quality check skipped (%s)", exc)

    return written


# ============================================================
# Phase H: render_markdown — read L3 JSON → render Markdown (T-W12-3)
# ============================================================
# NOT in main graph flow. Called manually for user-triggered export.


def render_markdown(
    json_dir: str | Path,
    template_dir: str | Path | None = None,
    *,
    template_name: str = "daily_report",
) -> Path:
    """Phase H: Read L3 JSON files and render a Markdown report.

    Reads the 4 L3 JSON files from json_dir, composes them into a
    ComposedData instance, renders via Jinja2 template, and writes
    the Markdown output to the same directory.

    This function is NOT called in the main graph flow. It is intended
    for manual/user-triggered export after review (S4).

    Args:
        json_dir: Directory containing the 4 L3 JSON files.
        template_dir: Optional Jinja2 template directory (unused — uses built-in).
        template_name: Template name for rendering, e.g. "daily_report".

    Returns:
        Path to the written Markdown file.
    """
    json_dir = Path(json_dir)

    # Read the mandatory L3 JSON files
    daily_data = _safe_read_json(json_dir / "daily.json")
    resources_data = _safe_read_json(json_dir / "resources.json")
    engineering_data = _safe_read_json(json_dir / "engineering.json")
    topics_data = _safe_read_json(json_dir / "topics.json")

    # Read every registered custom table's L3 file (engineering, world_models, …)
    # for generic Markdown rendering. Missing files are simply absent.
    from z_winnow.custom_tables import registry as _ct_reg

    custom_table_data: dict[str, dict[str, Any]] = {}
    for tdef in _ct_reg.get_all_tables():
        tbl = _safe_read_json(json_dir / f"{tdef.id}.json")
        if isinstance(tbl, dict):
            custom_table_data[tdef.id] = tbl

    # Merge into ComposedData for rendering
    composed = _merge_json_to_composed(
        daily_data,
        resources_data,
        engineering_data,
        topics_data,
        custom_table_data=custom_table_data,
    )

    # Render via Jinja2
    final_md = render_composed(composed, template_name=template_name)

    # Markdown syntax check
    md_errors = check_markdown_syntax(final_md)
    if md_errors:
        logger.warning("render_markdown: %d syntax warnings", len(md_errors))

    # Write Markdown output
    md_path = json_dir / "report.md"
    md_path.write_text(final_md, encoding="utf-8")

    logger.info(
        "render_markdown: wrote %s (%d chars)",
        md_path,
        len(final_md),
    )
    return md_path


# ============================================================
# compose_json helpers — data extraction
# ============================================================


def _extract_daily_data(unified: dict[str, Any], date: str) -> dict[str, Any]:
    """Extract daily.json structure from unified reporter output."""
    return {
        "date": date,
        "overview": unified.get("overview", ""),
        "important_notice": unified.get("important_notice", ""),
        "topics": unified.get("topics", []),
        "trend_analysis": unified.get("trend_analysis", ""),
        "trend_summary": unified.get("trend_summary", ""),
        "highlights": unified.get("highlights", []),
    }


def _extract_resources_data(unified: dict[str, Any], date: str) -> dict[str, Any]:
    """Extract resources.json structure from unified reporter output."""
    resources = unified.get("resources", [])
    return {
        "date": date,
        "resources": resources,
        "count_by_type": unified.get("resource_count_by_type", {}),
        "total_count": len(resources),
    }


def _build_server_id_map(messages: list[dict[str, Any]] | None) -> dict[str, tuple[str, str]]:
    """Build {server_id: (local_path, local_url)} index from messages."""
    local_map: dict[str, tuple[str, str]] = {}
    if not messages:
        return local_map
    for m in messages:
        if not isinstance(m, dict):
            continue
        sid = str(m.get("server_id") or "")
        p = m.get("media_local_path")
        if sid and p and Path(str(p)).is_file():
            try:
                parts = Path(str(p)).parts
                aidx = parts.index("attachments")
                gid, dt, fn = parts[aidx - 2], parts[aidx - 1], parts[aidx + 1]
                url = f"/api/v1/attachments/{gid}/{dt}/{fn}"
            except (ValueError, IndexError):
                url = ""
            local_map[sid] = (str(p), url)
    return local_map


def _scan_attachments_dir(attachments_dir: Path) -> list[tuple[str, str]]:
    """Scan attachments directory and return [(absolute_path, local_url), ...].

    Only returns entries for files that actually exist on disk.
    Sorted so that longer filenames come first (more specific matches).
    """
    if not attachments_dir.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    try:
        parts = attachments_dir.parts
        aidx = parts.index("attachments")
        gid, dt = parts[aidx - 2], parts[aidx - 1]
    except (ValueError, IndexError):
        return []
    for f in attachments_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        url = f"/api/v1/attachments/{gid}/{dt}/{name}"
        entries.append((str(f), url))
    # Longer filenames first — more specific, less likely to false-match
    entries.sort(key=lambda x: -len(Path(x[0]).name))
    return entries


def _compute_content_hash(file_path: str, n_chars: int = 8) -> str:
    """Compute SHA256 of a file and return first *n_chars* hex chars.

    Returns empty string on I/O error.  Cached per file path for performance
    (the same SMB file may be checked against multiple resources).
    """
    import hashlib

    # Module-level cache: {file_path: hash_prefix}
    if not hasattr(_compute_content_hash, "_cache"):
        _compute_content_hash._cache = {}  # type: ignore[attr-defined]
    cache: dict[str, str] = _compute_content_hash._cache  # type: ignore[attr-defined]
    if file_path in cache:
        return cache[file_path]
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                hasher.update(chunk)
        result = hasher.hexdigest()[:n_chars]
    except OSError:
        result = ""
    cache[file_path] = result
    return result


def _resource_matches_filename(resource: dict[str, Any], filename: str) -> bool:
    """Check whether a resource entry matches a local filename.

    Supports two forms of resource filenames:
      1. Plain name — ``报告.pdf`` → exact match against SMB ``报告.pdf``
      2. Hash-suffixed — ``报告_a1b2c3d4.pdf`` → strip ``_{8hex}`` suffix,
         match base name ``报告.pdf`` against SMB filename.

    Also strips hash prefix/suffix from already-cached files for Level 2 matching.
    Substring / fuzzy matching is deliberately avoided.
    """
    from z_winnow.content_enrich.file_dedup import strip_hash_suffix

    candidates: list[str] = []
    title = str(resource.get("resource_title", "") or "").strip()
    content = str(resource.get("content", "") or "").strip()
    if title:
        candidates.append(title)
    if content and content != title:
        candidates.append(content)

    if not candidates:
        return False

    # Build alternative representations of the SMB filename
    fname_raw = filename
    fname_stem = Path(filename).stem
    bare_raw = filename
    stem = Path(filename).stem
    # Strip _{8-hex} suffix from already-cached files
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and len(parts[1]) == 8:
        try:
            int(parts[1], 16)
            bare_raw = parts[0] + Path(filename).suffix
        except ValueError:
            pass
    # Also strip old 12-hex prefix format for backward compat
    if bare_raw == filename and "_" in stem and len(stem.split("_")[0]) == 12:
        try:
            int(stem.split("_")[0], 16)
            bare_raw = "_".join(stem.split("_")[1:]) + Path(filename).suffix
        except ValueError:
            pass
    bare_stem = Path(bare_raw).stem

    fname_variants = {
        fname_raw.lower(),
        fname_stem.lower(),
        bare_raw.lower(),
        bare_stem.lower(),
    }

    for cand in candidates:
        cand_norm = cand.lower().strip()
        if not cand_norm:
            continue
        cand_stem = cand_norm.rsplit(".", 1)[0] if "." in cand_norm else cand_norm

        # Exact match (with / without extension) against SMB filename variants
        if cand_norm in fname_variants:
            return True
        if cand_stem in fname_variants:
            return True

        # If candidate carries a hash suffix (from file_dedup), strip it and
        # try matching the base name against SMB filename variants.
        base_name, _hash = strip_hash_suffix(cand_norm)
        if base_name != cand_norm:
            base_stem = base_name.rsplit(".", 1)[0] if "." in base_name else base_name
            if base_name in fname_variants or base_stem in fname_variants:
                return True

    return False


def _load_attachment_index(attachments_dir: Path) -> dict[str, Any]:
    """Load (or create empty) attachments/index.json."""
    idx_path = attachments_dir / "index.json"
    if idx_path.is_file():
        data = _safe_read_json(idx_path)
        if isinstance(data, dict):
            return data
    return {"files": {}}


def _update_attachment_index(
    attachments_dir: Path,
    *,
    hash_name: str,
    original_name: str,
    content_sha256: str,
    size_bytes: int,
    source_group: str = "",
    source_date: str = "",
) -> None:
    """Record a cached file's origin in attachments/index.json.

    Idempotent: if the hash entry already exists, only adds the new
    source_group/source_date to the ``seen_in`` list.
    """
    import datetime

    idx = _load_attachment_index(attachments_dir)
    files = idx.setdefault("files", {})
    now_iso = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    entry = files.get(hash_name)
    if entry is None:
        entry = {
            "original_name": original_name,
            "content_sha256": content_sha256,
            "size_bytes": size_bytes,
            "first_cached_at": now_iso,
            "seen_in": [],
        }
        files[hash_name] = entry

    # Record this group+date occurrence (dedup)
    occurrence = f"{source_group}/{source_date}" if source_group else ""
    seen: list[str] = entry.setdefault("seen_in", [])
    if occurrence and occurrence not in seen:
        seen.append(occurrence)
        seen.sort()
        entry["last_seen_at"] = now_iso

    _safe_write_json(attachments_dir / "index.json", idx)


def _copy_ext_file(
    src_path: str,
    original_name: str,
    attachments_dir: Path,
    resource: dict[str, Any],
) -> None:
    """Copy an external file into attachments/ with hash naming + index update.

    Stores as ``{原名}_{sha256[:8]}.{ext}``, updates ``index.json``, and
    sets ``resource["local_path"]`` / ``resource["local_url"]``.
    """
    import hashlib
    import re as _re
    import shutil

    attachments_dir.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    fsize = 0
    with open(src_path, "rb") as _f:
        while True:
            chunk = _f.read(1 << 20)
            if not chunk:
                break
            hasher.update(chunk)
            fsize += len(chunk)
    digest = hasher.hexdigest()
    hshort = digest[:8]
    stem = Path(original_name).stem
    ext = Path(original_name).suffix.lower()
    safe_stem = _re.sub(r'[\\/\x00-\x1f<>:"|?*]+', "_", stem).strip(" ._")
    if not safe_stem:
        safe_stem = "file"
    safe_stem = safe_stem[:120]
    dest_name = f"{safe_stem}_{hshort}{ext}"
    dest_path = attachments_dir / dest_name
    if not dest_path.exists():
        shutil.copy2(src_path, dest_path)
        logger.info("patch_resources: copied %s → %s", original_name, dest_name)
    try:
        parts = dest_path.parts
        aidx = parts.index("attachments")
        _gid, _dt, _fn = parts[aidx - 2], parts[aidx - 1], parts[aidx + 1]
        resource["local_url"] = f"/api/v1/attachments/{_gid}/{_dt}/{_fn}"
    except (ValueError, IndexError):
        _gid, _dt = "", ""
    resource["local_path"] = str(dest_path)
    _update_attachment_index(
        attachments_dir,
        hash_name=dest_name,
        original_name=original_name,
        content_sha256=digest,
        size_bytes=fsize,
        source_group=_gid,
        source_date=_dt,
    )


def patch_resources_local_path(
    resources_path: Path,
    messages: list[dict[str, Any]] | None,
    *,
    external_dirs: list[str] | None = None,
) -> int:
    """#9.3: 按 source_server_ids 或文件名匹配, 回填 resource["local_path"]。

    三级匹配策略:
      1. **server_id 精确匹配** — 通过 resource.source_server_ids 在
         message.media_local_path 中查找 (content_enrich 阶段下载的媒体)。
      2. **本地 attachments/ 文件名模糊匹配** — 扫描 attachments/ 目录,
         用 resource_title / content 与文件名做归一化子串匹配。
      3. **外部目录文件名匹配 + 自动拷贝** — 扫描 external_dirs 中按
         ``YYYY-MM/`` 分月组织的微信文件存储目录, 匹配到时拷贝到
         attachments/ 并设置 local_path。

    同时生成 local_url — 可从浏览器直接访问的本地 API 地址:
      /api/v1/attachments/{group_id}/{date}/{filename}

    best-effort: 文件不存在 / 解析失败 均跳过, 不抛异常。
    返回回填了 local_path 的 resource 数。
    """
    if not resources_path.exists():
        return 0
    data = _safe_read_json(resources_path)
    if not isinstance(data, dict):
        return 0

    # Level 1: server_id → message.media_local_path
    sid_map = _build_server_id_map(messages)

    # Level 2: scan attachments directory (for files not tracked in messages)
    # M4 版本化: resources.json 在 {date}/v{n}/resources.json, 但 attachments 是
    # per-date 的 {date}/attachments (content_enrich media_downloader 落盘处).
    # 从 v{n}/ 子目录往上跳一层, 否则 attachments_dir 偏移 → Level 2 扫不到已落盘
    # 文件、_copy_ext_file 拷到错位目录、local_url 把 date/v1 当作 gid/date.
    _res_parent = resources_path.parent
    if _res_parent.name.startswith("v") and _res_parent.parent.name:
        attachments_dir = _res_parent.parent / "attachments"
    else:
        attachments_dir = _res_parent / "attachments"
    dir_entries = _scan_attachments_dir(attachments_dir)

    # Level 3: scan external directories (WeChat file storage via SMB, etc.)
    # Build flat list of (abs_path, basename) from YYYY-MM/ subdirs.
    ext_entries: list[tuple[str, str]] = []
    if external_dirs:
        for ed in external_dirs:
            ep = Path(ed)
            if not ep.is_dir():
                continue
            # Walk YYYY-MM/ month dirs
            try:
                for child in ep.iterdir():
                    if not child.is_dir():
                        continue
                    # Match YYYY-MM pattern (e.g. "2026-06")
                    if len(child.name) != 7 or child.name[4] != "-":
                        continue
                    for f in child.iterdir():
                        if f.is_file():
                            ext_entries.append((str(f), f.name))
            except OSError:
                continue
        # Longer filenames first
        ext_entries.sort(key=lambda x: -len(x[1]))

    if not sid_map and not dir_entries and not ext_entries:
        return 0

    resources = data.get("resources")
    if not isinstance(resources, list):
        return 0

    patched = 0
    for r in resources:
        if not isinstance(r, dict):
            continue
        # Already has a valid local_path → skip
        if r.get("local_path") and Path(str(r["local_path"])).is_file():
            continue

        matched = False

        # ── Level 1: server_id exact match ──
        sids = r.get("source_server_ids")
        if isinstance(sids, list) and sid_map:
            for sid in sids:
                if str(sid) in sid_map:
                    lp, lu = sid_map[str(sid)]
                    r["local_path"] = lp
                    if lu:
                        r["local_url"] = lu
                    patched += 1
                    matched = True
                    break

        # ── Level 2: local attachments filename fuzzy match ──
        if not matched and dir_entries:
            for epath, eurl in dir_entries:
                if _resource_matches_filename(r, Path(epath).name):
                    r["local_path"] = epath
                    r["local_url"] = eurl
                    patched += 1
                    matched = True
                    break

        # ── Level 3: external dir filename match + copy to attachments ──
        # resource.content may carry a hash suffix from content_enrich file_dedup
        # (e.g. "报告_a1b2c3d4.pdf").  Match strategy:
        #   1. Exact name match (strip hash suffix before comparing).
        #   2. If the resource has a hash suffix, verify content hash of the
        #      matched SMB file.  If mismatch, fall through to hash-verified
        #      search across base-name candidates.
        if not matched and ext_entries:
            from z_winnow.content_enrich.file_dedup import strip_hash_suffix

            ext_entries.sort(key=lambda x: -len(x[1]))
            # Collect resource names + detect hash suffix
            _rc_names: list[str] = []
            for _field in ("resource_title", "content"):
                _v = str(r.get(_field, "") or "").strip()
                if _v:
                    _rc_names.append(_v)

            # Determine the expected hash from resource (if any)
            _res_hash = ""
            _res_base = ""
            for _rc in _rc_names:
                _b, _h = strip_hash_suffix(_rc)
                if _h:
                    _res_hash = _h
                    _res_base = _b
                    break

            # Level 3a: exact filename match
            for esrc, ename in ext_entries:
                if _resource_matches_filename(r, ename):
                    # If resource has a hash suffix, verify content hash
                    if _res_hash:
                        _actual = _compute_content_hash(esrc)[:8]
                        if _actual != _res_hash:
                            continue  # hash mismatch, try next candidate
                    _copy_ext_file(esrc, ename, attachments_dir, r)
                    patched += 1
                    matched = True
                    break

            # Level 3b: hash-verified base-name search
            if not matched and _res_hash and _res_base:
                _base_stem = Path(_res_base).stem.lower()
                for _esrc, _ename in ext_entries:
                    _n_stem = Path(_ename).stem.lower()
                    if _n_stem != _base_stem and not _n_stem.startswith(_base_stem + "("):
                        continue
                    _actual = _compute_content_hash(_esrc)[:8]
                    if _actual == _res_hash:
                        _copy_ext_file(_esrc, _ename, attachments_dir, r)
                        patched += 1
                        matched = True
                        break

    if patched:
        _safe_write_json(resources_path, data)
    return patched


def inject_custom_record_ids(
    unified: dict[str, Any] | None,
    enabled_kinds: list[str],
) -> None:
    """M4: 为每个自定义表记录注入确定性 ``{kind}_id``（in-place 改 unified）。

    供 reports UI 反馈 target_id 与 feedback_memory search 定位 node 使用。
    ``id = f"{kind}_{md5(record)[:10]}"`` —— 内容不变则跨版本稳定（同一问题在
    v1/v2 同 id，反馈可跨版本定位）。compose_json 调用本函数后，写出的
    ``{kind}.json`` 与主图 output_composer 节点的 MemOS enqueue（读同一 unified
    对象）都能拿到 id。

    Args:
        unified: unified_reporter 输出 dict（将被原地修改）；None 则 no-op。
        enabled_kinds: 已解析的激活自定义表 kind 列表。
    """
    if not unified or not enabled_kinds:
        return
    import hashlib

    from z_winnow.custom_tables import registry as ct_registry

    ct = unified.get("custom_tables")
    if not isinstance(ct, dict):
        return
    for kind in enabled_kinds:
        slot = ct.get(kind)
        if not isinstance(slot, dict):
            continue
        tdef = ct_registry.get_table(kind)
        rkey = tdef.records_key if tdef else "items"
        records = slot.get(rkey)
        if not isinstance(records, list):
            continue
        for rec in records:
            if isinstance(rec, dict) and not rec.get(f"{kind}_id"):
                payload = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
                h = hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()[:10]
                rec[f"{kind}_id"] = f"{kind}_{h}"


def _resolve_enabled_table_kinds(custom_tables_config: dict[str, Any] | None) -> list[str]:
    """Resolve the ordered list of custom-table kinds to write to L3.

    Driven by the YAML registry (custom tables), not the feishu TABLE_CATALOG — a
    table registered but not pushed to feishu still gets an L3 file. ``None``/empty
    config → backward-compat default of ``["engineering"]`` (legacy default-on).
    """
    from z_winnow.custom_tables import registry as ct_registry

    if not custom_tables_config:
        return ["engineering"] if ct_registry.get_table("engineering") else []
    return [
        kind
        for kind, cfg in custom_tables_config.items()
        if isinstance(cfg, dict) and cfg.get("enabled") and ct_registry.get_table(kind)
    ]


def _extract_table_data(unified: dict[str, Any], kind: str, date: str) -> dict[str, Any]:
    """Extract ``{kind}.json`` from the custom_tables slot (generic).

    Slot shape = the table's YAML ``output_schema`` shape (records_key / summary_key),
    written verbatim. ``date`` + ``model_used`` are added as report-level metadata.
    """
    slot = (unified.get("custom_tables") or {}).get(kind)
    if not isinstance(slot, dict):
        slot = {}
    data: dict[str, Any] = {"date": date, "model_used": unified.get("model_used", "")}
    data.update(slot)
    return data


def _extract_topics_data(
    unified: dict[str, Any],
    date: str,
) -> dict[str, Any]:
    """Extract topics.json structure from unified_reporter output.

    T-W13: Topics come from the unified topics[] list with lifecycle classification.
    No separate topic_tracker agent or topic_reports parameter needed.
    """
    topics = unified.get("topics", [])

    # Count by lifecycle
    lifecycle_counts: dict[str, int] = {}
    for t in topics:
        if isinstance(t, dict):
            lc = t.get("lifecycle", "emerging")
            lifecycle_counts[lc] = lifecycle_counts.get(lc, 0) + 1

    return {
        "date": date,
        "topics": topics,
        "trend_summary": unified.get("trend_summary", ""),
        "lifecycle_counts": lifecycle_counts,
        "total_active": sum(
            1 for t in topics if isinstance(t, dict) and t.get("status") == "active"
        ),
        "total_count": len(topics),
    }


def _dict_to_composed(
    data: dict[str, Any],
    date: str = "",
    custom_tables_config: dict[str, Any] | None = None,
) -> ComposedData:
    """Convert unified reporter dict to ComposedData for quality checks.

    W16-A2: fault-tolerant consumption of unified_reporter's strongly-typed
    models — dict → model → ``model_dump()`` → dict, loaded into ComposedData.
    ComposedData.topics/resources/issues remain ``list[dict]`` so downstream
    quality.py / renderer.py / merger.py keep using dict-style ``t.get(...)``
    access unchanged.

    CT-3: Passes custom_tables_config through to ComposedData.

    A026: Topic / Resource / EngineeringIssue are the SINGLE field-definition
    point (models.py) — composer does not redefine any fields. L037: per-item
    error isolation — a single bad element falls back to a default instance
    without aborting the batch.

    Tolerance boundary (P045, cross-contract W16-A1): the three L3-record
    models use ``extra='allow'`` with all-default fields, so legacy on-disk
    fields (``topic_sections``, ``legacy_*``, ...) are absorbed as a backward-
    compatible passthrough (NO ValidationError, NO fallback). Only a TRUE
    type-mismatch / constraint ValidationError triggers the default-instance
    fallback branch below.
    """
    topics_out: list[dict[str, Any]] = []
    for item in data.get("topics", []):
        if not isinstance(item, dict):
            # L037: non-dict element → default instance, isolate failure
            topics_out.append(Topic().model_dump())
            continue
        try:
            # A026: Topic is the single topic field-definition source of truth
            topics_out.append(Topic(**item).model_dump())
        except ValidationError:
            # L037: per-item isolation → default instance, don't break batch
            topics_out.append(Topic().model_dump())

    resources_out: list[dict[str, Any]] = []
    for item in data.get("resources", []):
        if not isinstance(item, dict):
            resources_out.append(Resource().model_dump())
            continue
        try:
            resources_out.append(Resource(**item).model_dump())
        except ValidationError:
            resources_out.append(Resource().model_dump())

    issues_out: list[dict[str, Any]] = []
    _eng_slot = (data.get("custom_tables") or {}).get("engineering") or {}
    for item in _eng_slot.get("issues", []) if isinstance(_eng_slot, dict) else []:
        if not isinstance(item, dict):
            issues_out.append(EngineeringIssue().model_dump())
            continue
        try:
            issues_out.append(EngineeringIssue(**item).model_dump())
        except ValidationError:
            issues_out.append(EngineeringIssue().model_dump())

    _eng_summary = _eng_slot.get("group_summary", {}) if isinstance(_eng_slot, dict) else {}

    # custom_table_data: every table's slot data, for generic Markdown rendering.
    _ct_slot = data.get("custom_tables")
    _ct_data = _ct_slot if isinstance(_ct_slot, dict) else {}

    return ComposedData(
        date=date,
        overview=data.get("overview", ""),
        important_notice=data.get("important_notice", ""),
        topics=topics_out,
        trend_analysis=data.get("trend_analysis", ""),
        trend_summary=data.get("trend_summary", ""),
        highlights=data.get("highlights", []),
        resources=resources_out,
        resource_count_by_type=data.get("resource_count_by_type", {}),
        issues=issues_out,
        group_summary=_eng_summary,
        custom_tables=custom_tables_config,
        custom_table_data=_ct_data,
    )


def _merge_json_to_composed(
    daily: dict[str, Any],
    resources: dict[str, Any],
    engineering: dict[str, Any],
    topics: dict[str, Any],
    custom_table_data: dict[str, dict[str, Any]] | None = None,
) -> ComposedData:
    """Merge L3 JSON dicts back into ComposedData for render_markdown.

    ``custom_table_data`` carries every custom table's L3 file contents (keyed by
    kind) for generic Markdown rendering; None → empty.
    """
    ct_data = custom_table_data if isinstance(custom_table_data, dict) else {}
    return ComposedData(
        date=daily.get("date", ""),
        overview=daily.get("overview", ""),
        important_notice=daily.get("important_notice", ""),
        topics=daily.get("topics", []),
        trend_analysis=daily.get("trend_analysis", ""),
        trend_summary=daily.get("trend_summary", ""),
        highlights=daily.get("highlights", []),
        resources=resources.get("resources", []),
        resource_count_by_type=resources.get("count_by_type", {}),
        issues=engineering.get("engineering_issues") or engineering.get("issues", []),
        group_summary=engineering.get("group_summary", {}),
        custom_table_data=ct_data,
    )


def _safe_write_json(filepath: Path, data: dict[str, Any]) -> None:
    """Write JSON safely — never raises. A008: data checked before write."""
    # A008: explicit check before write
    to_write: Any = data if isinstance(data, dict) else {}
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(to_write, ensure_ascii=False, indent=2, default=str)
        filepath.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception("compose_json: failed to write %s", filepath)


def _safe_read_json(filepath: Path) -> dict[str, Any]:
    """Read JSON safely — returns empty dict on any error. A008."""
    # A008: explicit init
    data: dict[str, Any] = {}
    try:
        if filepath.exists():
            raw = filepath.read_text(encoding="utf-8")
            if raw.strip():
                data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        logger.debug("compose_json: failed to read %s", filepath)
    return data


__all__ = [
    "check_markdown_syntax",
    "compose_json",
    "quality_check",
    "render_composed",
    "render_markdown",
]
