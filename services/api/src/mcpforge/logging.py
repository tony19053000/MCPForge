"""Structured logging with redaction — 03_SECURITY_ACCESS.md §9.

Redaction is installed from the first commit rather than added later, because a
log line that once carried a token has already leaked it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

# Keys whose values are never logged, whatever the level.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "token",
        "id_token",
        "access_token",
        "refresh_token",
        "api_key",
        "gemini_api_key",
        "private_key",
        "client_secret",
        "webhook_secret",
        "password",
        "secret",
        "credentials",
        # File bodies never go to logs, only paths and counts.
        "content",
        "file_content",
        "source",
        "prompt",
        "response_text",
    }
)

_TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

REDACTED = "[redacted]"


def _redact_text(value: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        elif isinstance(event_dict[key], str):
            event_dict[key] = _redact_text(event_dict[key])
    return event_dict


def configure_logging(level: str = "info", *, json_output: bool = False) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            structlog.processors.JSONRenderer()
            if json_output
            else structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
