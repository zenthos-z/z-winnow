"""z_winnow/subagents/ — Subagent implementation modules.

T-W12-11: Cleaned up Wave 7/8 legacy 4-subagent I/O types. Only active
subagent contracts are exported.

Subagents:
- unified_reporter: Single LLM call for daily report + resources + engineering
- output_composer: Markdown report assembly from unified output
"""

from z_winnow.subagents.contracts import (
    OutputComposerInput,
    OutputComposerOutput,
    SubagentInput,
    SubagentOutput,
    UnifiedReporterOutput,
)
from z_winnow.subagents.unified_reporter import (
    DEFAULT_TIMEOUT_SECONDS as UNIFIED_DEFAULT_TIMEOUT,
)
from z_winnow.subagents.unified_reporter import (
    MAX_RETRIES as UNIFIED_MAX_RETRIES,
)
from z_winnow.subagents.unified_reporter import (
    SYSTEM_PROMPT as UNIFIED_SYSTEM_PROMPT,
)
from z_winnow.subagents.unified_reporter import (
    USER_PROMPT_TEMPLATE as UNIFIED_USER_PROMPT,
)
from z_winnow.subagents.unified_reporter import (
    OutputParseError,
    _mock_generate_unified_report,
    create_unified_reporter,
    generate_unified_report,
)
from z_winnow.subagents.unified_reporter import (
    build_user_prompt as build_unified_user_prompt,
)
from z_winnow.subagents.unified_reporter import (
    parse_json_output as parse_unified_output,
)

__all__ = [
    "UNIFIED_DEFAULT_TIMEOUT",
    "UNIFIED_MAX_RETRIES",
    "UNIFIED_SYSTEM_PROMPT",
    "UNIFIED_USER_PROMPT",
    "OutputComposerInput",
    "OutputComposerOutput",
    "OutputParseError",
    "SubagentInput",
    "SubagentOutput",
    "UnifiedReporterOutput",
    "_mock_generate_unified_report",
    "build_unified_user_prompt",
    "create_unified_reporter",
    "generate_unified_report",
    "parse_unified_output",
]
