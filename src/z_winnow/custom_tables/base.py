"""自定义表的基础数据类。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillDefinition:
    """技能定义 — 描述一个表的数据如何被处理。

    id: 唯一标识，与 TableDefinition.skill_id 对应
    name: 中文名，如"工程问题提取器"
    version: 版本号，如 "1.0.0"
    type: 处理类型
        - "prompt": 纯 prompt 片段，注入到 unified_reporter 提示词
        - "agent_workflow": 独立的 agent 工作流（未来扩展）
    prompt: 当 type="prompt" 时的 prompt 文本
    """

    id: str
    name: str
    version: str = "1.0.0"
    type: str = "prompt"
    prompt: str = ""


@dataclass
class TableDefinition:
    """表定义 — 描述一个自定义表的元数据。

    id: 唯一标识，如 "engineering"
    name: 中文名，如 "工程问题"
    description: 说明用途
    skill_id: 绑定的技能 ID，与 SkillDefinition.id 对应
    output_schema: JSON Schema 输出格式
    feishu_fields: 飞书字段定义列表，None 表示不同步到飞书
    frontend_component: 前端组件类型
        - "table": 表格渲染
        - 后续可扩展: "card", "chart", "map"
    enabled_by_default: 新群组默认是否启用
    mandatory: 是否强制启用（群组不能关闭）
    records_key: 记录数组在 custom_tables 槽位 / L3 JSON 里的键名
        （engineering→"issues"，world_models→"items"）。槽位形状 = output_schema 形状。
    summary_key: 摘要对象的键名（"group_summary"/"school_summary"），None 表示无 summary。
    """

    id: str
    name: str
    description: str = ""
    skill_id: str = ""
    output_schema: dict[str, Any] = field(default_factory=dict)
    feishu_fields: list[dict[str, Any]] | None = None
    frontend_component: str = "table"
    enabled_by_default: bool = False
    mandatory: bool = False
    # records_key: 该表记录数组在槽位/L3 JSON 中的键名（如 engineering→"issues"，
    # world_models→"items"）。槽位形状 = YAML output_schema 形状，compose 原样落盘。
    records_key: str = "items"
    # summary_key: 该表分组/流派摘要对象的键名（如 "group_summary"/"school_summary"），
    # None 表示该表无 summary 结构。
    summary_key: str | None = None
    # markdown_columns: Markdown 渲染时的列定义 {record_key: 中文列名}，None 则按记录
    # 键自动生成列。是各表"渲染组件"的自描述配置，让导出报告更易读。
    markdown_columns: dict[str, str] | None = None
