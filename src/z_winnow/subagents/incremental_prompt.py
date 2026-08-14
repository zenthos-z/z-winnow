"""T-W12-13: Incremental prompt builder for feedback-driven corrections.

Builds the incremental prompt used by the incremental_reprocess entry point.
The prompt includes: L2 context, original output, correction requirements,
and MemOS historical feedback memory context.

L070: This module uses only stdlib imports at module level — no third-party
dependencies that could cause ImportError cascading in __init__.py.

Prompt Input (per S5 Design Standard):
  - target_type: "report" | "topic" | "trend" | "resource" | "engineering"
  - target_id: identifier of the target record
  - l2_context: parsed_contexts text from L2
  - original_output: the current L3 content to be corrected
  - correction: the user-provided correction text
  - memory_context: historical feedback from MemOS (optional)

Prompt Output (per S5 Design Standard):
  - corrected_item: the corrected content
  - mem_feedback_record: summary for MemOS feedback memory
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# Data classes for incremental prompt I/O
# ============================================================


@dataclass
class IncrementalPromptInput:
    """Input for incremental correction prompt.

    Per S5 Design Standard:
      - target_type / target_id / l2_context / original_output
      - correction / memory_context
    """

    target_type: str = ""
    target_id: str = ""
    l2_context: str = ""
    original_output: str = ""
    correction: str = ""
    memory_context: dict[str, Any] | None = None

    # Avoid A002: explicit field defaults
    feedback_id: str = ""
    group_id: str = ""
    date: str = ""


@dataclass
class IncrementalPromptOutput:
    """Output from incremental correction.

    Per S5 Design Standard:
      - corrected_item: the corrected L3 content
      - mem_feedback_record: summary for MemOS feedback memory
    """

    corrected_item: str = ""
    mem_feedback_record: str = ""
    success: bool = True
    error: str = ""


# ============================================================
# Prompt template
# ============================================================

_INCREMENTAL_SYSTEM_PROMPT = """\
You are an incremental report correction assistant. Your task is to apply \
user-provided corrections to a specific report record while maintaining \
consistency with the surrounding context.

Rules:
1. Apply ONLY the requested correction — do not change unrelated content.
2. Preserve the original JSON structure and data format.
3. If the correction is ambiguous, make the minimal interpretation.
4. Output must be valid JSON matching the original schema.
5. Provide a brief summary of what was changed for the feedback memory.
"""

_INCREMENTAL_USER_PROMPT_TEMPLATE = """\
## Correction Task

Target type: {target_type}
Target ID: {target_id}

## L2 Context (source conversations)

{l2_context}

## Original Output

{original_output}

## Required Correction

{correction}

{memory_section}

## Output Format

Respond with a JSON object:
```json
{{
  "corrected_item": "<the corrected content, same format as original>",
  "mem_feedback_record": "<brief summary of changes for feedback memory>"
}}
```
"""


def build_incremental_prompt(inp: IncrementalPromptInput) -> tuple[str, str]:
    """Build system and user prompts for incremental correction.

    Args:
        inp: IncrementalPromptInput with all required fields.

    Returns:
        Tuple of (system_prompt, user_prompt) strings.
    """
    # Build memory section if context available
    memory_section = ""
    if inp.memory_context:
        memory_lines: list[str] = []
        prior_corrections = inp.memory_context.get("prior_corrections", [])
        if prior_corrections:
            memory_lines.append("### Historical Feedback (from MemOS)")
            for pc in prior_corrections[:5]:  # P006: top 5 to manage token budget
                mem = pc.get("memory", "")
                if mem:
                    memory_lines.append(f"- {mem}")
        if memory_lines:
            memory_section = "\n".join(memory_lines)

    # P006: Truncate L2 context to manage token budget
    l2_context = inp.l2_context or "(no L2 context available)"
    if len(l2_context) > 4000:
        l2_context = l2_context[:4000] + "\n...(truncated)"

    user_prompt = _INCREMENTAL_USER_PROMPT_TEMPLATE.format(
        target_type=inp.target_type,
        target_id=inp.target_id,
        l2_context=l2_context,
        original_output=inp.original_output or "(no original output)",
        correction=inp.correction,
        memory_section=memory_section,
    )

    return _INCREMENTAL_SYSTEM_PROMPT, user_prompt


def parse_incremental_output(raw_output: str) -> IncrementalPromptOutput:
    """Parse the LLM output from incremental correction.

    A008: data: Any = None — defensive JSON parsing.

    Args:
        raw_output: Raw LLM response string.

    Returns:
        IncrementalPromptOutput with parsed results.
    """
    # A008: defensive initialization
    result = IncrementalPromptOutput()

    # Strip markdown code fences if present
    cleaned = raw_output.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json") :]
    if cleaned.startswith("```"):
        cleaned = cleaned[len("```") :]
    if cleaned.endswith("```"):
        cleaned = cleaned[: -len("```")]
    cleaned = cleaned.strip()

    try:
        data: Any = json.loads(cleaned)  # A008: Any first, validate later
        if isinstance(data, dict):
            result.corrected_item = str(data.get("corrected_item", ""))
            result.mem_feedback_record = str(data.get("mem_feedback_record", ""))
            result.success = True
        else:
            result.success = False
            result.error = f"Expected dict, got {type(data).__name__}"
    except json.JSONDecodeError as exc:
        result.success = False
        result.error = f"JSON parse error: {exc}"
        logger.warning("parse_incremental_output: failed to parse — %s", exc)

    return result
