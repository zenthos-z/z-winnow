"""Feishu Bitable field schemas + Layer-3 → row mappers.

Defines the **new-form** table framework created for each group's Base and the
mappers that turn Layer-3 JSON (``data/processed/{group}/{date}/*.json``) into
column-oriented rows for ``lark-cli base +record-batch-create``.

Decisions (confirmed with user 2026-07-10, grounded in the real template Base
``XgifbiqYMag2k4srFF4cPb5qnwe`` — see memory feishu-base-template):

- **日报改结构化**：旧表「内容=attachment」（整份报告当文件传，手动适配）→ 新版拆成
  「日报汇总」(1 行/天) + 「议题明细」(1 行/议题)，内容可筛选可检索。
- **不纳入**「信息源表格」+ 工程表「父记录」关联（保持扁平，v1 先跑通主流程）。
- **工程表按群可选**（``feishu_engineering_enabled``）。
- 资源/工程表字段对齐用户真实 Base（命中），仅去掉附件类字段（自动化不传附件）。

lark-cli record format is **column-oriented**: ``{"fields": [colnames],
"rows": [[v1, v2, ...], ...]}``. Each mapper below returns exactly that shape.
CellValue conventions: datetime → ``"YYYY-MM-DD HH:mm:ss"``; select → option
label string; multi-select → list of label strings; number → float; text → str.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Type alias for an L3→row mapper: (l3_json_dict, date) -> (column_names, rows).
RowMapper = Callable[[dict[str, Any], str], tuple[list[str], list[list[Any]]]]

# ============================================================
# Enum label maps (L3 English/code → Chinese select labels)
# ============================================================

LIFECYCLE_LABELS: dict[str, str] = {
    "emerging": "新兴",
    "sustained": "持续",
    "user_defined": "自定义",
}
TOPIC_STATUS_LABELS: dict[str, str] = {
    "active": "进行中",
    "resolved": "已解决",
    "fading": "衰退",
    "dormant": "休眠",
}
ENG_STATUS_DESC_LABELS: dict[str, str] = {
    "待解决": "待解决",
    "已解决": "已解决",
    "跟进中": "跟进中",
    "阻塞": "阻塞",
    # 常见同义词归一到飞书单选选项（防止 LLM 产出变体值导致整批 record 被拒）
    "已修复": "已解决",
    "修复中": "跟进中",
    "处理中": "跟进中",
    "进行中": "跟进中",
}
_LIFECYCLE_OPTIONS = [LIFECYCLE_LABELS[k] for k in ("emerging", "sustained", "user_defined")] + [
    "其他"
]
_TOPIC_STATUS_OPTIONS = [
    TOPIC_STATUS_LABELS[k] for k in ("active", "resolved", "fading", "dormant")
]
_RESOURCE_TAG_OPTIONS = ["链接", "工具", "文档", "插件", "其他"]

# resource_type → 飞书标签 自动推导（LLM 不产出 tags 字段，上游字段名是 resource_type）
_RESOURCE_TYPE_TO_TAG: dict[str, str] = {
    "link": "链接",
    "site": "链接",
    "paper": "文档",
    "article": "文档",
    "doc": "文档",
    "repo": "工具",
    "image": "其他",
    "file": "其他",
    "other": "其他",
}
_ENG_GROUP_OPTIONS = ["开发与调试工具", "部署与基础设施", "其他"]
_ENG_STATUS_OPTIONS = ["待解决", "已解决", "跟进中", "阻塞"]
# 世界大模型动态 — 流派（按机制/公式分类）+ 信号类型枚举
_WM_SCHOOL_OPTIONS = ["生成派", "推理派", "结构派", "仿真派", "跨流派"]
_WM_SIGNAL_OPTIONS = ["突破进展", "评测对比", "观点质疑", "趋势预测", "应用案例"]


def _opt(names: list[str]) -> list[dict[str, str]]:
    return [{"name": n} for n in names]


# ============================================================
# Table field schemas (lark-cli --fields JSON shape)
# ============================================================

DAILY_SUMMARY_FIELDS: list[dict[str, Any]] = [
    {"name": "日期", "type": "datetime"},
    {"name": "概述", "type": "text"},
    {"name": "重点提醒", "type": "text"},
    {"name": "趋势总结", "type": "text"},
    {"name": "议题数", "type": "number"},
    # Attachment fields — populated via +record-upload-attachment (not batch_create).
    # 日报文档: rendered daily Markdown (.md). 图片: generated cover image (TBD).
    {"name": "日报文档", "type": "attachment"},
    {"name": "图片", "type": "attachment"},
]

TOPIC_DETAIL_FIELDS: list[dict[str, Any]] = [
    {"name": "日期", "type": "datetime"},
    {"name": "议题名称", "type": "text"},
    {"name": "生命周期", "type": "select", "options": _opt(_LIFECYCLE_OPTIONS)},
    {"name": "状态", "type": "select", "options": _opt(_TOPIC_STATUS_OPTIONS)},
    # 短列在前便于阅览；背景/进展为长文本，移到末尾。
    {"name": "结论", "type": "text"},
    {"name": "趋势", "type": "text"},
    {"name": "参与人", "type": "text"},
    {"name": "权重", "type": "number"},
    {"name": "背景", "type": "text"},
    {"name": "进展", "type": "text"},
]

RESOURCE_FIELDS: list[dict[str, Any]] = [
    {"name": "发布日期", "type": "datetime"},
    {"name": "资源标题", "type": "text"},
    {"name": "标签", "type": "select", "multiple": True, "options": _opt(_RESOURCE_TAG_OPTIONS)},
    {"name": "简介", "type": "text"},
    {"name": "具体内容", "type": "text"},
    {"name": "分享人", "type": "text"},
    # 文件类资源（不是链接、需上传文件）走附件字段；具体内容仍存链接。
    {"name": "附件", "type": "attachment"},
]

ENGINEERING_FIELDS: list[dict[str, Any]] = [
    {"name": "日期", "type": "datetime"},
    {"name": "问题分组", "type": "select", "options": _opt(_ENG_GROUP_OPTIONS)},
    {"name": "问题描述", "type": "text"},
    {"name": "解决方案", "type": "text"},
    {"name": "关键操作/工具", "type": "text"},
    {"name": "状态", "type": "select", "options": _opt(_ENG_STATUS_OPTIONS)},
    {"name": "状态描述", "type": "text"},
    {"name": "信息来源", "type": "text"},
]

WORLD_MODELS_FIELDS: list[dict[str, Any]] = [
    {"name": "日期", "type": "datetime"},
    {"name": "流派", "type": "select", "options": _opt(_WM_SCHOOL_OPTIONS)},
    {"name": "模型/系统", "type": "text"},
    {"name": "核心要点", "type": "text"},
    {"name": "进展详述", "type": "text"},
    {"name": "信号类型", "type": "select", "options": _opt(_WM_SIGNAL_OPTIONS)},
    {"name": "意义/影响", "type": "text"},
    {"name": "信息来源", "type": "text"},
]

# ============================================================
# Date / value helpers
# ============================================================


def _norm_date(date: str) -> str:
    """YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD → ``YYYY-MM-DD``."""
    cleaned = (date or "").strip().replace("-", "").replace("/", "")
    if len(cleaned) == 8 and cleaned.isdigit():
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    return (date or "").strip()[:10]


def _dt(date: str, time_part: str = "00:00:00") -> str:
    """lark-cli datetime CellValue: ``YYYY-MM-DD HH:mm:ss``."""
    return f"{_norm_date(date)} {time_part}"


def _s(v: Any) -> str:
    """Coerce to string, treating None as empty."""
    if v is None:
        return ""
    return str(v)


def _join_participants(p: Any) -> str:
    """participants may be a list of names or list of dicts."""
    if not p:
        return ""
    if isinstance(p, str):
        return p
    out: list[str] = []
    for item in p:
        if isinstance(item, dict):
            out.append(_s(item.get("name") or item.get("wxid") or ""))
        else:
            out.append(_s(item))
    return ", ".join(x for x in out if x)


# ============================================================
# L3 → row mappers (return (columns, rows) in column-oriented form)
# ============================================================


def daily_summary_rows(daily: dict[str, Any], date: str) -> tuple[list[str], list[list[Any]]]:
    """日报汇总: 1 row/day from daily.json."""
    topics = daily.get("topics") or daily.get("topic_sections") or []
    row = [
        _dt(date),
        _s(daily.get("overview")),
        _s(daily.get("important_notice")),
        _s(daily.get("trend_summary")),
        float(len(topics)) if isinstance(topics, list) else 0.0,
    ]
    return ["日期", "概述", "重点提醒", "趋势总结", "议题数"], [row]


def topic_detail_rows(daily: dict[str, Any], date: str) -> tuple[list[str], list[list[Any]]]:
    """议题明细: 1 row/topic from daily.json topics[]/topic_sections[]."""
    topics = daily.get("topics") or daily.get("topic_sections") or []
    columns = [
        "日期",
        "议题名称",
        "生命周期",
        "状态",
        "结论",
        "趋势",
        "参与人",
        "权重",
        "背景",
        "进展",
    ]
    rows: list[list[Any]] = []
    for t in topics:
        if not isinstance(t, dict):
            continue
        weight = t.get("weight")
        lifecycle = LIFECYCLE_LABELS.get(_s(t.get("lifecycle")), _s(t.get("lifecycle"))) or ""
        if lifecycle not in _LIFECYCLE_OPTIONS:
            lifecycle = "其他"
        status = TOPIC_STATUS_LABELS.get(_s(t.get("status")), _s(t.get("status"))) or ""
        if status not in _TOPIC_STATUS_OPTIONS:
            status = "进行中"
        rows.append(
            [
                _dt(date),
                _s(t.get("topic_name") or t.get("title")),
                lifecycle,
                status,
                _s(t.get("conclusion")),
                _s(t.get("trend")),
                _join_participants(t.get("participants")),
                float(weight) if isinstance(weight, (int, float)) else None,
                _s(t.get("background")),
                _s(t.get("process")),
            ]
        )
    return columns, rows


def resource_rows(resources_data: dict[str, Any], date: str) -> tuple[list[str], list[list[Any]]]:
    """资源: 1 row/resource from resources.json."""
    columns = ["发布日期", "资源标题", "标签", "简介", "具体内容", "分享人"]
    items = resources_data.get("resources") or []
    if not isinstance(items, list):
        return columns, []
    rows: list[list[Any]] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        rt = (r.get("resource_type") or "").strip().lower()
        tag = _RESOURCE_TYPE_TO_TAG.get(rt)
        tags = [tag] if tag else []
        rows.append(
            [
                _dt(date),
                _s(r.get("resource_title")),
                tags,
                _s(r.get("summary")),
                _s(r.get("content")),
                _s(r.get("shared_by")),
            ]
        )
    return columns, rows


def engineering_rows(eng_data: dict[str, Any], date: str) -> tuple[list[str], list[list[Any]]]:
    """工程问题: 1 row/issue from engineering.json issues[]."""
    columns = [
        "日期",
        "问题分组",
        "问题描述",
        "解决方案",
        "关键操作/工具",
        "状态",
        "状态描述",
        "信息来源",
    ]
    items = eng_data.get("engineering_issues") or eng_data.get("issues") or []
    if not isinstance(items, list):
        return columns, []
    rows: list[list[Any]] = []
    for i in items:
        if not isinstance(i, dict):
            continue
        desc = _s(i.get("status_desc"))
        status_label = ENG_STATUS_DESC_LABELS.get(desc, desc) or "待解决"
        # 兜底：映射后仍不在飞书单选选项内 → 回落到"待解决"，避免单条非法值让整批被拒
        if status_label not in _ENG_STATUS_OPTIONS:
            status_label = "待解决"
        # Prefer the issue's own datetime when present (e.g. "2026-07-09 11:08").
        d = _s(i.get("datetime")) or date
        rows.append(
            [
                _dt(d) if "-" in _s(i.get("datetime")) else _dt(date),
                _s(i.get("group")),
                _s(i.get("description")),
                _s(i.get("solution")),
                _s(i.get("key_operations")),
                status_label,
                desc,
                _s(i.get("source_members")),
            ]
        )
    return columns, rows


def world_models_rows(wm_data: dict[str, Any], date: str) -> tuple[list[str], list[list[Any]]]:
    """世界大模型动态: 1 row/item from world_models.json items[]."""
    columns = [
        "日期",
        "流派",
        "模型/系统",
        "核心要点",
        "进展详述",
        "信号类型",
        "意义/影响",
        "信息来源",
    ]
    items = wm_data.get("items") or []
    if not isinstance(items, list):
        return columns, []
    rows: list[list[Any]] = []
    for i in items:
        if not isinstance(i, dict):
            continue
        # 流派/信号兜底回落到合法单选选项，避免单条非法值让整批 record 被拒
        school = _s(i.get("school"))
        if school not in _WM_SCHOOL_OPTIONS:
            school = "跨流派"
        signal = _s(i.get("signal"))
        if signal not in _WM_SIGNAL_OPTIONS:
            signal = "趋势预测"
        d = _s(i.get("datetime")) or date
        rows.append(
            [
                _dt(d) if "-" in _s(i.get("datetime")) else _dt(date),
                school,
                _s(i.get("model_or_system")),
                _s(i.get("topic")),
                _s(i.get("progress")),
                signal,
                _s(i.get("significance")),
                _s(i.get("source_members")),
            ]
        )
    return columns, rows


# ============================================================
# Table catalog (pluggable per-group table set, #9.4)
# ============================================================


@dataclass(frozen=True)
class L3Source:
    """Where a table's rows come from: which L3 JSON file + which mapper.

    ``l3_key`` is one of the keys returned by :func:`load_l3`
    (``daily`` / ``resources`` / ``engineering``).
    """

    l3_key: str
    mapper: RowMapper


# Post-create attachment hooks an enabled table opts into. Declared per table
# so the uploader stays kind-agnostic (no ``if kind == "summary"`` branches).
#   "daily_md"       — render daily Markdown → 日报文档 attachment (summary)
#   "cover"          — attach pre-generated cover.png → 图片 (summary)
#   "resource_files" — upload local_path files → 附件 (resources)
AttachmentHook = str


@dataclass(frozen=True)
class TableDef:
    """A pluggable table kind in the catalog.

    Attributes:
        kind: stable config key (``summary``/``topics``/``resources``/...). Used
            as the key in the per-group ``feishu_tables`` blob and the catalog.
        display_name: human label shown in the UI and used as the Bitable table
            name when creating the framework.
        fields: lark-cli field schema (``--fields`` JSON).
        source: L3 data source (file key + row mapper). Tables with no natural
            data source (future manual tables) may pass a mapper returning [].
        mandatory: if True the table is always created+uploaded for every group
            (the core spine: 议题/资源/日报汇总). Optional kinds are per-group.
        attachments: post-create hooks this table opts into (see AttachmentHook).
        default_enabled: whether the table is enabled by default for a NEW group
            when the group hasn't expressed a preference. Mandatory kinds ignore
            this (always on).
    """

    kind: str
    display_name: str
    fields: list[dict[str, Any]]
    source: L3Source
    mandatory: bool = False
    attachments: tuple[AttachmentHook, ...] = ()
    default_enabled: bool = False


# The catalog is the single source of truth for the table set. Adding a new
# table kind = append one entry here (+ its field schema + mapper); the uploader
# and framework init pick it up automatically — no other code changes.
# Order matters: the first kind is created inline with base_create; the rest via
# table_create. Mandatory kinds (the core spine) come first.
TABLE_CATALOG: dict[str, TableDef] = {
    "summary": TableDef(
        kind="summary",
        display_name="日报汇总",
        fields=DAILY_SUMMARY_FIELDS,
        source=L3Source("daily", daily_summary_rows),
        mandatory=True,
        attachments=("daily_md", "cover"),
    ),
    "topics": TableDef(
        kind="topics",
        display_name="议题明细",
        fields=TOPIC_DETAIL_FIELDS,
        source=L3Source("daily", topic_detail_rows),
        mandatory=True,
    ),
    "resources": TableDef(
        kind="resources",
        display_name="资源",
        fields=RESOURCE_FIELDS,
        source=L3Source("resources", resource_rows),
        mandatory=True,
        attachments=("resource_files",),
    ),
    "engineering": TableDef(
        kind="engineering",
        display_name="工程问题",
        fields=ENGINEERING_FIELDS,
        source=L3Source("engineering", engineering_rows),
        mandatory=False,
        default_enabled=False,  # #7.1: independent toggle via group.engineering_enabled
    ),
    "world_models": TableDef(
        kind="world_models",
        display_name="世界大模型动态",
        fields=WORLD_MODELS_FIELDS,
        source=L3Source("world_models", world_models_rows),
        mandatory=False,
        default_enabled=False,
    ),
}

# Kinds every group gets, regardless of its per-group selection.
MANDATORY_KINDS: frozenset[str] = frozenset(k for k, d in TABLE_CATALOG.items() if d.mandatory)
# Kinds that are on by default for a brand-new group (mandatory + opt-in default).
DEFAULT_ENABLED_KINDS: frozenset[str] = MANDATORY_KINDS | frozenset(
    k for k, d in TABLE_CATALOG.items() if d.default_enabled
)


def active_kinds(
    tables_config: dict[str, Any] | None,
    custom_tables_config: dict[str, Any] | None = None,
) -> list[str]:
    """Mandatory kinds + kinds the group explicitly enabled, in catalog order.

    Unknown keys in ``tables_config`` (not in the catalog) are ignored — they may
    be future kinds this older code doesn't know about.

    Args:
        tables_config: Per-group feishu_tables blob ``{kind: {enabled, table_id}}``.
        custom_tables_config: Per-group custom_tables blob ``{kind: {enabled, config}}``.
            When provided, overrides the legacy tables_config for corresponding kinds.
            Currently only supports "engineering" kind.

    Returns:
        List of active table kinds in catalog order.
    """
    enabled: set[str] = set(MANDATORY_KINDS)

    # Process custom_tables_config first (higher priority)
    # CT-5: custom_tables.engineering.enabled controls engineering table
    if custom_tables_config and isinstance(custom_tables_config, dict):
        for kind, cfg in custom_tables_config.items():
            if kind in TABLE_CATALOG and isinstance(cfg, dict) and cfg.get("enabled"):
                enabled.add(kind)
            elif kind in TABLE_CATALOG and isinstance(cfg, dict) and not cfg.get("enabled"):
                # Explicitly disabled in custom_tables config
                enabled.discard(kind)

    # Process legacy tables_config (lower priority, skip kinds already in custom_tables)
    for kind, cfg in (tables_config or {}).items():
        # Skip if already configured via custom_tables_config
        if custom_tables_config and kind in custom_tables_config:
            continue
        if kind in TABLE_CATALOG and isinstance(cfg, dict) and cfg.get("enabled"):
            enabled.add(kind)

    return [k for k in TABLE_CATALOG if k in enabled]


def kind_enabled_for_report(
    kind: str,
    custom_tables: dict[str, Any] | None,
    feishu_tables: dict[str, Any] | None,
    legacy: Any = False,
) -> bool:
    """Resolve whether a custom table should appear in a group's *report*.

    Generic single source of truth for the display + generation layers. Priority:

      1. ``custom_tables.<kind>.enabled`` — UI authoritatively writes this
         (custom_tables is the single source of truth; feishu_tables is derived).
      2. ``feishu_tables.<kind>.enabled`` — the toggle the UI historically wrote
         before custom_tables existed.
      3. ``legacy`` — e.g. the deprecated ``engineering_enabled`` column for
         engineering (DEFAULT 1, never UI-updated, last-resort default).
      4. ``False`` — matches the YAML ``default_enabled: false``.

    The Feishu *push* path uses :func:`active_kinds` instead.
    """
    ct = custom_tables if isinstance(custom_tables, dict) else {}
    ct_entry = ct.get(kind)
    if isinstance(ct_entry, dict) and "enabled" in ct_entry:
        return bool(ct_entry["enabled"])
    ft = feishu_tables if isinstance(feishu_tables, dict) else {}
    ft_entry = ft.get(kind)
    if isinstance(ft_entry, dict) and "enabled" in ft_entry:
        return bool(ft_entry["enabled"])
    return bool(legacy)


def engineering_enabled_for_report(
    custom_tables: dict[str, Any] | None,
    feishu_tables: dict[str, Any] | None,
    legacy_engineering_enabled: Any = False,
) -> bool:
    """Backward-compat wrapper: resolve engineering via :func:`kind_enabled_for_report`.

    ``legacy_engineering_enabled`` is the deprecated ``groups.engineering_enabled``
    column value (DEFAULT 1, never UI-updated).
    """
    return kind_enabled_for_report(
        "engineering", custom_tables, feishu_tables, legacy_engineering_enabled
    )


def table_cfg(tables_config: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    """Read one kind's per-group config with safe defaults.

    Returns ``{"enabled": bool, "table_id": str}``. Missing entries default to
    enabled for mandatory kinds, disabled otherwise; missing table_id → "".
    """
    cfg = (tables_config or {}).get(kind)
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", kind in MANDATORY_KINDS)),
        "table_id": str(cfg.get("table_id") or ""),
    }


def default_tables_config() -> dict[str, dict[str, Any]]:
    """A fresh per-group blob for a brand-new group (DEFAULT_ENABLED_KINDS on)."""
    return {k: {"enabled": k in DEFAULT_ENABLED_KINDS, "table_id": ""} for k in TABLE_CATALOG}


# ============================================================
# L3 file loading
# ============================================================


def load_l3(l3_dir: Path, date: str, *, version_number: int | None = None) -> dict[str, dict[str, Any]]:
    """Load the Layer-3 JSON files for a group/date.

    Returns a dict keyed by kind (``daily``/``resources``/``engineering``/
    ``world_models``); missing files are simply absent from the dict.

    M4: ``version_number`` 可显式指定读某版本（回滚后推 active 版本用）；None=最新 v{n}/。
    """
    norm = (date or "").strip().replace("-", "").replace("/", "")
    # M4: 版本化目录回退 — l3_dir = layer3_root/group_id；解析 v{n}/，回退扁平 date 目录
    from z_winnow.pipeline.l3_paths import resolve_l3_dir

    resolved = resolve_l3_dir(l3_dir.parent, l3_dir.name, norm, version_number=version_number)
    out: dict[str, dict[str, Any]] = {}
    for kind, fname in (
        ("daily", "daily.json"),
        ("resources", "resources.json"),
        ("engineering", "engineering.json"),
        ("world_models", "world_models.json"),
    ):
        for candidate in (resolved / fname, l3_dir / fname):
            if candidate.exists():
                try:
                    out[kind] = json.loads(candidate.read_text(encoding="utf-8"))
                    break
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read L3 %s: %s", candidate, exc)
    return out


__all__ = [
    "DAILY_SUMMARY_FIELDS",
    "DEFAULT_ENABLED_KINDS",
    "ENGINEERING_FIELDS",
    "MANDATORY_KINDS",
    "RESOURCE_FIELDS",
    "TABLE_CATALOG",
    "TOPIC_DETAIL_FIELDS",
    "WORLD_MODELS_FIELDS",
    "L3Source",
    "TableDef",
    "active_kinds",
    "daily_summary_rows",
    "default_tables_config",
    "engineering_enabled_for_report",
    "engineering_rows",
    "kind_enabled_for_report",
    "load_l3",
    "resource_rows",
    "table_cfg",
    "topic_detail_rows",
    "world_models_rows",
]
