"""M4: L3 版本化目录路径解析.

output_composer 写 ``data/processed/{group_id}/{date}/v{version_number}/``。
本模块统一解析"某版本/当前的 L3 目录"，带扁平路径回退（旧数据或未版本化场景），
让读侧在 v{n}/ 与历史扁平路径之间平滑过渡。

典型用法::

    from z_winnow.pipeline.l3_paths import resolve_l3_dir, read_l3_json
    d = resolve_l3_dir(settings.layer3_output_dir, group_id, date)          # 当前/最新版本
    d = resolve_l3_dir(settings.layer3_output_dir, group_id, date, version_number=2)  # 指定版本
    topics = read_l3_json(settings.layer3_output_dir, group_id, date, "topics", version_number=2)
"""

from __future__ import annotations

import json
from pathlib import Path


def versioned_l3_dir(
    layer3_root: str | Path,
    group_id: str,
    date: str,
    version_number: int,
) -> Path:
    """返回确定版本号的 L3 目录（不检查存在性）。

    纯路径构造，供写侧（output_composer）与需要精确版本的读侧（provenance/regenerate）使用。
    """
    return Path(layer3_root) / group_id / date / f"v{version_number}"


def resolve_l3_dir(
    layer3_root: str | Path,
    group_id: str,
    date: str,
    *,
    version_number: int | None = None,
) -> Path:
    """解析 L3 目录，带回退。

    优先级：
      1. 显式 ``version_number`` → ``v{version_number}/``（存在时）
      2. 已存在的最大 ``v{n}/``（"当前/最新版本"目录）
      3. 扁平 ``{group_id}/{date}/``（旧数据回退）

    Args:
        layer3_root: L3 输出根目录（settings.layer3_output_dir）。
        group_id: 群组 ID。
        date: 日期 YYYYMMDD。
        version_number: 可选显式版本号；None=取最新版本目录。

    Returns:
        解析后的目录 Path（可能不存在——调用方按需 .exists() 检查）。
    """
    base = Path(layer3_root) / group_id / date

    if version_number is not None:
        explicit = base / f"v{version_number}"
        if explicit.exists():
            return explicit

    if base.exists():
        versions: list[int] = []
        for p in base.iterdir():
            if p.is_dir() and len(p.name) > 1 and p.name.startswith("v") and p.name[1:].isdigit():
                versions.append(int(p.name[1:]))
        if versions:
            return base / f"v{max(versions)}"

    return base


def read_l3_json(
    layer3_root: str | Path,
    group_id: str,
    date: str,
    kind: str,
    *,
    version_number: int | None = None,
) -> dict | None:
    """读取某版本的 ``{kind}.json``（daily/topics/resources/{table_id}），带回退。

    Args:
        kind: 文件名（不含 .json），如 "daily" / "topics" / "engineering"。
        version_number: 可选显式版本号；None=最新版本目录。

    Returns:
        解析后的 dict，或 None（文件缺失/解析失败）。
    """
    directory = resolve_l3_dir(layer3_root, group_id, date, version_number=version_number)
    path = directory / f"{kind}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
