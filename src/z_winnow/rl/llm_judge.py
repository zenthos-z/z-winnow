"""LLM-as-judge — Claude Haiku 评分引擎 (T-W10-B-a).

使用 Claude Haiku 4.5 对群日报报告进行 4 维度质量评估:
  - completeness (完整性): 是否覆盖所有必要章节和议题
  - accuracy (准确性): 内容是否事实正确、逻辑合理
  - conciseness (简洁性): 是否冗余、篇幅适中
  - actionability (可操作性): 结论是否可执行、任务是否明确

Architecture (P014: 异常安全解析器):
  内层 _parse_judge_json() → 纯解析, 可能抛异常
  外层 _safe_parse_judge_json() → try/except 兜底, NEVER 抛异常

P007: json_mode 优先 (response_format={"type":"json_object"})
  → 解析失败则 fallback 到 invoke + 手动提取 JSON

Reference:
  docs/wave10-design.md §4.2 — judge_report 接口定义
  docs/experiences/patterns/P014.md — 异常安全解析器架构
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from z_winnow.config.models import create_model

logger = logging.getLogger(__name__)

# ============================================================
# Data Models
# ============================================================


class ReportVersion(BaseModel):
    """报告版本 — judge_report 的输入。

    Attributes:
        content: 报告全文 (Markdown 格式)。
        version_id: 版本标识 (如 "v1", "v2-after-edit")。
        group_id: 群聊标识。
        date: 报告日期 (YYYYMMDD)。
    """

    content: str = Field(description="报告全文 (Markdown 格式)")
    version_id: str = Field(default="v1", description="版本标识")
    group_id: str = Field(default="", description="群聊标识")
    date: str = Field(default="", description="报告日期 YYYYMMDD")


class DimensionScore(BaseModel):
    """单个维度的评分结果。

    Attributes:
        score: 评分 [0.0, 1.0]。
        evidence: 引用报告原文的证据。
    """

    score: float = Field(ge=0.0, le=1.0, description="维度评分 [0.0, 1.0]")
    evidence: str = Field(default="", description="引用报告原文的证据")


class JudgeResult(BaseModel):
    """LLM-as-judge 完整评分结果。

    Attributes:
        completeness: 完整性评分。
        accuracy: 准确性评分。
        conciseness: 简洁性评分。
        actionability: 可操作性评分。
        overall: 4 维度平均分。
        model_used: 评分使用的模型标识。
        judge_at: ISO 8601 评分时间戳。
        error: 解析失败时为 True。
        raw_response: 解析失败时保留原始 LLM 回复。
    """

    completeness: DimensionScore = Field(
        default_factory=lambda: DimensionScore(score=0.0, evidence="")
    )
    accuracy: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0.0, evidence=""))
    conciseness: DimensionScore = Field(
        default_factory=lambda: DimensionScore(score=0.0, evidence="")
    )
    actionability: DimensionScore = Field(
        default_factory=lambda: DimensionScore(score=0.0, evidence="")
    )
    overall: float = Field(default=0.0, ge=0.0, le=1.0, description="4 维度平均分")
    model_used: str = Field(default="", description="评分模型标识")
    judge_at: str = Field(default="", description="ISO 8601 评分时间戳")
    error: bool = Field(default=False, description="解析失败标记")
    raw_response: str = Field(default="", description="解析失败时保留原始 LLM 回复")


# ============================================================
# Prompt Templates
# ============================================================

JUDGE_SYSTEM_PROMPT = """你是一个群日报质量评审专家。你的任务是对给定的群聊日报进行 4 维度评分。

## 评分维度

1. **completeness (完整性, 0-1)**:
   - 是否覆盖了当天所有重要议题？
   - 是否包含概览、议题详情、讨论趋势等必要章节？
   - 是否遗漏关键人物的重要发言？
   - 1.0 = 完全覆盖，0.0 = 严重缺失

2. **accuracy (准确性, 0-1)**:
   - 事实描述是否正确？（议题归属、参与群成员、结论）
   - 时间线是否合理？
   - 是否存在明显的事实错误或张冠李戴？
   - 1.0 = 完全准确，0.0 = 大量错误

