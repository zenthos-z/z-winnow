"""z_winnow.templates — Jinja2 report templates and Markdown renderer.

Provides a template-based report generation system with:

Templates (Jinja2):
    daily_report.j2       — Daily report Markdown template
    resource_extraction.j2 — Resource extraction table template
    engineering_issues.j2  — Engineering issue report template
    feishu_daily.j2        — Feishu Bitable daily format

Note: topic_tracker.j2 was removed in the field-unification refactor —
topic rendering is handled by ``render_topics_with_lifecycle`` in
output_composer, which reads the unified ``lifecycle`` model directly.

Renderer API:
    render_report(name, data)  — Render any registered template by name
    render_daily(data)         — Convenience: render daily report
    render_resources(data)     — Convenience: render resource table
    render_engineering(data)   — Convenience: render engineering issues
    render_feishu_daily(data)  — Convenience: render Feishu daily format

Markdown Helpers:
    md_table(rows, headers)    — Build Markdown table from data
    md_heading(text, level)    — Build Markdown heading
    md_bold(text) / md_italic(text) / md_code(text)
    md_link(text, url) / md_image(alt, url)
    md_list(items, ordered) / md_quote(text)
    md_code_block(code, lang) / md_horizontal_rule()
    md_escape_table(text) / md_sanitize_cell(text)

Template Registration:
    register_template(name, template)        — Register a Jinja2 Template
    register_template_source(name, source)   — Register from source string
    unregister_template(name)                — Remove a template
    clear_overrides()                        — Clear all registered overrides
    list_registered_schemas()                — List all registered names
"""

from z_winnow.templates.renderer import (
    clear_overrides,
    list_registered_schemas,
    md_bold,
    md_code,
    md_code_block,
    md_escape_table,
    md_heading,
    md_horizontal_rule,
    md_image,
    md_italic,
    md_link,
    md_list,
    md_quote,
    md_sanitize_cell,
    md_table,
    register_template,
    register_template_source,
    render_daily,
    render_engineering,
    render_feishu_daily,
    render_report,
    render_resources,
    unregister_template,
)

__all__ = [
    "clear_overrides",
    "list_registered_schemas",
    "md_bold",
    "md_code",
    "md_code_block",
    "md_escape_table",
    "md_heading",
    "md_horizontal_rule",
    "md_image",
    "md_italic",
    "md_link",
    "md_list",
    "md_quote",
    "md_sanitize_cell",
    # Markdown helpers
    "md_table",
    "register_template",
    "register_template_source",
    # Convenience shortcuts
    "render_daily",
    "render_engineering",
    "render_feishu_daily",
    # Main render API
    "render_report",
    "render_resources",
    "unregister_template",
]
