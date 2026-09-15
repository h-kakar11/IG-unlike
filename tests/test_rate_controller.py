"""Rate control: pacing, exponential backoff and the circuit breakers.

Every test injects sleep and randomness, so the suite runs instantly and the
assertions are about the *policy*, not about wall-clock timing.
"""

from __future__ import annotations

import pytest

from core.errors import CircuitBreakerTripped, RateLimitedError
from core.rate_controller import RateController, RateSettings


@pytest.fixture
def controller():
    """A controller with deterministic randomness and recorded sleeps."""

    def _make(**overrides):
        waits: list[float] = []
        values = {
            "min_delay": 2.0,
            "max_delay": 6.0,
            "backoff_initial": 30.0,
            "backoff_factor": 2.0,
            "backoff_max": 1800.0,
            "max_consecutive_failures": 5,
            "max_rate_limit_hits": 4,
        }
        values.update(overrides)
        settings = RateSettings(**values)
        rate = RateController(
            settings,
            sleep=waits.append,
            rand=lambda low, high: (low + high) / 2,  # midpoint, not random
        )
        return rate, waits

    return _make


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------
def test_delay_sits_inside_the_configured_window(controller):
    rate, waits = controller()
    rate.before_action()
    assert waits == [4.0]  # midpoint of 2..6


def test_delays_are_randomised_across_the_window():
    """Real randomness: every delay must land inside the configured bounds."""
    waits: list[float] = []
    rate = RateController(RateSettings(min_delay=2.0, max_delay=6.0), sleep=waits.append)
    for _ in range(50):
        rate.before_action()

    assert all(2.0 <= wait <= 6.0 for wait in waits)
    assert len(set(waits)) > 1, "a fixed delay is a fingerprint; vary it"


def test_batch_pause_is_applied(controller):
    rate, waits = controller(pause_after_batch=120.0)
    assert rate.after_batch(processed=25) == 120.0
    assert waits == [120.0]


def test_zero_batch_pause_does_not_sleep(controller):
    rate, waits = controller(pause_after_batch=0.0)
    assert rate.after_batch() == 0.0
    assert waits == []


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
def test_rate_limiting_backs_off_exponentially(controller):
    rate, waits = controller()
    for _ in range(3):
        rate.on_rate_limited(RateLimitedError())

    # 30, 60, 120 with the midpoint jitter factor of 0.875 applied.
    assert waits == pytest.approx([26.25, 52.5, 105.0])
    assert rate.state.backoff_level == 3


def test_backoff_is_capped(controller):
    rate, waits = controller(backoff_max=100.0, max_rate_limit_hits=99)
    for _ in range(8):
        rate.on_rate_limited(RateLimitedError())

    assert max(waits) <= 100.0
    assert waits[-1] == pytest.approx(87.5)  # the cap, jittered


def test_retry_after_from_the_site_is_respected(controller):
    rate, waits = controller()
    rate.on_rate_limited(RateLimitedError(retry_after=45.0))
    assert waits == [45.0]


def test_retry_after_is_still_capped(controller):
    rate, waits = controller(backoff_max=60.0)
    rate.on_rate_limited(RateLimitedError(retry_after=9999.0))
    assert waits == [60.0]


def test_jitter_never_shortens_a_wait_below_three_quarters():
    rate = RateController(
        RateSettings(backoff_initial=100.0, backoff_factor=2.0, max_rate_limit_hits=99),
        sleep=lambda _: None,
    )
    rate.state.backoff_level = 1
    for _ in range(100):
        assert 75.0 <= rate.backoff_wait() <= 100.0


def test_per_item_retry_waits_grow_and_cap(controller):
    rate, _ = controller(backoff_max=240.0)
    assert [rate.retry_wait(n) for n in (1, 2, 3, 4, 5)] == [30, 60, 120, 240, 240]


def test_delays_grow_while_backed_off(controller):
    rate, waits = controller()
    rate.on_rate_limited(RateLimitedError())
    waits.clear()
    rate.before_action()
    assert waits == [8.0], "the inter-action delay doubles at backoff level 1"


def test_success_eases_the_backoff(controller):
    rate, _ = controller()
    rate.on_rate_limited(RateLimitedError())
    rate.on_rate_limited(RateLimitedError())
    assert rate.state.backoff_level == 2

    rate.on_success()
    assert rate.state.backoff_level == 1
    rate.on_success()
    rate.on_success()
    assert rate.state.backoff_level == 0, "backoff must not go negative"


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------
def test_consecutive_failures_trip_the_breaker(controller):
    rate, _ = controller(max_consecutive_failures=3)
    rate.on_failure()
    rate.on_failure()
    with pytest.raises(CircuitBreakerTripped, match="consecutive failures"):
        rate.on_failure()


def test_a_success_resets_the_failure_run(controller):
    rate, _ = controller(max_consecutive_failures=3)
    rate.on_failure()
    rate.on_failure()
    rate.on_success()
    rate.on_failure()
    rate.on_failure()  # would have tripped without the reset
    assert rate.state.consecutive_failures == 2


def test_repeated_rate_limiting_stops_rather_than_hammering(controller):
    rate, _ = controller(max_rate_limit_hits=3)
    rate.on_rate_limited(RateLimitedError())
    rate.on_rate_limited(RateLimitedError())
    with pytest.raises(CircuitBreakerTripped, match="Rate limiting detected"):
        rate.on_rate_limited(RateLimitedError())


def test_should_retry_follows_the_budget(controller):
    rate, _ = controller(max_retries=3)
    assert [rate.should_retry(n) for n in (1, 2, 3, 4)] == [True, True, True, False]


# ---------------------------------------------------------------------------
# Interruption
# ---------------------------------------------------------------------------
def test_interrupt_cuts_a_long_wait_short():
    """A stop request must not have to wait out a 30-minute backoff."""
    rate = RateController(RateSettings(min_delay=600, max_delay=600))
    rate.interrupt()

    import time

    started = time.monotonic()
    rate.before_action()
    assert time.monotonic() - started < 1.0


def test_reset_clears_state(controller):
    rate, _ = controller()
    rate.on_rate_limited(RateLimitedError())
    rate.on_failure()
    rate.interrupt()

    rate.reset()

    assert rate.state.backoff_level == 0
    assert rate.state.consecutive_failures == 0
    assert rate.state.rate_limit_hits == 0
    assert not rate.interrupted


def test_snapshot_is_reportable(controller):
    rate, _ = controller()
    rate.on_rate_limited(RateLimitedError())
    snapshot = rate.snapshot()

    assert snapshot["rate_limit_hits"] == 1
    assert snapshot["backoff_multiplier"] == 2.0
    assert snapshot["last_pause_reason"] == "rate limited"


def test_pause_callback_is_notified(controller):
    seen: list[tuple[float, str]] = []
    rate = RateController(
        RateSettings(min_delay=5, max_delay=5),
        sleep=lambda _: None,
        on_pause=lambda seconds, reason: seen.append((seconds, reason)),
    )
    rate.before_action()
    assert seen == [(5.0, "inter-action delay")]


def test_settings_come_from_config():
    from config import Config

    config = Config.load(env={}, overrides={"min_delay": 4, "max_delay": 9, "batch_size": 40})
    settings = RateSettings.from_config(config)

    assert (settings.min_delay, settings.max_delay, settings.batch_size) == (4.0, 9.0, 40)
