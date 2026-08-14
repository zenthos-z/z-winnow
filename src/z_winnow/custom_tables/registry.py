"""自定义表注册表 — 管理所有注册的表定义和技能。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from z_winnow.custom_tables.base import SkillDefinition, TableDefinition

logger = logging.getLogger(__name__)

# 全局注册表
_REGISTRY: dict[str, TableDefinition] = {}
_SKILLS: dict[str, SkillDefinition] = {}

# YAML 文件所在目录（相对于本文件）
_TABLES_DIR = Path(__file__).parent / "tables"
_SKILLS_DIR = Path(__file__).parent / "skills"


def register_table(defn: TableDefinition) -> None:
    """注册一个表定义到全局注册表。"""
    if defn.id in _REGISTRY:
        logger.warning("Table %s already registered, overwriting", defn.id)
    _REGISTRY[defn.id] = defn
    logger.debug("Registered table: %s (skill=%s)", defn.id, defn.skill_id)


def register_skill(defn: SkillDefinition) -> None:
    """注册一个技能定义到全局注册表。"""
    _SKILLS[defn.id] = defn
    logger.debug("Registered skill: %s v%s", defn.id, defn.version)


def get_table(table_id: str) -> TableDefinition | None:
    """获取表定义，不存在返回 None。"""
    return _REGISTRY.get(table_id)


def get_skill(skill_id: str) -> SkillDefinition | None:
    """获取技能定义，不存在返回 None。"""
    return _SKILLS.get(skill_id)


def get_skill_prompt(skill_id: str) -> str | None:
    """获取技能的 prompt 内容。"""
    skill = _SKILLS.get(skill_id)
    if skill and skill.type == "prompt":
        return skill.prompt
    return None


def get_all_tables() -> list[TableDefinition]:
    """获取所有注册的表定义列表。"""
    return list(_REGISTRY.values())


def get_active_tables_prompts(custom_tables_config: dict[str, Any] | None) -> list[str]:
    """根据群组配置，生成激活表的 prompt 片段列表。

    Args:
        custom_tables_config: 群组的 custom_tables blob，如 {"engineering": {"enabled": true}}

    Returns:
        每个 prompt 片段的列表，可直接拼接到 unified_reporter system prompt
    """
    if not custom_tables_config:
        return []

    prompts = []
    for table_id, config in custom_tables_config.items():
        if not isinstance(config, dict) or not config.get("enabled"):
            continue

        table_def = get_table(table_id)
        if not table_def:
            logger.warning("Unknown table_id in config: %s", table_id)
            continue

        skill_prompt = get_skill_prompt(table_def.skill_id)
        if not skill_prompt:
            logger.warning("No prompt for skill %s (table %s)", table_def.skill_id, table_id)
            continue

        # 输出契约：告诉 LLM 把本表结果放进 custom_tables 槽位的哪个键下。
        # 槽位形状 = 该表 YAML output_schema 形状（records_key / summary_key）。
        rk = table_def.records_key or "items"
        if table_def.summary_key:
            shape = f'{{"_empty": false, "{rk}": [...], "{table_def.summary_key}": {{...}}}}'
        else:
            shape = f'{{"_empty": false, "{rk}": [...]}}'
        prompts.append(
            f"### 表: {table_def.name}\n"
            f"技能: {table_def.skill_id}\n"
            f"调用规则:\n{skill_prompt}\n\n"
            f"**输出契约（极其重要）**:\n"
            f"- 将本表结果放入 JSON 顶层 custom_tables.{table_id} 下，结构为 {shape}。\n"
            f"- **有相关内容时**：_empty 设为 false，{rk} 填正常数据。\n"
            f"- **无相关内容时（必须输出！）**：_empty 设为 true，{rk} 设为空数组 []，"
            f"{table_def.summary_key + ' 设为空对象 {}' if table_def.summary_key else ''}"
            f"（系统用 _empty=true 区分「模型检查过但无内容」vs「模型忽略了此表」——漏输出此表会导致排查困难）。"
        )

    return prompts


def _load_table_from_yaml(path: Path) -> TableDefinition | None:
    """从 YAML 文件加载表定义。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            return None

        return TableDefinition(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            skill_id=data.get("skill_id", ""),
            output_schema=data.get("output_schema", {}),
            feishu_fields=data.get("feishu_fields"),
            frontend_component=data.get("frontend_component", "table"),
            enabled_by_default=data.get("enabled_by_default", False),
            mandatory=data.get("mandatory", False),
            records_key=data.get("records_key", "items"),
            summary_key=data.get("summary_key"),
            markdown_columns=data.get("markdown_columns"),
        )
    except Exception:
        logger.exception("Failed to load table from %s", path)
        return None


def _load_skill_from_yaml(path: Path) -> SkillDefinition | None:
    """从 YAML 文件加载技能定义。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            return None

        return SkillDefinition(
            id=data.get("id", ""),
            name=data.get("name", ""),
            version=data.get("version", "1.0.0"),
            type=data.get("type", "prompt"),
            prompt=data.get("prompt", ""),
        )
    except Exception:
        logger.exception("Failed to load skill from %s", path)
        return None


def auto_register_builtin() -> None:
    """启动时自动扫描并注册所有内置表和技能。"""
    # 先注册技能（表定义依赖技能）
    if _SKILLS_DIR.exists():
        for skill_file in _SKILLS_DIR.glob("*.yaml"):
            skill_def = _load_skill_from_yaml(skill_file)
            if skill_def and skill_def.id:
                register_skill(skill_def)

    # 再注册表
    if _TABLES_DIR.exists():
        for table_file in _TABLES_DIR.glob("*.yaml"):
            table_def = _load_table_from_yaml(table_file)
            if table_def and table_def.id:
                register_table(table_def)

    logger.info(
        "Auto-registered %d tables, %d skills",
        len(_REGISTRY),
        len(_SKILLS),
    )


# 模块加载时自动注册
auto_register_builtin()
