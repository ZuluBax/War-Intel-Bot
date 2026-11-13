"""Structured logging setup.

Uses :mod:`structlog` on top of the stdlib :mod:`logging` module so third-party
libraries (discord.py, httpx, SQLAlchemy) funnel through the same pipeline.

A redaction processor scrubs anything that looks like an API key / token from
log events as a defence-in-depth measure — call sites are still expected to not
pass secrets in the first place.
"""

from __future__ import annotations

import logging
import re
from typing import cast

import structlog
from structlog.types import EventDict, WrappedLogger

from app.config.settings import Settings

# Patterns that should never appear in a log line. These match the *value*
# shapes used by Torn (16-hex-ish), FFScouter (16 alphanumeric) and Discord
# (long dotted base64) so a stray secret in a dict/kwarg is masked even if the
# key name is not one we recognise.
_SECRET_KEY_NAMES = {
    "torn_api_key",
    "ffscouter_api_key",
    "discord_token",
    "api_key",
    "apikey",
    "token",
    "authorization",
    "key",
    "password",
    "secret",
}
_DISCORD_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}")
_MASK = "***REDACTED***"
# FFScouter takes its key as a ``?key=`` query parameter, so any logged URL (httpx
# request lines, exception text) would carry it. Whole parameter names only, so
# e.g. ``monkey=`` is left alone.
_URL_SECRET_RE = re.compile(r"(?i)([?&](?:key|api_key|apikey|token)=)[^&\s\"'#]+")


def _redact_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: mask secret-looking keys and Discord tokens."""
    for field, value in list(event_dict.items()):
        if field.lower() in _SECRET_KEY_NAMES:
            event_dict[field] = _MASK
        elif isinstance(value, str):
            masked = _DISCORD_TOKEN_RE.sub(_MASK, value)
            masked = _URL_SECRET_RE.sub(rf"\g<1>{_MASK}", masked)
            if masked != value:
                event_dict[field] = masked
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure stdlib + structlog. Idempotent; safe to call once at startup."""
    level = getattr(logging, settings.log_level)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (discord.py etc.) through structlog's renderer.
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # discord.py is noisy at INFO for gateway keepalives; keep it at WARNING
    # unless we're explicitly debugging.
    if level > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.WARNING)
    # Always, even at DEBUG: httpx/httpcore log full request URLs, and FFScouter's
    # carries the API key. The redactor masks it too; this keeps it out at source.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for the given module name."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
