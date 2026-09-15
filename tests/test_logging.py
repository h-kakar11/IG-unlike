"""Logging must record everything interesting and no secrets at all."""

from __future__ import annotations

import logging

import pytest

from core.logging_setup import REDACTED, RedactingFilter, get_logger, redact, safe_url, setup_logging


@pytest.mark.parametrize(
    "text",
    [
        "Cookie: sessionid=ABC123xyz; csrftoken=deadbeef",
        "set-cookie: sessionid=9999",
        "password=hunter2",
        "PASSWORD: hunter2",
        "access_token=abcdef123456",
        "authorization: Bearer abc.def.ghi",
        "ds_user_id: 1234567890",
        "csrftoken=QQQQQQQQ",
    ],
)
def test_credential_shapes_are_scrubbed(text):
    cleaned = redact(text)
    assert REDACTED in cleaned
    for secret in ("hunter2", "ABC123xyz", "deadbeef", "abcdef123456", "1234567890", "9999"):
        assert secret not in cleaned


def test_jwt_like_blobs_are_scrubbed():
    text = "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4"
    assert "eyJhbGciOiJIUzI1NiJ9" not in redact(text)


def test_long_hex_blobs_are_scrubbed():
    assert redact("id " + "a" * 40) == "id " + REDACTED


def test_ordinary_messages_are_left_alone():
    message = "Item p/ABC123 completed in 1.4s (batch 7 of 12)"
    assert redact(message) == message


def test_urls_are_logged_without_query_strings():
    assert safe_url("https://www.instagram.com/p/X/?sessionid=zzz#frag") == (
        "https://www.instagram.com/p/X/"
    )
    assert safe_url("") == ""


def test_the_filter_rewrites_records():
    record = logging.LogRecord(
        "unliker.test", logging.INFO, __file__, 1, "sessionid=%s", ("secret123",), None
    )
    RedactingFilter().filter(record)
    assert "secret123" not in record.getMessage()


def test_secrets_never_reach_the_log_file(tmp_path):
    path = tmp_path / "unliker.log"
    logger = setup_logging(path, level="DEBUG", console=False)

    logger.info("Restoring session with sessionid=TOPSECRET and csrftoken=ALSOSECRET")
    logger.warning("password=hunter2")
    for handler in logger.handlers:
        handler.flush()

    contents = path.read_text()
    assert "TOPSECRET" not in contents
    assert "ALSOSECRET" not in contents
    assert "hunter2" not in contents
    assert REDACTED in contents


def test_exception_text_is_redacted_too(tmp_path):
    path = tmp_path / "unliker.log"
    logger = setup_logging(path, level="DEBUG", console=False)

    try:
        raise RuntimeError("failed with sessionid=LEAKED")
    except RuntimeError:
        logger.exception("Operation failed")
    for handler in logger.handlers:
        handler.flush()

    assert "LEAKED" not in path.read_text()


def test_setup_is_idempotent(tmp_path):
    path = tmp_path / "unliker.log"
    first = setup_logging(path, console=False)
    count = len(first.handlers)
    second = setup_logging(path, console=False)

    assert len(second.handlers) == count, "repeated setup must not duplicate handlers"


def test_child_loggers_are_namespaced():
    assert get_logger("worker").name == "unliker.worker"
    assert get_logger("unliker.worker").name == "unliker.worker"


def test_the_log_rotates(tmp_path):
    path = tmp_path / "unliker.log"
    logger = setup_logging(path, console=False, max_bytes=2048, backup_count=2)
    for index in range(500):
        logger.info("a fairly long line of log output number %d", index)
    for handler in logger.handlers:
        handler.flush()

    assert path.exists()
    assert (tmp_path / "unliker.log.1").exists()
    assert not (tmp_path / "unliker.log.3").exists()