3. **conciseness (简洁性, 0-1)**:
   - 是否冗余啰嗦？篇幅是否适中？
   - 是否存在非必要的重复内容？
   - 表述是否清晰精炼？
   - 1.0 = 非常简洁，0.0 = 严重冗余

4. **actionability (可操作性, 0-1)**:
   - 是否有明确的待办事项和行动建议？
   - 结论是否具体可执行？
   - 工程问题的负责人和状态是否明确？
   - 1.0 = 高度可操作，0.0 = 无任何行动指引

## 输出格式

你必须只返回一个 JSON 对象，格式如下：
```json
{
  "completeness": {"score": 0.85, "evidence": "报告覆盖了X、Y议题..."},
  "accuracy": {"score": 0.90, "evidence": "议题归属正确..."},
  "conciseness": {"score": 0.75, "evidence": "第3段有轻微冗余..."},
  "actionability": {"score": 0.60, "evidence": "工程问题缺少负责人..."}
}
```

每个维度的 evidence 必须引用报告中的具体内容，不超过 80 字。
score 必须是 0.0 到 1.0 之间的浮点数。
只返回 JSON，不要输出任何其他文字。
"""

JUDGE_USER_PROMPT = """请对以下群日报进行 4 维度评分。

<report>
{report_content}
</report>
"""

JUDGE_USER_PROMPT_WITH_REFERENCE = """请对以下群日报进行 4 维度评分。

<reference_report>
以下是一份已标记为金标准的报告，供你参考对比：
{reference_content}
</reference_report>

