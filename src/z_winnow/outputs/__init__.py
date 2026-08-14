"""Output channels for winnow-langchain.

- ``report_writer``: daily report Markdown rendering (Phase H manual trigger,
  lazily loaded — may be unavailable).
- ``image_gen``: daily cover image generation (DMX/Gemini native generateContent).

The legacy direct-HTTP Feishu adapter (``outputs/feishu.py``) was removed — all
Feishu Bitable I/O now flows through ``pipeline/feishu/`` (lark-cli, user identity).
"""

# T-W7-5 report_writer is loaded lazily — it may not be implemented yet.
_report_writer_imported = False

try:
    from z_winnow.outputs.report_writer import (  # noqa: F401
        write_daily_report,
    )

    _report_writer_imported = True
except Exception:
    pass


__all__: list[str] = []

if _report_writer_imported:
    __all__.append("write_daily_report")
