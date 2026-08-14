"""unified_reporter agent — single LLM call replaces 3 separate subagents.

P002: Agent factory + DI — llm/memos_adapter injected via closure.
P007: json_mode primary → invoke+parse fallback — multi-model compatibility.
P014: Three-strategy JSON parsing (direct → code fence → regex).
P010: Mock mode shortcut to deterministic mock output.

Replaces: daily_reporter + resource_extractor + engineering_analyzer + topic_tracker.
topics[] is the unified topic list with lifecycle classification (user_defined|sustained|emerging).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from z_winnow.config.models import create_model_for_subagent
from z_winnow.graph.error_handling import NodeError
from z_winnow.subagents.unified_reporter.models import UnifiedReporterOutput
from z_winnow.subagents.unified_reporter.prompt import (
    SHORT_MESSAGES_APPENDIX,
    build_prompt,
    build_system_prompt,
)

logger = logging.getLogger(__name__)

# 5min 超时策略：实测正常生成（137 条消息 6 合 1 日报）~196s 即可完成（with_retry 成功案例）。
# DeepSeek 偶发卡顿会撑满超时仍不返回 —— 设短超时让它快速失败，交给节点级 with_retry 重试，
# 第二次调用通常快速成功。比"干等卡住的调用 600s"更省时。
# 与 L012（重任务给 1200s）区别：那是"真慢"，这里是"卡住"，用快速失败+重试更合适。
DEFAULT_TIMEOUT_SECONDS = 300
MAX_RETRIES = 2


# ============================================================
# OutputParseError
# ============================================================


class OutputParseError(Exception):
    """Raised when LLM output cannot be parsed into UnifiedReporterOutput."""

    def __init__(self, message: str, raw_text: str = "") -> None:
        self.raw_text: str = raw_text
        preview = raw_text[:500] if raw_text else "(empty)"
        super().__init__(f"{message}. Raw output preview: {preview}")


# ============================================================
# source_server_ids Validation — P014 + L005
# ============================================================


def validate_source_server_ids(output: UnifiedReporterOutput) -> list[str]:
    """Validate source_server_ids across all L3 record types.

    P014: Warn on empty arrays but do NOT crash the whole batch.
    Logs warnings for each record with empty source_server_ids.
    Returns list of warning messages (for testing introspection).

    L005: source_server_ids must reference actual message serverId values.

    W16-A1: ``topics`` / ``resources`` are strongly typed models (Topic /
    Resource) where ``source_server_ids`` is a ``list[str]`` with
    ``default_factory=list``. Custom-table records live in the generic
    ``custom_tables`` slot (plain dicts); each table's record array key is
    declared via ``TableDefinition.records_key``. Only the genuinely possible
    failure mode is checked here: an *empty* list.

    Args:
        output: Parsed UnifiedReporterOutput to validate.

    Returns:
        List of warning message strings (empty if all pass).
    """
    warnings: list[str] = []

    # topics (unified topic list) — attribute access on Topic models
    for i, topic in enumerate(output.topics):
        ids = topic.source_server_ids
        if len(ids) == 0:
            msg = f"topics[{i}] '{topic.topic_name or '?'}' source_server_ids is empty"
            logger.warning("validate_source_server_ids: %s", msg)
            warnings.append(msg)

    # resources — attribute access on Resource models
    for i, resource in enumerate(output.resources):
        ids = resource.source_server_ids
        if len(ids) == 0:
            msg = f"resources[{i}] source_server_ids is empty"
            logger.warning("validate_source_server_ids: %s", msg)
            warnings.append(msg)

    # custom_tables — 所有启用表的记录（generic）。每张表的记录数组键名由
    # registry TableDefinition.records_key 声明（engineering→issues, world_models→items）。
    from z_winnow.custom_tables import registry as ct_registry

    ct_records_total = 0
    ct = output.custom_tables if isinstance(output.custom_tables, dict) else {}
    for table_id, table_data in ct.items():
        if not isinstance(table_data, dict):
            continue
        tdef = ct_registry.get_table(table_id)
        records_key = tdef.records_key if tdef else "items"
        records = table_data.get(records_key)
        if not isinstance(records, list):
            continue
        for i, rec in enumerate(records):
            ct_records_total += 1
            if not isinstance(rec, dict):
                continue
            ids = rec.get("source_server_ids")
            if not isinstance(ids, list) or len(ids) == 0:
                msg = f"custom_tables.{table_id}[{i}] source_server_ids is empty"
                logger.warning("validate_source_server_ids: %s", msg)
                warnings.append(msg)

    # Summary log
    if warnings:
        total_records = len(output.topics) + len(output.resources) + ct_records_total
        logger.warning(
            "validate_source_server_ids: %d/%d records have empty source_server_ids",
            len(warnings),
            total_records,
        )
    else:
        logger.info("validate_source_server_ids: all records have valid source_server_ids")

    return warnings


def custom_tables_record_count(output: UnifiedReporterOutput) -> int:
    """Count total records across all custom_tables slots (for logging)."""
    ct = output.custom_tables if isinstance(output.custom_tables, dict) else {}
    from z_winnow.custom_tables import registry as ct_registry

    total = 0
    for table_id, table_data in ct.items():
        if not isinstance(table_data, dict):
            continue
        tdef = ct_registry.get_table(table_id)
        records_key = tdef.records_key if tdef else "items"
        records = table_data.get(records_key)
        if isinstance(records, list):
            total += len(records)
    return total


# ============================================================
# Prompt Building
# ============================================================


def build_user_prompt(
    messages: list[dict[str, Any]],
    date: str,
    group_name: str,
    group_cfg: dict | None = None,
    members: list[dict] | None = None,
    prior_corrections: list[Any] | None = None,
    historical_topics: list[dict[str, Any]] | None = None,
    user_defined_topics: list[dict[str, Any]] | None = None,
    chat_context_md: str | None = None,
    feedback_hints: list[str] | None = None,
    custom_tables: dict[str, Any] | None = None,
) -> str:
    """Build the user prompt — always delegates to prompt.py build_prompt.

    Args:
        messages: Chat message list.
        date: Target date YYYYMMDD.
        group_name: Chat group display name.
        group_cfg: Optional group configuration dict.
        members: Optional member list for injection.
        prior_corrections: Optional historical correction examples.
        historical_topics: Optional MemOS historical topics for cross-day analysis.
        user_defined_topics: Optional user-created core topics for lifecycle matching.
        chat_context_md: Pre-formatted markdown chat context from ChatContextBuilder.
        feedback_hints: Optional unconsumed feedback hints for prompt injection.
        custom_tables: Optional per-group custom_tables blob for dynamic task list.
    """
    return build_prompt(
        messages,
        group_cfg=group_cfg,
        members=members,
        prior_corrections=prior_corrections,
        historical_topics=historical_topics,
        user_defined_topics=user_defined_topics,
        chat_context_md=chat_context_md,
        feedback_hints=feedback_hints,
        custom_tables=custom_tables,
        date=date,
        group_name=group_name,
    )


# ============================================================
# JSON Parsing — P014 three-strategy fallback
# ============================================================


def parse_json_output(raw: str) -> UnifiedReporterOutput:
    """Parse LLM output with multi-strategy fallback + source_server_ids validation.

    P014: Strategy 1 (direct) → Strategy 2 (code fence) → Strategy 3 (regex).
    After successful parse, runs validate_source_server_ids() to log warnings
    for records with empty source_server_ids. Warnings do not block output.
    """
    data: Any = None

    if not raw or not raw.strip():
        raise OutputParseError("Empty LLM output", raw_text=raw)

    errors: list[str] = []
    raw_stripped = raw.strip()

    # Strategy 1: Direct JSON parse
    try:
        data = json.loads(raw_stripped)
        result = UnifiedReporterOutput.model_validate(data)
        validate_source_server_ids(result)  # P014: warn but don't crash
        return result
    except json.JSONDecodeError as e:
        errors.append(f"Direct parse: {e}")
    except Exception as e:
        errors.append(f"Direct validation: {e}")

    # Strategy 2: Extract from markdown code fence
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1).strip())
            result = UnifiedReporterOutput.model_validate(data)
            validate_source_server_ids(result)  # P014: warn but don't crash
            return result
        except json.JSONDecodeError as e:
            errors.append(f"Fence parse: {e}")
        except Exception as e:
            errors.append(f"Fence validation: {e}")

    # Strategy 3: Regex extract outermost JSON object
    obj_match = re.search(r"\{[\s\S]*\}", raw)
    if obj_match:
        try:
            data = json.loads(obj_match.group(0))
            result = UnifiedReporterOutput.model_validate(data)
            validate_source_server_ids(result)  # P014: warn but don't crash
            return result
        except json.JSONDecodeError as e:
            errors.append(f"Regex parse: {e}")
        except Exception as e:
            errors.append(f"Regex validation: {e}")

    raise OutputParseError(
        f"Failed to parse LLM output after 3 attempts: {'; '.join(errors)}",
        raw_text=raw,
    )


# ============================================================
# LLM Call — P007 json_mode primary + invoke+parse fallback
# ============================================================


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
    llm: Any = None,
) -> UnifiedReporterOutput:
    """Call LLM with json_mode → fallback to invoke+parse.

    P007: json_mode via response_format={"type": "json_object"}.
    Falls back to plain invoke + manual parse on failure.
    """
    if llm is None:
        llm = create_model_for_subagent("unified-reporter", temperature=0.1)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Primary: json_mode via response_format
    try:
        if hasattr(llm, "bind"):
            json_llm = llm.bind(response_format={"type": "json_object"})
        else:
            json_llm = llm

        result = await json_llm.ainvoke(messages)
        raw_text: str = str(result.content) if hasattr(result, "content") else str(result)
        logger.info("json_mode succeeded (length=%d)", len(raw_text))
        return parse_json_output(raw_text)

    except OutputParseError:
        logger.warning("json_mode output parse failed, falling back to invoke+parse")
    except Exception as e:
        logger.warning("json_mode LLM call failed: %s. Falling back to invoke+parse.", e)

    # Fallback: plain invoke + manual parse
    try:
        result = await llm.ainvoke(messages)
        raw_text = str(result.content) if hasattr(result, "content") else str(result)
        logger.info("invoke+parse fallback (length=%d)", len(raw_text))
        return parse_json_output(raw_text)
    except OutputParseError:
        raise
    except Exception as e:
        raise OutputParseError(f"LLM invoke failed: {e}") from e


# ============================================================
# Factory Function — P002 DI + P010 mock mode
# ============================================================


def create_unified_reporter(
    llm: Any = None,
    memos_adapter: Any = None,
) -> Callable[[list[dict[str, Any]], str, str], UnifiedReporterOutput]:
    """Create a unified reporter agent via factory pattern with DI.

    P002: Agent factory + closure — llm/memos_adapter captured in closure.
    P010: Mock mode → deterministic mock output.

    Args:
        llm: Optional pre-configured LLM instance for DI.
        memos_adapter: Optional MemOS adapter (B4 graceful degradation).

    Returns:
        Callable[[messages, date, group_name], UnifiedReporterOutput]
    """
    # P010: Mock mode shortcut — use Settings instead of raw os.environ
    from z_winnow.config.settings import get_settings

    if get_settings().use_mock_llm:
        from z_winnow.subagents.unified_reporter.mock import (
            _mock_generate_unified_report,
        )

        logger.info("use_mock_llm=true — using deterministic mock unified reporter")

        def _mock_wrapper(
            messages: list[dict[str, Any]], date: str, group_name: str
        ) -> UnifiedReporterOutput:
            return _mock_generate_unified_report(messages, date, group_name)

        return _mock_wrapper

    _llm = llm
    _memos = memos_adapter

    def unified_reporter(
        messages: list[dict[str, Any]],
        date: str,
        group_name: str,
        prior_corrections: list[Any] | None = None,
    ) -> UnifiedReporterOutput:
        """Generate unified report — single LLM call, all sections.

        P009: prior_corrections optional cascading — None → block absent,
              non-None → XML injected via build_prompt().

        Args:
            messages: Chat message list (after content_enrich preprocessing).
            date: Target date YYYYMMDD.
            group_name: Chat group display name.
            prior_corrections: Optional historical correction examples for
                few-shot prompt injection. Default None (backward compatible).

        Returns:
            UnifiedReporterOutput with all sections.
        """

        async def _async_impl() -> UnifiedReporterOutput:
            # --- Build prompts ---
            # CT-2: Use build_system_prompt() so __CUSTOM_TABLES_FORMAT__
            # placeholder is replaced (even when custom_tables is None → "{}").
            system_prompt = build_system_prompt(group_cfg=None, custom_tables=None)

            if len(messages) <= 5:
                system_prompt += SHORT_MESSAGES_APPENDIX

            user_prompt = build_user_prompt(
                messages,
                date,
                group_name,
                prior_corrections=prior_corrections,
            )

            logger.info(
                "unified_reporter: date=%s group=%s msgs=%d",
                date,
                group_name,
                len(messages),
            )

            # --- Call LLM (P007 json_mode → fallback) ---
            try:
                result = await asyncio.wait_for(
                    _call_llm(system_prompt, user_prompt, llm=_llm),
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                raise NodeError(
                    node_name="unified_reporter",
                    original_error=TimeoutError(
                        f"LLM call timed out after {DEFAULT_TIMEOUT_SECONDS}s"
                    ),
                    retry_count=0,
                ) from None

            if not result.model_used:
                object.__setattr__(result, "model_used", "unified-reporter-llm")

            logger.info(
                "unified_reporter completed: overview=%d topics=%d "
                "resources=%d issues=%d highlights=%d",
                len(result.overview),
                len(result.topics),
                len(result.resources),
                custom_tables_record_count(result),
                len(result.highlights),
            )
            return result

        return asyncio.run(_async_impl())

    return unified_reporter


# ============================================================
# Convenience: generate_unified_report
# ============================================================


async def generate_unified_report(
    messages: list[dict[str, Any]],
    date: str,
    group_name: str,
    prior_corrections: list[Any] | None = None,
    historical_topics: list[dict[str, Any]] | None = None,
    user_defined_topics: list[dict[str, Any]] | None = None,
    chat_context_md: str | None = None,
    group_cfg: dict | None = None,
    members: list[dict] | None = None,
    feedback_hints: list[str] | None = None,
    custom_tables: dict[str, Any] | None = None,
) -> UnifiedReporterOutput:
    """Convenience function — directly call LLM async, no sync closure.

    P009: prior_corrections optional cascading — default None.

    CT-2: Accepts custom_tables for dynamic Task 3 prompt injection.
    When None/empty, old hardcoded Task 3 is used as fallback.

    Used by graph node_unified_reporter.

    Args:
        messages: Chat message list.
        date: Target date YYYYMMDD.
        group_name: Chat group display name.
        prior_corrections: Optional historical correction examples.
        historical_topics: Optional MemOS historical topics for cross-day analysis.
        user_defined_topics: Optional user-created core topics for lifecycle matching.
        chat_context_md: Optional pre-formatted markdown chat context.
        group_cfg: Optional group configuration (display_name, custom_prompt_hints).
        members: Optional VIP member list with role/weight for prompt injection.
        feedback_hints: Optional unconsumed feedback hints from feedback_events table.
        custom_tables: Optional per-group custom_tables blob
            ``{kind: {enabled: bool, config: dict}}`` for dynamic Task 3 injection.

    Returns:
        UnifiedReporterOutput with all sections.
    """
    system_prompt = build_system_prompt(group_cfg, custom_tables=custom_tables)
    if len(messages) <= 5:
        system_prompt += SHORT_MESSAGES_APPENDIX

    user_prompt = build_user_prompt(
        messages,
        date,
        group_name,
        group_cfg=group_cfg,
        members=members,
        prior_corrections=prior_corrections,
        historical_topics=historical_topics,
        user_defined_topics=user_defined_topics,
        chat_context_md=chat_context_md,
        feedback_hints=feedback_hints,
        custom_tables=custom_tables,
    )

    logger.info(
        "unified_reporter: date=%s group=%s msgs=%d (direct async)",
        date,
        group_name,
        len(messages),
    )

    llm = create_model_for_subagent("unified-reporter", temperature=0.1)

    try:
        result = await asyncio.wait_for(
            _call_llm(system_prompt, user_prompt, llm=llm),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise NodeError(
            node_name="unified_reporter",
            original_error=TimeoutError(f"LLM call timed out after {DEFAULT_TIMEOUT_SECONDS}s"),
            retry_count=0,
        ) from None

    if not result.model_used:
        object.__setattr__(result, "model_used", "unified-reporter-llm")

    logger.info(
        "unified_reporter completed: overview=%d topics=%d resources=%d issues=%d highlights=%d",
        len(result.overview),
        len(result.topics),
        len(result.resources),
        custom_tables_record_count(result),
        len(result.highlights),
    )
    return result
