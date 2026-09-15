"""The error taxonomy is the retry policy, so it gets tested like one."""

from __future__ import annotations

import pytest

from core.errors import (
    AuthenticationRequiredError,
    BrowserCrashedError,
    CheckpointError,
    CircuitBreakerTripped,
    ElementNotFoundError,
    NetworkError,
    PageTimeoutError,
    RateLimitedError,
    SessionExpiredError,
    StaleElementError,
    TransientError,
    UIChangedError,
    UnlikerError,
    classify,
)


@pytest.mark.parametrize(
    "error",
    [
        NetworkError("x"),
        PageTimeoutError("x"),
        ElementNotFoundError("x"),
        StaleElementError("x"),
        TransientError("x"),
    ],
)
def test_transient_errors_are_retryable_and_not_fatal(error):
    assert error.retryable
    assert not error.fatal
    assert not error.needs_human


@pytest.mark.parametrize(
    "error", [AuthenticationRequiredError("x"), SessionExpiredError("x"), CheckpointError("x")]
)
def test_authentication_problems_need_a_person(error):
    assert error.needs_human
    assert not error.retryable


def test_a_checkpoint_is_an_authentication_problem():
    """So that one handler covers login, expiry, 2FA and challenges alike."""
    assert isinstance(CheckpointError("x"), AuthenticationRequiredError)
    assert isinstance(SessionExpiredError("x"), AuthenticationRequiredError)


@pytest.mark.parametrize(
    "error", [BrowserCrashedError("x"), UIChangedError("x"), CircuitBreakerTripped("x")]
)
def test_fatal_errors_stop_the_run(error):
    assert error.fatal


def test_rate_limiting_is_retryable_but_not_a_normal_failure():
    error = RateLimitedError("blocked", retry_after=120)
    assert error.retryable
    assert not error.fatal
    assert error.retry_after == 120


def test_every_error_carries_a_code():
    for cls in UnlikerError.__subclasses__():
        assert cls.code and cls.code != UnlikerError.code or cls is TransientError


@pytest.mark.parametrize(
    "message,expected",
    [
        ("net::ERR_CONNECTION_RESET at https://x", NetworkError),
        ("Temporary failure in name resolution", NetworkError),
        ("Timeout 30000ms exceeded waiting for selector", ElementNotFoundError),
        ("Timeout 30000ms exceeded.", PageTimeoutError),
        ("Target page, context or browser has been closed", BrowserCrashedError),
        ("Page crashed", BrowserCrashedError),
        ("Element is not attached to the DOM", StaleElementError),
        ("strict mode violation: resolved to 3 elements", ElementNotFoundError),
    ],
)
def test_third_party_errors_are_classified(message, expected):
    assert isinstance(classify(Exception(message)), expected)


def test_classification_is_idempotent():
    original = RateLimitedError("blocked")
    assert classify(original) is original


def test_unknown_errors_default_to_retryable():
    """An unrecognised error should cost one attempt, not the whole run."""
    classified = classify(ValueError("something odd"))
    assert isinstance(classified, TransientError)
    assert classified.retryable
    assert "ValueError" in str(classified)
