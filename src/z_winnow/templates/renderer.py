"""Markdown renderer for z-winnow report templates.

Provides:
- render_report(schema_name, data): route data to the correct Jinja2 template
- md_table(headers, rows): build a Markdown table string
- md_heading(level, text): build a Markdown heading
- Additional Markdown helpers (md_bold, md_italic, md_link, md_list, md_hr)
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent

# Lazy-loaded Jinja2 environment
_env: Environment | None = None


def _get_env() -> Environment:
    """Return the cached Jinja2 Environment pointing to the templates directory."""
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=False,  # nosec B701 — Markdown templates, not HTML; XSS not applicable
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _env


# ---------------------------------------------------------------------------
# Schema → Template mapping
# ---------------------------------------------------------------------------

_SCHEMA_TEMPLATE_MAP: dict[str, str] = {
    "daily_report_v1": "daily_report.j2",
    "daily_report": "daily_report.j2",
    "resource_extraction_v1": "resource_extraction.j2",
    "resource_extraction": "resource_extraction.j2",
    "engineering_issues_v1": "engineering_issues.j2",
    "engineering_issues": "engineering_issues.j2",
    "feishu_daily_v1": "feishu_daily.j2",
    "feishu_daily": "feishu_daily.j2",
}

# Allow external registration of custom templates
# Values can be either a filename (str) or a Template object
_CUSTOM_TEMPLATES: dict[str, Any] = {}
_SOURCE_TEMPLATES: dict[str, Template] = {}


def register_template(schema_name: str, template_name: str) -> None:
    """Register a custom Jinja2 template filename for a schema name.

    Args:
        schema_name: A key used by render_report() to select the template.
        template_name: The filename of a Jinja2 template inside the templates directory.
    """
    _CUSTOM_TEMPLATES[schema_name] = template_name


def register_template_source(schema_name: str, template_source: str) -> None:
    """Register a custom Jinja2 template from a raw source string.

    Use this to inject custom formatting without modifying template files.

    Args:
        schema_name: Schema identifier to associate with the template.
        template_source: Raw Jinja2 template string.
    """
    _SOURCE_TEMPLATES[schema_name] = _get_env().from_string(template_source)


def unregister_template(schema_name: str) -> bool:
    """Remove a registered override template.

    Returns:
        True if the override was removed, False if it didn't exist.
    """
    removed_file = _CUSTOM_TEMPLATES.pop(schema_name, None) is not None
    removed_source = _SOURCE_TEMPLATES.pop(schema_name, None) is not None
    return removed_file or removed_source


def clear_overrides() -> None:
    """Remove all registered override templates."""
    _CUSTOM_TEMPLATES.clear()
    _SOURCE_TEMPLATES.clear()


def list_registered_schemas() -> list[str]:
    """Return all schema names that can be rendered."""
    return sorted(set(_SCHEMA_TEMPLATE_MAP) | set(_CUSTOM_TEMPLATES) | set(_SOURCE_TEMPLATES))


# ---------------------------------------------------------------------------
# Main render API
# ---------------------------------------------------------------------------


def render_report(
    schema_name: str,
    data: dict[str, Any],
    *,
    extra_context: dict[str, Any] | None = None,
) -> str:
    """Render structured data to Markdown using the appropriate Jinja2 template.

    Args:
        schema_name: Key identifying the report type (e.g. ``"daily_report_v1"``).
        data: The report data dict matching the corresponding JSON Schema.
        extra_context: Optional extra variables to inject into the template context.

    Returns:
        Rendered Markdown string.

    Raises:
        ValueError: If ``schema_name`` is not recognised.
        jinja2.TemplateError: If template rendering fails.
    """
    env = _get_env()

    # 1. Resolve template
    # Priority: source templates > file-name overrides > built-in map
    source_template = _SOURCE_TEMPLATES.get(schema_name)
    template_name = _CUSTOM_TEMPLATES.get(schema_name) or _SCHEMA_TEMPLATE_MAP.get(schema_name)

    if source_template is not None:
        template = source_template
    elif template_name is not None:
        template = env.get_template(template_name)
    else:
        raise ValueError(
            f"Unknown schema_name: {schema_name!r}. Registered schemas: {list_registered_schemas()}"
        )

    context: dict[str, Any] = dict(data)
    if extra_context:
        context.update(extra_context)

    # Inject a generation_time if not already present (used by some templates)
    if "generation_time" not in context:
        context["generation_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return str(template.render(**context))


# ---------------------------------------------------------------------------
# Markdown helper utilities
# ---------------------------------------------------------------------------


def md_heading(level: int, text: str) -> str:
    """Render a Markdown ATX heading.

    Args:
        level: Heading level 1-6.
        text: Heading text.

    Returns:
        Markdown heading string, e.g. ``"## My Heading"``.
    """
    level = max(1, min(6, level))
    return f"{'#' * level} {text}"


def md_bold(text: str) -> str:
    """Render bold text: ``**text**``."""
    return f"**{text}**"


def md_italic(text: str) -> str:
    """Render italic text: ``*text*``."""
    return f"*{text}*"


def md_link(text: str, url: str) -> str:
    """Render a Markdown link: ``[text](url)``."""
    return f"[{text}]({url})"


def md_code(text: str, lang: str = "") -> str:
    """Render an inline or fenced code block.

    Args:
        text: Code content.
        lang: Optional language identifier for fenced blocks.
              If ``text`` contains newlines a fenced block is produced;
              otherwise an inline code span.

    Returns:
        Markdown code string.
    """
    if "\n" in text:
        return f"```{lang}\n{text}\n```"
    return f"`{text}`"


def md_code_block(code: str, language: str = "") -> str:
    """Render a Markdown fenced code block (always fenced).

    Args:
        code: Code content.
        language: Optional language identifier for syntax highlighting.

    Returns:
        Fenced code block string.
    """
    return f"```{language}\n{code}\n```"


def md_image(alt: str, url: str) -> str:
    """Render a Markdown image: ``![alt](url)``."""
    return f"![{alt}]({url})"


def md_escape_table(text: str) -> str:
    """Escape pipe and newline characters for safe embedding in table cells."""
    return str(text).replace("|", "\\|").replace("\n", "<br>")


def md_sanitize_cell(text: str) -> str:
    """Sanitize text for use in Markdown table cells.

    Strips whitespace, collapses spaces, escapes pipes.
    """
    text = str(text).strip()
    text = " ".join(text.split())
    return text.replace("|", "\\|")


def md_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    align: Sequence[str] | None = None,
) -> str:
    """Build a Markdown table.

    Args:
        headers: Column header texts.
        rows: Row data, each row a sequence of strings.
        align: Optional per-column alignment hints
               (``"left"``, ``"center"``, ``"right"``, or ``None`` for default).

    Returns:
        A Markdown table string.  Example::

            | Name | Age |
            | --- | --- |
            | Alice | 30 |

    Notes:
        Pipes and newlines inside cell values are escaped.
        This function does **not** wrap the result in blank lines;
        callers should add those if needed.
    """
    if not headers:
        return ""

    def _escape_cell(cell: str) -> str:
        """Escape pipe and newline characters inside a table cell."""
        return str(cell).replace("|", "\\|").replace("\n", " ")

    # Build alignment row
    align = align or []
    align_row: list[str] = []
    for i, _ in enumerate(headers):
        a = align[i] if i < len(align) else ""
        if a == "center":
            align_row.append(":---:")
        elif a == "right":
            align_row.append("---:")
        elif a == "left":
            align_row.append(":---")
        else:
            align_row.append("---")

    lines: list[str] = []
    lines.append("| " + " | ".join(_escape_cell(h) for h in headers) + " |")
    lines.append("| " + " | ".join(align_row) + " |")
    for row in rows:
        cells = [_escape_cell(c) for c in row]
        # Pad to header count if needed
        while len(cells) < len(headers):
            cells.append("")
        lines.append("| " + " | ".join(cells[: len(headers)]) + " |")

    return "\n".join(lines)


def md_list(items: Sequence[str], ordered: bool = False) -> str:
    """Render a Markdown list.

    Args:
        items: List item texts.
        ordered: If True, produce an ordered (numbered) list, else unordered (bullets).

    Returns:
        Markdown list string, one item per line.
    """
    lines: list[str] = []
    for i, item in enumerate(items, start=1):
        prefix = f"{i}." if ordered else "-"
        lines.append(f"{prefix} {item}")
    return "\n".join(lines)


def md_horizontal_rule() -> str:
    """Return a Markdown horizontal rule ``---``."""
    return "---"


def md_quote(text: str) -> str:
    """Render a Markdown blockquote (lines prefixed with ``> ``)."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.split("\n"))


# ---------------------------------------------------------------------------
# Convenience: quick render for each report type
# ---------------------------------------------------------------------------


def render_daily(data: dict[str, Any]) -> str:
    """Shortcut for rendering a daily report."""
    return render_report("daily_report_v1", data)


def render_resources(data: dict[str, Any]) -> str:
    """Shortcut for rendering a resource extraction report."""
    return render_report("resource_extraction_v1", data)


def render_engineering(data: dict[str, Any]) -> str:
    """Shortcut for rendering an engineering issues report."""
    return render_report("engineering_issues_v1", data)


def render_feishu_daily(data: dict[str, Any]) -> str:
    """Shortcut for rendering a daily report in Feishu Markdown format.

    Feishu Markdown uses blockquotes for metadata footers and
    simplified emoji-based heat indicators to ensure optimal rendering
    in Feishu Docs.
    """
    return render_report("feishu_daily_v1", data)
