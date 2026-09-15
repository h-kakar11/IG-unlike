"""Progress accounting, observed throughput and the ETA."""

from __future__ import annotations

import pytest

from core.progress import (
    ProgressTracker,
    format_count,
    format_duration,
    format_rate,
)


class Clock:
    """A hand-cranked monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def tracker():
    clock = Clock()
    return ProgressTracker(clock=clock), clock


def test_counts_each_outcome(tracker):
    progress, _ = tracker
    progress.record_success()
    progress.record_success()
    progress.record_failure()
    progress.record_skip()
    progress.record_retry()

    assert (progress.processed, progress.successful, progress.failed, progress.skipped) == (4, 2, 1, 1)
    assert progress.retries == 1


def test_previewed_items_are_counted_separately(tracker):
    """A dry run processed nothing, and must not claim it did."""
    progress, _ = tracker
    progress.record_preview()
    progress.record_preview()

    assert progress.previewed == 2
    assert progress.processed == 2
    assert progress.successful == 0
    assert progress.skipped == 0


def test_no_estimate_before_two_observations(tracker):
    progress, clock = tracker
    assert progress.average_seconds() is None
    assert progress.eta_seconds(100) is None

    progress.record_success()
    assert progress.average_seconds() is None, "one point is not a rate"

    clock.advance(2.0)
    progress.record_success()
    assert progress.average_seconds() == pytest.approx(2.0)


def test_throughput_reflects_observed_completions(tracker):
    progress, clock = tracker
    for _ in range(10):
        clock.advance(1.5)
        progress.record_success()

    assert progress.average_seconds() == pytest.approx(1.5)
    assert progress.items_per_minute() == pytest.approx(40.0)


def test_eta_uses_measured_throughput_not_configured_delays(tracker):
    progress, clock = tracker
    for _ in range(5):
        clock.advance(2.0)
        progress.record_success()

    # 100 items at 2s each = 200s, whatever the configured delay happens to be.
    assert progress.eta_seconds(100) == pytest.approx(200.0)
    assert progress.eta_seconds(0) == 0.0


def test_the_window_forgets_old_measurements():
    """A slow start must not skew the estimate for ever."""
    clock = Clock()
    progress = ProgressTracker(window=5, clock=clock)
    for _ in range(5):  # a slow patch
        clock.advance(60.0)
        progress.record_success()
    for _ in range(5):  # then a fast one
        clock.advance(2.0)
        progress.record_success()

    assert progress.average_seconds() == pytest.approx(2.0, abs=0.1)


def test_failures_do_not_inflate_throughput(tracker):
    progress, clock = tracker
    clock.advance(1.0)
    progress.record_success()
    for _ in range(20):
        clock.advance(1.0)
        progress.record_failure()
    clock.advance(1.0)
    progress.record_success()

    # Two successes 21s apart: the rate reflects real progress, not attempts.
    assert progress.average_seconds() == pytest.approx(21.0)


def test_snapshot_carries_everything_the_display_needs(tracker):
    progress, clock = tracker
    for _ in range(3):
        clock.advance(1.0)
        progress.record_success()

    snapshot = progress.snapshot(total_recorded=250_000, remaining=249_997, status="running")

    assert snapshot.successful == 3
    assert snapshot.total_recorded == 250_000
    assert snapshot.remaining == 249_997
    assert snapshot.status == "running"
    assert snapshot.eta_seconds == pytest.approx(249_997.0)
    assert snapshot.eta_text != "unknown"


def test_elapsed_never_goes_negative(tracker):
    progress, clock = tracker
    clock.now -= 50
    assert progress.elapsed >= 0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "unknown"),
        (0, "0s"),
        (45, "45s"),
        (60, "1m 00s"),
        (3600, "1h 00m"),
        (4821, "1h 20m"),
        (303_660, "3d 12h 21m"),
        (-5, "0s"),
    ],
)
def test_duration_formatting(seconds, expected):
    assert format_duration(seconds) == expected


def test_count_formatting():
    assert format_count(1234567) == "1,234,567"
    assert format_count(0) == "0"
    assert format_count(None) == "-"


def test_rate_formatting():
    assert format_rate(None) == "measuring..."
    assert format_rate(0) == "measuring..."
    assert format_rate(42.4) == "42 items/min"
    assert format_rate(1.25) == "1.2 items/min"
