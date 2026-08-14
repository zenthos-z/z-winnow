"""unified_reporter subagent — single LLM call replaces 3 subagents.

Replaces daily_reporter + resource_extractor + engineering_analyzer with
one unified agent that produces all report sections in one LLM call.

P002: Factory + DI pattern — create_unified_reporter() returns closure.
P010: Mock mode → deterministic mock output.
"""

from z_winnow.subagents.unified_reporter.agent import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RETRIES,
    OutputParseError,
    build_user_prompt,
    create_unified_reporter,
    generate_unified_report,
    parse_json_output,
)
from z_winnow.subagents.unified_reporter.mock import (
    _mock_generate_unified_report,
)
from z_winnow.subagents.unified_reporter.models import (
    UnifiedReporterOutput,
)
from z_winnow.subagents.unified_reporter.prompt import (
    SHORT_MESSAGES_APPENDIX,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_prompt,
    build_system_prompt,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "SHORT_MESSAGES_APPENDIX",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "OutputParseError",
    "UnifiedReporterOutput",
    "_mock_generate_unified_report",
    "build_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "create_unified_reporter",
    "generate_unified_report",
    "parse_json_output",
]
