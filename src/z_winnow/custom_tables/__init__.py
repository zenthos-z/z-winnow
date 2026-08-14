"""Custom tables — 插件化自定义表系统。

架构：
- TableDefinition: 表定义（字段 schema、飞书映射、前端组件）
- SkillDefinition: 技能定义（处理逻辑的 prompt/agent_workflow）
- registry: 注册表（表定义 + 技能绑定的统一注册入口）

使用方式：
    from z_winnow.custom_tables.registry import get_table, get_active_tables
    tdef = get_table("engineering")
    if tdef:
        prompts = get_active_tables_prompts({"engineering": {"enabled": True}})
"""

from z_winnow.custom_tables.base import SkillDefinition, TableDefinition
from z_winnow.custom_tables.registry import (
    get_active_tables_prompts,
    get_all_tables,
    get_skill_prompt,
    get_table,
    register_table,
)

__all__ = [
    "SkillDefinition",
    "TableDefinition",
    "get_active_tables_prompts",
    "get_all_tables",
    "get_skill_prompt",
    "get_table",
    "register_table",
]
