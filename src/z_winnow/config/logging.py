"""T-D4: Structured logging with structlog.

JSON format output to file + colored output to console.
All logs auto-inject date, track_id, server_id context.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

import structlog


def _add_context(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Auto-inject standard context fields into every log event."""
    event_dict.setdefault("date", datetime.now(UTC).isoformat())
    # track_id and server_id are optional context set via structlog.contextvars
    return event_dict


def configure_logging(
    log_level: str | None = None,
    json_to_file: bool = True,
    log_file: str = "logs/winnow.log",
) -> None:
    """Configure structlog globally.

    Console: colored human-readable output.
    File: JSON-lines format for machine parsing.

    Args:
        log_level: Override log level (DEBUG, INFO, WARNING, ERROR).
                   Defaults to LOG_LEVEL env var or "INFO".
        json_to_file: If True, write JSON-formatted logs to a file.
        log_file: Path to the log file (created if it doesn't exist).
    """
    raw_level: str = log_level or os.getenv("LOG_LEVEL") or "INFO"
    level = raw_level.upper()
    logging_level = getattr(logging, level, logging.INFO)

    # Shared processors: produce an event_dict (no renderer at the end)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_context,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    # Configure structlog (no renderer — handlers do the rendering)
    structlog.configure(
        processors=shared_processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Root logger setup
    root_logger = logging.getLogger()
    root_logger.setLevel(logging_level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Console handler with colored output
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors[:-1],  # exclude wrap_for_formatter
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler with JSON output
    if json_to_file:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        file_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors[:-1],  # exclude wrap_for_formatter
        )
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging_level)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger for the given name.

    Args:
        name: Logger name (typically __name__). If None, uses the root logger.

    Returns:
        A structlog BoundLogger instance with automatic context injection.

    Example:
        >>> log = get_logger(__name__)
        >>> log.info("hello", server_id="abc", track_id="T-D4")
    """
    return structlog.get_logger(name or "winnow")  # type: ignore[no-any-return]


# Auto-configure logging on first import
_configured = False


def _ensure_configured() -> None:
    """Configure logging once (idempotent)."""
    global _configured
    if not _configured:
        configure_logging()
        _configured = True


_ensure_configured()
