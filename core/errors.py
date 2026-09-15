"""Exception taxonomy.

The worker's retry policy is driven entirely by exception *type*, so the
classification here is the retry policy. Three questions are asked of every
error:

* ``retryable``   — is another attempt at this item worth making?
* ``fatal``       — must the whole run stop?
* ``needs_human`` — is a person required before anything can continue?
"""

from __future__ import annotations


class UnlikerError(Exception):
    """Base class for everything this application raises deliberately."""

    #: Another attempt at the same item may succeed.
    retryable = False
    #: The run cannot usefully continue.
    fatal = False
    #: A human has to do something (log in, pass a checkpoint) before we go on.
    needs_human = False
    #: Short machine-readable tag recorded in the database and logs.
    code = "error"


# ---------------------------------------------------------------------------
# Transient conditions — retry the item
# ---------------------------------------------------------------------------
class TransientError(UnlikerError):
    """Something went wrong that is expected to pass on its own."""

    retryable = True
    code = "transient"


class NetworkError(TransientError):
    """DNS failure, connection reset, proxy error, offline machine."""

    code = "network"


class PageTimeoutError(TransientError):
    """A navigation or action exceeded its timeout."""

    code = "page_timeout"


class ElementNotFoundError(TransientError):
    """An expected control was not present.

    Retryable because the usual cause is a slow render, but a *run* of these
    is how a changed Instagram UI announces itself, so the worker escalates
    repeated occurrences (see :class:`UIChangedError`).
    """

    code = "element_not_found"


class StaleElementError(TransientError):
    """The DOM node went away between locating it and using it."""

    code = "stale_element"


class ServerError(TransientError):
    """Instagram returned a 5xx or an explicit "try again later" page."""

    code = "server_error"


class UnexpectedNavigationError(TransientError):
    """The page navigated somewhere we did not ask it to go."""

    code = "unexpected_navigation"


class VerificationFailedError(TransientError):
    """The action was performed but the expected state change never appeared.

    Deliberately retryable-but-counted: never assume a click worked.
    """

    code = "verification_failed"


# ---------------------------------------------------------------------------
# Throttling — back off, do not retry immediately
# ---------------------------------------------------------------------------
class RateLimitedError(UnlikerError):
    """Instagram is asking us to slow down, or has temporarily blocked actions.

    Not "retryable" in the per-item sense: the correct response is to stop
    acting and wait, which the rate controller handles.
    """

    retryable = True
    code = "rate_limited"

    def __init__(self, message: str = "Action rate limit reached", *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Conditions requiring a human
# ---------------------------------------------------------------------------
class AuthenticationRequiredError(UnlikerError):
    """Not logged in. The user must log in manually in the browser window."""

    needs_human = True
    code = "auth_required"


class SessionExpiredError(AuthenticationRequiredError):
    """We were logged in and are no longer."""

    code = "session_expired"


class CheckpointError(AuthenticationRequiredError):
    """A challenge, checkpoint, 2FA prompt or suspension screen is showing.

    The tool stops here by design and never attempts to answer it.
    """

    code = "checkpoint"


# ---------------------------------------------------------------------------
# Fatal conditions — stop the run
# ---------------------------------------------------------------------------
class FatalError(UnlikerError):
    fatal = True
    code = "fatal"


class BrowserCrashedError(FatalError):
    """The browser or its page/context died."""

    code = "browser_crashed"


class UIChangedError(FatalError):
    """Nothing on the page matches any known selector.

    Raised rather than guessed around: clicking blindly on a page we cannot
    read is exactly how a tool like this does damage.
    """

    needs_human = True
    code = "ui_changed"


class CircuitBreakerTripped(FatalError):
    """Too many consecutive failures; stopping instead of hammering the site."""

    code = "circuit_breaker"


class AbortedByUser(UnlikerError):
    """A graceful stop was requested."""

    code = "aborted"


def classify(exc: BaseException) -> UnlikerError:
    """Map a third-party exception onto this taxonomy.

    Playwright is imported lazily and matched by class name so that the core
    package (and its unit tests) never require Playwright to be installed.
    """
    if isinstance(exc, UnlikerError):
        return exc

    name = type(exc).__name__
    text = str(exc)
    lowered = text.lower()

    if name == "TimeoutError" or "timeout" in lowered:
        if "waiting for selector" in lowered or "locator" in lowered:
            return ElementNotFoundError(text)
        return PageTimeoutError(text)

    if any(
        marker in lowered
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser closed",
            "connection closed",
            "target crashed",
            "page crashed",
        )
    ):
        return BrowserCrashedError(text)

    if any(
        marker in lowered
        for marker in (
            "net::err_",
            "nameresolutionerror",
            "connection refused",
            "connection reset",
            "temporary failure in name resolution",
            "socket hang up",
        )
    ):
        return NetworkError(text)

    if "element is not attached" in lowered or "node is detached" in lowered:
        return StaleElementError(text)

    if "strict mode violation" in lowered or "resolved to" in lowered:
        return ElementNotFoundError(text)

    return TransientError(f"{name}: {text}")
