"""The batch loop: discover, claim, act, persist, pause, repeat.

Shape of a run::

    load batch  ->  identify liked items  ->  process a controlled number
         ^                                              |
         |                                       persist progress
         +--------------- load more  <------------------+

Invariants this module is responsible for:

* **Nothing destructive happens in dry-run mode.** The strategy is never even
  constructed until the run is live and confirmed.
* **Every outcome is committed before the next item starts.** A crash costs at
  most the item in flight, which comes back as ``pending`` on restart.
* **Failures are bounded.** Per-item attempts are capped; consecutive failures
  trip the rate controller's breaker; a stop is honoured at every step.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from core.errors import (
    AbortedByUser,
    AuthenticationRequiredError,
    BrowserCrashedError,
    CircuitBreakerTripped,
    ElementNotFoundError,
    RateLimitedError,
    UIChangedError,
    UnlikerError,
    classify,
)
from core.logging_setup import get_logger
from core.progress import ProgressSnapshot, ProgressTracker
from core.rate_controller import RateController, RateSettings
from database import Database, Item
from instagram.likes import LikedItem, LikesScanner, Outcome, UnlikeResult, build_strategy
from instagram.navigation import Navigator

log = get_logger("worker")

#: Consecutive "element not found" errors that mean the UI has changed rather
#: than that one render was slow.
UI_CHANGE_THRESHOLD = 5


class WorkerState(enum.Enum):
    IDLE = "idle"
    DISCOVERING = "discovering"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FINISHED = "finished"
    ERROR = "error"

    @property
    def is_active(self) -> bool:
        return self in (WorkerState.DISCOVERING, WorkerState.RUNNING, WorkerState.PAUSED)


@dataclass
class RunReport:
    """What a completed (or interrupted) run did."""

    session_id: int
    dry_run: bool
    discovered: int = 0
    processed: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    previewed: int = 0
    batches: int = 0
    stop_reason: str = ""
    state: WorkerState = WorkerState.IDLE

    def summary(self) -> str:
        if self.dry_run:
            return (
                f"DRY RUN — nothing was changed: discovered {self.discovered:,}, "
                f"previewed {self.previewed:,} item(s) in {self.batches} batch(es). "
                f"{self.stop_reason}".strip()
            )
        return (
            f"Live run: discovered {self.discovered:,}, processed {self.processed:,} "
            f"({self.successful:,} ok / {self.failed:,} failed / {self.skipped:,} "
            f"skipped) in {self.batches} batch(es). {self.stop_reason}".strip()
        )


class Worker:
    """Drives a full unliking (or dry) run.

    Parameters
    ----------
    navigator, scanner:
        The Instagram layer. Injectable so the worker can be unit-tested with
        fakes and no browser at all.
    database:
        Progress store. The worker holds no durable state of its own.
    config:
        Timing, batching and the all-important ``dry_run`` flag.
    on_progress:
        Called after every item with a :class:`ProgressSnapshot`, so the CLI
        can redraw without the worker knowing anything about the terminal.
    """

    def __init__(
        self,
        *,
        navigator: Navigator,
        scanner: LikesScanner,
        database: Database,
        config: Any,
        rate: RateController | None = None,
        progress: ProgressTracker | None = None,
        on_progress: Callable[[ProgressSnapshot], None] | None = None,
        on_event: Callable[[str, str], None] | None = None,
        strategy_factory: Callable[[], Any] | None = None,
    ):
        self.navigator = navigator
        self.scanner = scanner
        self.db = database
        self.config = config
        self.rate = rate or RateController(
            RateSettings.from_config(config),
            on_pause=lambda seconds, reason: self._emit(
                "pause", f"Pausing {seconds:.0f}s ({reason})"
            ),
        )
        self.progress = progress or ProgressTracker()
        self._on_progress = on_progress
        self._on_event = on_event
        self._strategy_factory = strategy_factory

        self.state = WorkerState.IDLE
        self.session_id: int | None = None
        self._strategy: Any = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._consecutive_not_found = 0
        #: Highest item id previewed by a dry run, so the read-only walk moves
        #: forward instead of re-reading the same pending rows for ever.
        self._preview_cursor = 0

    # ------------------------------------------------------------------
    # Control surface (thread-safe; the CLI calls these from its input thread)
    # ------------------------------------------------------------------
    def request_stop(self, reason: str = "stopped by user") -> None:
        """Ask the run to finish the current item and shut down cleanly."""
        if self.state in (WorkerState.STOPPED, WorkerState.FINISHED):
            return
        log.info("Stop requested: %s", reason)
        self._stop_reason = reason
        self._stop.set()
        self._pause.clear()
        self.rate.interrupt()  # cut short any long backoff
        if self.state.is_active:
            self.state = WorkerState.STOPPING

    def request_pause(self) -> None:
        if self.state is WorkerState.RUNNING:
            log.info("Pause requested")
            self._pause.set()
            self.state = WorkerState.PAUSED
            self._emit("pause", "Paused")

    def request_resume(self) -> None:
        if self.state is WorkerState.PAUSED:
            log.info("Resume requested")
            self._pause.clear()
            self.state = WorkerState.RUNNING
            self._emit("resume", "Resumed")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _wait_while_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.2)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise AbortedByUser(getattr(self, "_stop_reason", "stopped by user"))

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def _emit(self, kind: str, message: str) -> None:
        log.info("%s", message)
        if self._on_event:
            self._on_event(kind, message)
        if self.session_id is not None:
            try:
                self.db.log_event(kind, message, session_id=self.session_id)
            except Exception as exc:  # noqa: BLE001 - never fail a run on audit
                log.debug("Could not record event: %s", exc)

    def snapshot(self) -> ProgressSnapshot:
        stats = self.db.stats()
        return self.progress.snapshot(
            total_recorded=stats.total,
            remaining=stats.remaining,
            status=self.state.value,
        )

    def _publish(self) -> None:
        if self._on_progress:
            self._on_progress(self.snapshot())

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    def scan_only(self, *, target: int | None = None, record: bool = True) -> RunReport:
        """Phase 3: read-only discovery. Never touches a like."""
        self.session_id = self.db.start_session(dry_run=True)
        report = RunReport(session_id=self.session_id, dry_run=True)
        self.state = WorkerState.DISCOVERING
        self._emit("scan_start", "Scanning liked content...")
        try:
            items = self.scanner.discover(
                target=target,
                on_progress=lambda count: self._emit_scan_progress(count),
            )
            report.discovered = len(items)
            if record and items:
                self.db.record_discovered(items, session_id=self.session_id)
            self.state = WorkerState.FINISHED
            report.stop_reason = "Dry run complete. No likes were removed."
        except UnlikerError as exc:
            self.state = WorkerState.ERROR
            report.stop_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report.state = self.state
            self.db.end_session(self.session_id, stop_reason=report.stop_reason)
        return report

    def _emit_scan_progress(self, count: int) -> None:
        if self._on_event:
            self._on_event("scan_progress", f"Currently detected: {count}")

    def run(
        self,
        *,
        limit: int | None = None,
        discover_first: bool = True,
    ) -> RunReport:
        """Process pending work until it runs out, is stopped, or we back off.

        ``limit`` caps the number of items this run will process, on top of any
        configured ``max_items_per_session``.
        """
        dry_run = bool(self.config.dry_run)
        self.session_id = self.db.start_session(dry_run=dry_run)
        report = RunReport(session_id=self.session_id, dry_run=dry_run)

        # Recovery: anything a previous crash left mid-flight comes back now.
        recovered = self.db.recover_abandoned(
            self.session_id, self.config.stale_processing_timeout
        )
        if recovered:
            self._emit("recovery", f"Recovered {recovered} interrupted item(s)")

        ceiling = _min_positive(limit, self.config.max_items_per_session)
        self.state = WorkerState.RUNNING
        self._stop.clear()
        self.rate.clear_interrupt()

        try:
            if discover_first:
                report.discovered = self._discover(target=ceiling)

            while True:
                self._check_stop()
                self._wait_while_paused()
                self._check_stop()

                if ceiling is not None and report.processed >= ceiling:
                    report.stop_reason = f"Reached this run's limit of {ceiling:,} item(s)."
                    break

                remaining_allowance = (
                    None if ceiling is None else ceiling - report.processed
                )
                batch = self._claim(remaining_allowance)

                if not batch:
                    if self._top_up(report):
                        continue
                    report.stop_reason = "No pending items remain."
                    self.state = WorkerState.FINISHED
                    break

                report.batches += 1
                self._emit(
                    "batch_start",
                    f"Batch {report.batches}: processing {len(batch)} item(s)",
                )
                self._process_batch(batch, report)

                if self._stop.is_set():
                    break
                self.rate.after_batch(processed=len(batch))

        except AbortedByUser as exc:
            report.stop_reason = str(exc)
            self.state = WorkerState.STOPPED
        except CircuitBreakerTripped as exc:
            report.stop_reason = str(exc)
            self.state = WorkerState.ERROR
            self._emit("circuit_breaker", str(exc))
        except AuthenticationRequiredError as exc:
            report.stop_reason = str(exc)
            self.state = WorkerState.ERROR
            self._emit("auth_required", str(exc))
        except (UIChangedError, BrowserCrashedError) as exc:
            report.stop_reason = str(exc)
            self.state = WorkerState.ERROR
            self._emit("fatal", str(exc))
        finally:
            self._finalise(report)

        return report

    def _finalise(self, report: RunReport) -> None:
        """Graceful shutdown: nothing in flight is left claimed."""
        released = self.db.release_all_processing(reason="run ended")
        if released:
            self._emit("recovery", f"Returned {released} in-flight item(s) to pending")

        report.processed = self.progress.processed
        report.successful = self.progress.successful
        report.failed = self.progress.failed
        report.skipped = self.progress.skipped
        report.previewed = self.progress.previewed
        if self.state is WorkerState.STOPPING:
            self.state = WorkerState.STOPPED
        if not report.stop_reason:
            report.stop_reason = "Run ended."
        report.state = self.state

        if self.session_id is not None:
            self.db.end_session(self.session_id, stop_reason=report.stop_reason)
        self._publish()
        log.info("Run finished: %s", report.summary())

    # ------------------------------------------------------------------
    # Discovery and claiming
    # ------------------------------------------------------------------
    def _discover(self, *, target: int | None = None) -> int:
        """Fill the queue from the live page, ignoring already-settled items."""
        self.state = WorkerState.DISCOVERING
        self._emit("scan_start", "Loading liked content...")

        # The per-post strategy navigates away from the grid to do its work, so
        # never assume the likes surface is still the page we are looking at.
        if not self.navigator.on_likes_page(timeout=1.0):
            self.navigator.navigate_to_likes()

        # Ask for more than the batch needs so a few already-done items do not
        # leave the batch short.
        scroll_target = None
        if target is not None:
            scroll_target = target
        elif self.config.batch_size:
            scroll_target = self.config.batch_size * 2

        items = self.scanner.discover(
            target=scroll_target, on_progress=self._emit_scan_progress
        )
        new = self.db.record_discovered(items, session_id=self.session_id)
        self._emit(
            "scan_complete",
            f"Discovered {len(items):,} item(s) on the page; {new:,} newly recorded",
        )
        self.state = WorkerState.RUNNING
        return len(items)

    def _claim(self, allowance: int | None) -> list[Item]:
        """Take the next batch of work.

        A live run *claims* rows (pending -> processing) so a crash is
        recoverable. A dry run only reads them, leaving the queue intact for
        the real run that follows.
        """
        size = self.config.batch_size
        if allowance is not None:
            size = min(size, max(allowance, 0))
        if size < 1:
            return []
        if self.config.dry_run:
            return self.db.pending_after(self._preview_cursor, size)
        return self.db.claim_batch(
            size,
            session_id=self.session_id,
            max_attempts=self.config.max_retries + 1,
        )

    def _top_up(self, report: RunReport) -> bool:
        """Scroll for more items when the queue empties. True if any appeared."""
        if self._stop.is_set():
            return False
        self._emit("load_more", "Queue empty; loading more liked content...")
        try:
            before = self.db.stats().total
            found = self._discover()
            added = self.db.stats().total - before
        except UnlikerError as exc:
            log.warning("Could not load more content: %s", exc)
            return False
        report.discovered += found
        if added:
            self._emit("load_more", f"Loaded {added:,} more item(s)")
            return True
        return False

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def _ensure_strategy(self) -> Any:
        """Build the unlike strategy — only ever reached on a live run."""
        if self.config.dry_run:
            raise AssertionError("dry-run must never construct an unlike strategy")
        if self._strategy is None:
            self._strategy = (
                self._strategy_factory()
                if self._strategy_factory
                else build_strategy(self.navigator, self.config, self.scanner.selectors)
            )
            self._emit("strategy", f"Using the '{self._strategy.name}' unlike strategy")
        return self._strategy

    def _process_batch(self, batch: Sequence[Item], report: RunReport) -> None:
        for item in batch:
            self._check_stop()
            self._wait_while_paused()
            self._check_stop()
            self._process_item(item, report)
            self._publish()

    def _process_item(self, item: Item, report: RunReport) -> None:
        liked = LikedItem(
            identifier=item.content_identifier,
            url=item.content_url,
            media_type=item.media_type,
        )

        if self.config.dry_run:
            # Rehearse the real loop without touching Instagram or the queue,
            # so that enabling live mode later still has a full queue to work on.
            self._preview_cursor = max(self._preview_cursor, item.id)
            self.progress.record_preview()
            report.previewed += 1
            report.processed += 1
            log.info("[dry run] would unlike %s", item.content_identifier)
            return

        self.rate.before_action()
        self._check_stop()

        try:
            result = self._ensure_strategy().unlike(liked)
        except RateLimitedError as exc:
            self._handle_rate_limit(item, exc)
            return
        except UnlikerError as exc:
            self._handle_failure(item, exc, report)
            return
        except Exception as exc:  # noqa: BLE001 - normalise anything unexpected
            self._handle_failure(item, classify(exc), report)
            return

        self._consecutive_not_found = 0
        self._record_result(item, result, report)

    def _record_result(self, item: Item, result: UnlikeResult, report: RunReport) -> None:
        report.processed += 1
        if result.outcome is Outcome.COMPLETED:
            self.db.mark_completed(item.id, session_id=self.session_id)
            self.progress.record_success()
            self.rate.on_success()
            log.info("Item %s completed", item.content_identifier)
        elif result.outcome is Outcome.SKIPPED:
            self.db.mark_skipped(item.id, result.detail, session_id=self.session_id)
            self.progress.record_skip()
            self.rate.on_success()  # a skip is a healthy interaction, not a failure
            log.info("Item %s skipped: %s", item.content_identifier, result.detail)
        else:
            self.db.mark_failed(
                item.id,
                result.detail,
                error_code=result.error_code or "failed",
                session_id=self.session_id,
            )
            self.progress.record_failure()
            self.rate.on_failure()
            log.warning("Item %s failed: %s", item.content_identifier, result.detail)

    def _handle_rate_limit(self, item: Item, exc: RateLimitedError) -> None:
        """Return the item to the queue and let the rate controller back off."""
        self.db.release_for_retry(
            item.id,
            str(exc),
            error_code=exc.code,
            # Throttling is not the item's fault, so it costs the item nothing:
            # neither a terminal failure nor a slot in its retry budget.
            max_attempts=None,
            count_attempt=False,
            session_id=self.session_id,
        )
        self.progress.record_retry()
        self._emit("rate_limited", "Rate limiting detected. Pausing automatically...")
        self.rate.on_rate_limited(exc)  # may raise CircuitBreakerTripped
        self._emit("rate_limited", "Retrying later.")

    def _handle_failure(self, item: Item, exc: UnlikerError, report: RunReport) -> None:
        identifier = item.content_identifier

        if isinstance(exc, ElementNotFoundError):
            self._consecutive_not_found += 1
            if self._consecutive_not_found >= UI_CHANGE_THRESHOLD:
                raise UIChangedError(
                    f"{self._consecutive_not_found} items in a row had no usable "
                    "control. Instagram's interface has probably changed — stopping "
                    "before doing anything unpredictable. Update selectors.json."
                ) from exc
        else:
            self._consecutive_not_found = 0

        if exc.fatal or exc.needs_human:
            self.db.release_for_retry(
                item.id, str(exc), error_code=exc.code, session_id=self.session_id
            )
            raise exc

        if exc.retryable:
            status = self.db.release_for_retry(
                item.id,
                str(exc),
                error_code=exc.code,
                max_attempts=self.config.max_retries,
                session_id=self.session_id,
            )
            if status == "failed":
                report.processed += 1
                self.progress.record_failure()
                log.error(
                    "Item %s failed permanently after %d attempt(s): %s",
                    identifier,
                    self.config.max_retries,
                    exc,
                )
            else:
                self.progress.record_retry()
                log.warning("Item %s failed: %s", identifier, exc)
                log.info("Retrying...")
            self.rate.on_failure(exc)
            return

        self.db.mark_failed(
            item.id, str(exc), error_code=exc.code, session_id=self.session_id
        )
        report.processed += 1
        self.progress.record_failure()
        self.rate.on_failure(exc)
        log.error("Item %s failed: %s", identifier, exc)


def _min_positive(*values: int | None) -> int | None:
    """Smallest positive value, or None when all are absent/zero."""
    candidates = [v for v in values if v is not None and v > 0]
    return min(candidates) if candidates else None