<report>
{report_content}
</report>
"""

# ============================================================
# JSON Parsing (P014: 内层纯解析 + 外层安全包装)
# ============================================================


def _parse_judge_json(raw_text: str) -> dict[str, Any]:
    """内层纯解析 — 从 LLM 回复中提取 JSON。

    P014: 纯解析层, 可能抛出异常; 由外层 _safe_parse_judge_json 兜底。
    A008: data 变量在 try 前初始化。

    Args:
        raw_text: LLM 原始回复文本。

    Returns:
        解析后的 dict, 含 completeness/accuracy/conciseness/actionability。

    Raises:
        ValueError: JSON 解析失败。
    """
    # A008: 初始化 data = None, 避免全层失败时 NameError
    data: Any = None

    text = raw_text.strip()

    # 策略 1: 直接 json.loads (如果 LLM 返回纯 JSON)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 策略 2: 提取 ```json ... ``` 代码块
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block_match:
        try:
            data = json.loads(code_block_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # 策略 3: 提取最外层 { ... } 大括号包裹的 JSON
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            data = json.loads(brace_match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # 全部策略失败 — 抛出异常, 由外层兜底
    raise ValueError(f"Failed to extract valid JSON from LLM response: {text[:200]}")


def _safe_parse_judge_json(raw_text: str) -> tuple[dict[str, Any] | None, bool, str]:
    """外层安全包装 — NEVER 抛异常。

    P014: try/except 包裹 _parse_judge_json, 解析失败返回 error flag + raw_response。

    Args:
        raw_text: LLM 原始回复文本。

    Returns:
        (parsed_dict | None, error_flag, raw_response_for_debug)
    """
    try:
        parsed = _parse_judge_json(raw_text)
        return parsed, False, ""
    except Exception as e:
        logger.warning("JSON parse failed: %s", e)
        return None, True, raw_text


def _build_result_from_parsed(
    parsed: dict[str, Any],
    dimensions: list[str] | None,
    model_used: str,
    judge_at: str,
) -> JudgeResult:
    """从解析后的 dict 构建 JudgeResult。

    P039: 仅做结构性验证, 不做精确值断言 (LLM 输出天然 non-deterministic)。

    Args:
        parsed: _parse_judge_json 返回的 dict。
        dimensions: 请求的维度列表 (None = 全部 4 维度)。
        model_used: 模型标识。
        judge_at: ISO 时间戳。

    Returns:
        JudgeResult 实例。
    """
    all_dimensions = ["completeness", "accuracy", "conciseness", "actionability"]
    active_dims = dimensions if dimensions is not None else all_dimensions

    result = JudgeResult(model_used=model_used, judge_at=judge_at)
    scores: list[float] = []

    for dim in active_dims:
        dim_data = parsed.get(dim, {})
        if isinstance(dim_data, dict):
            score_val = dim_data.get("score", 0.0)
            evidence_val = dim_data.get("evidence", "")
        else:
            score_val = 0.0
            evidence_val = ""

        # 边界钳制
        score_val = max(0.0, min(1.0, float(score_val)))

        dim_score = DimensionScore(score=score_val, evidence=str(evidence_val))
        setattr(result, dim, dim_score)
        scores.append(score_val)

    # overall = 活跃维度平均
    result.overall = round(sum(scores) / len(scores), 4) if scores else 0.0

    return result


# ============================================================
# Core Function — judge_report
# ============================================================


async def judge_report(
    report: ReportVersion,
    reference: ReportVersion | None = None,
    dimensions: list[str] | None = None,
) -> JudgeResult:
    """用 Claude Haiku 对报告进行 4 维度质量评分。

    接口定义: docs/wave10-design.md §4.2

    Args:
        report: 待评估的报告版本。
        reference: 金标准报告 (管理员标 5 星或 inline_edit 后版本), 可选。
        dimensions: 要评分的维度列表, 默认全部 4 维度。
                   可传 ["completeness", "conciseness"] 等子集。

    Returns:
        JudgeResult 含各维度评分 + overall + model_used + judge_at。
        解析失败时 error=True + raw_response 写回, 不抛异常 (B3)。
    """
    all_dimensions = frozenset({"completeness", "accuracy", "conciseness", "actionability"})

    # 验证 dimensions
    if dimensions is not None:
        invalid = [d for d in dimensions if d not in all_dimensions]
        if invalid:
            raise ValueError(f"Invalid dimensions: {invalid}. Valid: {sorted(all_dimensions)}")

    # 生成时间戳
    judge_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017

    # 模型 — DeepSeek v4 Flash
    model_used = "deepseek-v4-flash"

    # --- 构建 prompt ---
    system_prompt = JUDGE_SYSTEM_PROMPT

    if reference is not None and reference.content.strip():
        user_prompt = JUDGE_USER_PROMPT_WITH_REFERENCE.format(
            reference_content=reference.content,
            report_content=report.content,
        )
    else:
        user_prompt = JUDGE_USER_PROMPT.format(report_content=report.content)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # --- 调用 LLM (P007: json_mode 优先 → invoke+parse fallback) ---
    raw_text: str = ""

    try:
        # 创建模型实例
        llm = create_model(
            "deepseek",
            model_name=model_used,
            temperature=0.1,
            max_tokens=2048,
        )

        # Primary: json_mode via response_format
        try:
            if hasattr(llm, "bind"):
                json_llm = llm.bind(response_format={"type": "json_object"})
            else:
                json_llm = llm

            result = await json_llm.ainvoke(messages)
            raw_text = str(result.content) if hasattr(result, "content") else str(result)
            logger.info("json_mode succeeded (length=%d)", len(raw_text))

            # P014: 安全解析 JSON
            parsed, is_error, raw_response = _safe_parse_judge_json(raw_text)
            if is_error:
                return JudgeResult(
                    model_used=model_used,
                    judge_at=judge_at,
                    error=True,
                    raw_response=raw_response,
                )

            return _build_result_from_parsed(parsed, dimensions, model_used, judge_at)  # type: ignore[arg-type]

        except Exception as e:
            logger.warning("json_mode LLM call failed: %s. Falling back to invoke+parse.", e)

        # Fallback: plain invoke + manual parse
        result = await llm.ainvoke(messages)
        raw_text = str(result.content) if hasattr(result, "content") else str(result)
        logger.info("invoke+parse fallback (length=%d)", len(raw_text))

        # P014: 安全解析
        parsed, is_error, raw_response = _safe_parse_judge_json(raw_text)
        if is_error:
            return JudgeResult(
                model_used=model_used,
                judge_at=judge_at,
                error=True,
                raw_response=raw_response,
            )

        return _build_result_from_parsed(parsed, dimensions, model_used, judge_at)  # type: ignore[arg-type]

    except Exception as e:
        # 最外层兜底 — 连 LLM 调用本身都失败时
        logger.error("LLM judge call completely failed: %s", e)
        return JudgeResult(
            model_used=model_used,
            judge_at=judge_at,
            error=True,
            raw_response=f"LLM call error: {e}",
        )
