"""Progress accounting, observed throughput and ETA.

The estimate is deliberately derived from measured completions in a rolling
window rather than from the configured delays: real throughput includes
backoff, batch pauses, retries and page loads, so a theoretical rate would be
optimistic by a wide margin. The number is still an estimate and is labelled
as one everywhere it is shown.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque

#: Completions considered when computing throughput. Large enough to smooth
#: out a single slow item, small enough to react to a backoff within minutes.
DEFAULT_WINDOW = 200


@dataclass
class ProgressSnapshot:
    """Everything the CLI needs to draw a status screen."""

    processed: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    retries: int = 0
    previewed: int = 0
    session_processed: int = 0
    total_recorded: int = 0
    remaining: int = 0
    elapsed: float = 0.0
    items_per_minute: float | None = None
    average_seconds: float | None = None
    eta_seconds: float | None = None
    status: str = "idle"

    @property
    def eta_text(self) -> str:
        return format_duration(self.eta_seconds) if self.eta_seconds else "unknown"


class ProgressTracker:
    """Counts outcomes and derives throughput from a rolling time window."""

    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._window: Deque[float] = deque(maxlen=max(window, 2))
        self.started_at = clock()
        self.processed = 0
        self.successful = 0
        self.failed = 0
        self.skipped = 0
        self.retries = 0
        self.previewed = 0
        self.status = "idle"

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_success(self) -> None:
        self.processed += 1
        self.successful += 1
        self._window.append(self._clock())

    def record_failure(self) -> None:
        self.processed += 1
        self.failed += 1

    def record_skip(self) -> None:
        self.processed += 1
        self.skipped += 1

    def record_retry(self) -> None:
        self.retries += 1

    def record_preview(self) -> None:
        """A dry-run item: counted, but nothing was done to it."""
        self.processed += 1
        self.previewed += 1

    # ------------------------------------------------------------------
    # Derived figures
    # ------------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return max(self._clock() - self.started_at, 0.0)

    def average_seconds(self) -> float | None:
        """Mean seconds between successful unlikes in the current window.

        ``None`` until at least two successes have been observed — an ETA from
        a single data point would be noise presented as information.
        """
        if len(self._window) < 2:
            return None
        span = self._window[-1] - self._window[0]
        if span <= 0:
            return None
        return span / (len(self._window) - 1)

    def items_per_minute(self) -> float | None:
        average = self.average_seconds()
        if not average:
            return None
        return 60.0 / average

    def eta_seconds(self, remaining: int) -> float | None:
        """Approximate seconds to process ``remaining`` items."""
        if remaining <= 0:
            return 0.0
        average = self.average_seconds()
        if not average:
            return None
        return remaining * average

    def snapshot(
        self,
        *,
        total_recorded: int = 0,
        remaining: int = 0,
        status: str | None = None,
    ) -> ProgressSnapshot:
        return ProgressSnapshot(
            processed=self.processed,
            successful=self.successful,
            failed=self.failed,
            skipped=self.skipped,
            retries=self.retries,
            previewed=self.previewed,
            session_processed=self.processed,
            total_recorded=total_recorded,
            remaining=remaining,
            elapsed=self.elapsed,
            items_per_minute=self.items_per_minute(),
            average_seconds=self.average_seconds(),
            eta_seconds=self.eta_seconds(remaining),
            status=status or self.status,
        )


# ----------------------------------------------------------------------
# Formatting helpers (shared by the CLI)
# ----------------------------------------------------------------------
def format_duration(seconds: float | None) -> str:
    """``None`` -> "unknown"; 4821 -> "1h 20m"; 45 -> "45s"."""
    if seconds is None:
        return "unknown"
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h {minutes:02d}m"


def format_count(value: int | float | None) -> str:
    """Thousands separators, because these numbers get large."""
    if value is None:
        return "-"
    return f"{int(value):,}"


def format_rate(items_per_minute: float | None) -> str:
    if not items_per_minute:
        return "measuring..."
    if items_per_minute >= 10:
        return f"{items_per_minute:.0f} items/min"
    return f"{items_per_minute:.1f} items/min"
