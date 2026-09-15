"""Logging with mandatory redaction of anything that looks like a secret.

Two rules shape this module:

1. Everything interesting is logged (startup, auth state, discovery, every
   unlike, every retry, every pause, every database write).
2. Nothing sensitive is logged. Cookies, session identifiers, CSRF tokens,
   bearer tokens and ``password=`` pairs are scrubbed by a filter attached to
   the handlers, so the redaction cannot be forgotten at a call site.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, Iterable

REDACTED = "[REDACTED]"

#: Patterns are applied to the *formatted* message of every record.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # key=value / key: value / "key": "value" for known-sensitive keys
    (
        re.compile(
            r"(?i)\b(sessionid|session_id|csrftoken|csrf_token|ds_user_id|mid|ig_did|"
            r"rur|shbid|shbts|password|passwd|pwd|access[_-]?token|auth[_-]?token|"
            r"api[_-]?key|bearer|authorization|cookie|set-cookie)\b"
            r"(\s*[:=]\s*|\"\s*:\s*\"?)"
            r"([^\s,;&\"'}\]]+)"
        ),
        r"\1\2" + REDACTED,
    ),
    # Whole Cookie headers
    (re.compile(r"(?i)cookie\s*:\s*[^\n]+"), "Cookie: " + REDACTED),
    # Long opaque blobs that look like tokens (JWT-ish or 32+ hex/base64)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), REDACTED),
    (re.compile(r"\b[0-9a-f]{32,}\b"), REDACTED),
)


def redact(text: str) -> str:
    """Scrub credential-shaped substrings from ``text``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites each record's message in place before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive: never break logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        if record.exc_info:
            # Exception text can carry a URL with tokens in the query string.
            record.exc_text = redact(
                record.exc_text or logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        return True


class _ConsoleFormatter(logging.Formatter):
    """Compact console lines: ``[12:43:51] INFO  message``."""

    def __init__(self) -> None:
        super().__init__("[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def setup_logging(
    log_path: str | Path,
    *,
    level: str = "INFO",
    console: bool = True,
    console_level: str | None = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the ``unliker`` logger tree and return its root.

    Safe to call repeatedly; existing handlers are replaced so that a second
    call in the same process does not double every line.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("unliker")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redactor = RedactingFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(getattr(logging, (console_level or level).upper(), logging.INFO))
        stream.setFormatter(_ConsoleFormatter())
        stream.addFilter(redactor)
        logger.addHandler(stream)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``unliker`` logger."""
    return logging.getLogger(f"unliker.{name}" if not name.startswith("unliker") else name)


def safe_url(url: str) -> str:
    """Strip query strings and fragments before a URL is logged."""
    if not url:
        return url
    for separator in ("?", "#"):
        url = url.split(separator, 1)[0]
    return redact(url)


def summarise(values: Iterable[Any], limit: int = 5) -> str:
    """Render a preview of a sequence for log lines (``a, b, c (+12 more)``)."""
    values = list(values)
    head = ", ".join(str(v) for v in values[:limit])
    extra = len(values) - limit
    return f"{head} (+{extra} more)" if extra > 0 else head
