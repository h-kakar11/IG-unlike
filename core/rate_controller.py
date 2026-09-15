"""Deliberate pacing and backoff.

The goal of this module is the opposite of the usual one: it exists to make
the tool *slower* and to make it stop. It never tries to find the fastest safe
rate, it never probes a limit, and it has no mechanism for evading one. When
Instagram signals that it wants fewer actions, the only response implemented
here is to wait longer and, past a threshold, to stop entirely.

All timing and randomness is injectable so the behaviour can be unit-tested
without real waits.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from core.errors import CircuitBreakerTripped, RateLimitedError
from core.logging_setup import get_logger

log = get_logger("rate")


@dataclass
class RateSettings:
    """Tunables, mirroring the matching names in :class:`config.Config`."""

    min_delay: float = 3.0
    max_delay: float = 7.0
    batch_size: int = 25
    pause_after_batch: float = 90.0
    max_retries: int = 3
    backoff_factor: float = 2.0
    backoff_initial: float = 30.0
    backoff_max: float = 1800.0
    max_consecutive_failures: int = 10
    max_rate_limit_hits: int = 5

    @classmethod
    def from_config(cls, config) -> "RateSettings":
        return cls(
            min_delay=config.min_delay,
            max_delay=config.max_delay,
            batch_size=config.batch_size,
            pause_after_batch=config.pause_after_batch,
            max_retries=config.max_retries,
            backoff_factor=config.backoff_factor,
            backoff_initial=config.backoff_initial,
            backoff_max=config.backoff_max,
            max_consecutive_failures=config.max_consecutive_failures,
            max_rate_limit_hits=config.max_rate_limit_hits,
        )


@dataclass
class RateState:
    """Observable counters, surfaced in the CLI and the logs."""

    actions: int = 0
    consecutive_failures: int = 0
    rate_limit_hits: int = 0
    backoff_level: int = 0
    total_wait: float = 0.0
    last_pause_reason: str = ""


class RateController:
    """Spaces actions apart and backs off when the site pushes back.

    Parameters
    ----------
    settings:
        Timing policy.
    sleep:
        Injected sleep. Defaults to an interruptible sleep backed by a
        :class:`threading.Event` so a stop request does not have to wait out a
        30-minute backoff.
    rand:
        Injected ``random.uniform``-alike, for deterministic tests.
    on_pause:
        Optional callback ``(seconds, reason)`` used by the CLI to show
        "Pausing automatically..." while a wait is in progress.
    """

    def __init__(
        self,
        settings: RateSettings | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        rand: Callable[[float, float], float] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_pause: Callable[[float, str], None] | None = None,
    ):
        self.settings = settings or RateSettings()
        self.state = RateState()
        self._interrupt = threading.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._rand = rand or random.uniform
        self._monotonic = monotonic
        self._on_pause = on_pause

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------
    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately if :meth:`interrupt` is called."""
        if seconds > 0:
            self._interrupt.wait(seconds)

    def interrupt(self) -> None:
        """Abort any in-progress wait (used by graceful shutdown)."""
        self._interrupt.set()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    def _pause(self, seconds: float, reason: str) -> float:
        if seconds <= 0:
            return 0.0
        self.state.total_wait += seconds
        self.state.last_pause_reason = reason
        if self._on_pause:
            self._on_pause(seconds, reason)
        log.debug("Waiting %.1fs (%s)", seconds, reason)
        self._sleep(seconds)
        return seconds

    # ------------------------------------------------------------------
    # The action cycle
    # ------------------------------------------------------------------
    def next_delay(self) -> float:
        """The delay that would be used before the next action."""
        base = self._rand(self.settings.min_delay, self.settings.max_delay)
        return base * self.backoff_multiplier()

    def backoff_multiplier(self) -> float:
        return float(self.settings.backoff_factor) ** self.state.backoff_level

    def before_action(self) -> float:
        """Wait the inter-action delay. Call once per attempted item."""
        delay = self.next_delay()
        self._pause(delay, "inter-action delay")
        return delay

    def after_batch(self, *, processed: int = 0) -> float:
        """Idle between batches so the run is bursty-with-rests, not constant."""
        if self.settings.pause_after_batch <= 0:
            return 0.0
        log.info(
            "Batch complete (%d item(s)); pausing %.0fs before the next batch",
            processed,
            self.settings.pause_after_batch,
        )
        return self._pause(self.settings.pause_after_batch, "batch pause")

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------
    def on_success(self) -> None:
        """Record a successful action and relax the backoff by one level."""
        self.state.actions += 1
        self.state.consecutive_failures = 0
        if self.state.backoff_level > 0:
            self.state.backoff_level -= 1
            log.info(
                "Recovered; easing backoff to level %d (x%.1f)",
                self.state.backoff_level,
                self.backoff_multiplier(),
            )

    def on_failure(self, error: BaseException | None = None) -> None:
        """Record a failed action and trip the breaker if they keep coming."""
        self.state.actions += 1
        self.state.consecutive_failures += 1
        if self.state.consecutive_failures >= self.settings.max_consecutive_failures:
            raise CircuitBreakerTripped(
                f"{self.state.consecutive_failures} consecutive failures; stopping "
                "rather than continuing to retry. Check the browser window and "
                "the log, then resume when the cause is understood."
            )

    def on_rate_limited(self, error: RateLimitedError | None = None) -> float:
        """Handle an explicit throttle signal: back off exponentially, or stop.

        Returns the number of seconds waited.
        """
        self.state.rate_limit_hits += 1
        self.state.consecutive_failures += 1
        if self.state.rate_limit_hits >= self.settings.max_rate_limit_hits:
            raise CircuitBreakerTripped(
                f"Rate limiting detected {self.state.rate_limit_hits} times in this "
                "session. Stopping safely — progress is saved. Wait several hours "
                "before resuming."
            )
        self.state.backoff_level += 1
        wait = self.backoff_wait(retry_after=getattr(error, "retry_after", None))
        log.warning(
            "Rate limiting detected. Pausing automatically for %.0fs (level %d). "
            "Retrying later.",
            wait,
            self.state.backoff_level,
        )
        self._pause(wait, "rate limited")
        return wait

    def backoff_wait(self, *, retry_after: float | None = None) -> float:
        """Exponential backoff for the current level, capped and jittered.

        ``retry_after`` (if the site told us how long to wait) always wins,
        except that it is still capped by ``backoff_max``.
        """
        if retry_after is not None and retry_after > 0:
            return min(float(retry_after), self.settings.backoff_max)
        level = max(self.state.backoff_level - 1, 0)
        raw = self.settings.backoff_initial * (self.settings.backoff_factor ** level)
        capped = min(raw, self.settings.backoff_max)
        # Full jitter in the top 25%: staggers retries without ever shortening
        # the wait below 75% of the computed value.
        return capped * self._rand(0.75, 1.0)

    def retry_wait(self, attempt: int) -> float:
        """Backoff for the ``attempt``-th retry of a single item (1-based)."""
        raw = self.settings.backoff_initial * (
            self.settings.backoff_factor ** max(attempt - 1, 0)
        )
        return min(raw, self.settings.backoff_max)

    def wait_before_retry(self, attempt: int) -> float:
        return self._pause(self.retry_wait(attempt), f"retry #{attempt}")

    def should_retry(self, attempts: int) -> bool:
        """True while the item still has attempts left in its budget."""
        return attempts <= self.settings.max_retries

    def reset(self) -> None:
        """Forget accumulated backoff (used when starting a fresh session)."""
        self.state = RateState()
        self.clear_interrupt()

    def snapshot(self) -> dict[str, float | int | str]:
        return {
            "actions": self.state.actions,
            "consecutive_failures": self.state.consecutive_failures,
            "rate_limit_hits": self.state.rate_limit_hits,
            "backoff_level": self.state.backoff_level,
            "backoff_multiplier": round(self.backoff_multiplier(), 2),
            "total_wait_seconds": round(self.state.total_wait, 1),
            "last_pause_reason": self.state.last_pause_reason,
        }
