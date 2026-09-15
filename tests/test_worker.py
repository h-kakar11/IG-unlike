"""Worker control flow: batching, retries, state transitions, shutdown.

No browser: the Instagram layer is faked, so these run in milliseconds and
cover the paths that are hard to provoke against a live site.
"""

from __future__ import annotations

import threading

import pytest

from core.errors import (
    BrowserCrashedError,
    CheckpointError,
    ElementNotFoundError,
    NetworkError,
    PageTimeoutError,
    RateLimitedError,
    SessionExpiredError,
    VerificationFailedError,
)
from core.progress import ProgressTracker
from core.rate_controller import RateController, RateSettings
from core.worker import UI_CHANGE_THRESHOLD, Worker, WorkerState
from database import Database
from instagram.likes import Outcome, UnlikeResult
from tests.fakes import FakeNavigator, FakeScanner, FakeStrategy, fast_config, make_items


@pytest.fixture
def build(tmp_path):
    """``build(pages, outcomes, **config)`` -> ``(worker, strategy, db)``."""
    created: list[Database] = []

    def _build(pages=None, outcomes=None, default=None, **config_overrides):
        config = fast_config(tmp_path, **config_overrides)
        database = Database(config.db_path)
        created.append(database)
        strategy = FakeStrategy(outcomes, default=default)
        worker = Worker(
            navigator=FakeNavigator(),
            scanner=FakeScanner(pages=list(pages or [])),
            database=database,
            config=config,
            rate=RateController(
                RateSettings.from_config(config), sleep=lambda _: None
            ),
            progress=ProgressTracker(),
            strategy_factory=lambda: strategy,
        )
        return worker, strategy, database

    yield _build
    for database in created:
        database.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_processes_every_discovered_item(build):
    worker, strategy, db = build(pages=[make_items(5)], batch_size=2)

    report = worker.run()

    assert report.successful == 5
    assert report.failed == 0
    assert len(strategy.seen) == 5
    assert db.stats().completed == 5
    assert worker.state is WorkerState.FINISHED


def test_work_is_split_into_batches(build):
    worker, _, _ = build(pages=[make_items(10)], batch_size=3)
    report = worker.run()

    assert report.batches == 4  # 3 + 3 + 3 + 1
    assert report.successful == 10


def test_queue_is_topped_up_by_scrolling(build):
    """Instagram does not hand over all history at once; keep asking."""
    worker, strategy, _ = build(
        pages=[make_items(3, "p/A"), make_items(3, "p/B"), []], batch_size=3
    )

    report = worker.run()

    assert report.successful == 6
    assert {name[:3] for name in strategy.seen} == {"p/A", "p/B"}


def test_limit_caps_the_run(build):
    worker, strategy, db = build(pages=[make_items(10)], batch_size=4)

    report = worker.run(limit=6)

    assert report.successful == 6
    assert len(strategy.seen) == 6
    assert db.stats().pending == 4
    assert "limit" in report.stop_reason


def test_session_ceiling_is_honoured(build):
    worker, _, _ = build(pages=[make_items(10)], batch_size=4, max_items_per_session=3)
    assert worker.run().successful == 3


def test_skips_are_not_failures(build):
    worker, _, db = build(
        pages=[make_items(3)],
        outcomes=[
            UnlikeResult("x", Outcome.COMPLETED),
            UnlikeResult("x", Outcome.SKIPPED, "already not liked", "not_liked"),
            UnlikeResult("x", Outcome.COMPLETED),
        ],
    )

    report = worker.run()

    assert (report.successful, report.skipped, report.failed) == (2, 1, 0)
    assert db.stats().skipped == 1


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_dry_run_never_builds_a_strategy(build):
    worker, strategy, db = build(pages=[make_items(4)], dry_run=True)

    report = worker.run()

    assert strategy.seen == [], "dry run must not reach the unlike code at all"
    assert report.previewed == 4
    assert report.successful == 0
    assert db.stats().pending == 4, "the queue is left intact for the real run"


def test_dry_run_terminates(build):
    """Regression: previewing must advance, not re-read the same rows."""
    worker, _, _ = build(pages=[make_items(7)], dry_run=True, batch_size=2)
    report = worker.run()
    assert report.previewed == 7


def test_dry_run_summary_says_nothing_changed(build):
    worker, _, _ = build(pages=[make_items(2)], dry_run=True)
    assert "nothing was changed" in worker.run().summary()


# ---------------------------------------------------------------------------
# Retries and failures
# ---------------------------------------------------------------------------
def test_a_transient_error_is_retried_then_succeeds(build):
    worker, strategy, db = build(
        pages=[make_items(1)],
        outcomes=[PageTimeoutError("slow"), UnlikeResult("x", Outcome.COMPLETED)],
    )

    report = worker.run()

    assert report.successful == 1
    assert len(strategy.seen) == 2, "the same item was attempted twice"
    assert db.stats().completed == 1


def test_retries_are_bounded_and_end_in_failure(build):
    worker, strategy, db = build(
        pages=[make_items(1)], default=NetworkError("down"), max_retries=2
    )

    report = worker.run()

    assert report.failed == 1
    assert len(strategy.seen) == 3, "attempts stop at the budget"
    assert db.stats().failed == 1
    assert db.get_item("p/ITEM000").error_code == "network"


def test_an_unverified_click_is_retried_not_trusted(build):
    worker, _, db = build(
        pages=[make_items(1)],
        outcomes=[VerificationFailedError("no change"), UnlikeResult("x", Outcome.COMPLETED)],
    )

    worker.run()

    assert db.stats().completed == 1
    assert db.stats().failed == 0


def test_a_run_of_missing_elements_is_treated_as_a_ui_change(build):
    """Better to stop than to keep clicking on a page we cannot read."""
    worker, _, db = build(
        pages=[make_items(20)],
        default=ElementNotFoundError("gone"),
        batch_size=20,
        max_retries=0,
        max_consecutive_failures=99,
    )

    report = worker.run()

    assert worker.state is WorkerState.ERROR
    assert "interface has probably changed" in report.stop_reason
    assert "selectors.json" in report.stop_reason
    assert db.stats().processing == 0


def test_isolated_missing_elements_do_not_trip_the_ui_guard(build):
    outcomes = []
    for _ in range(UI_CHANGE_THRESHOLD + 2):
        outcomes.append(ElementNotFoundError("slow render"))
        outcomes.append(UnlikeResult("x", Outcome.COMPLETED))
    worker, _, _ = build(pages=[make_items(7)], outcomes=outcomes, batch_size=7)

    report = worker.run()

    assert worker.state is WorkerState.FINISHED
    assert report.successful == 7


def test_consecutive_failures_stop_the_run(build):
    worker, _, db = build(
        pages=[make_items(30)],
        default=VerificationFailedError("nope"),
        batch_size=30,
        max_retries=0,
        max_consecutive_failures=4,
    )

    report = worker.run()

    assert worker.state is WorkerState.ERROR
    assert "consecutive failures" in report.stop_reason
    assert db.stats().processing == 0, "nothing is left claimed"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limiting_requeues_the_item_and_backs_off(build):
    worker, strategy, db = build(
        pages=[make_items(2)],
        outcomes=[
            RateLimitedError("blocked"),
            UnlikeResult("x", Outcome.COMPLETED),
            UnlikeResult("x", Outcome.COMPLETED),
        ],
        batch_size=2,
    )

    report = worker.run()

    assert report.successful == 2
    assert worker.rate.state.rate_limit_hits == 1
    assert worker.rate.state.total_wait > 0, "the throttle should have caused a wait"
    assert worker.rate.state.last_pause_reason == "rate limited"
    # The backoff eased back to zero as the retries succeeded, which is the
    # point: a single throttle should not slow the rest of the run for ever.
    assert worker.rate.state.backoff_level == 0
    assert db.stats().completed == 2


def test_rate_limiting_does_not_spend_the_items_retry_budget(build):
    worker, _, db = build(
        pages=[make_items(1)],
        outcomes=[RateLimitedError(), RateLimitedError(), UnlikeResult("x", Outcome.COMPLETED)],
        max_retries=1,
        max_rate_limit_hits=9,
    )

    assert worker.run().successful == 1
    assert db.stats().completed == 1


def test_persistent_rate_limiting_stops_safely(build):
    worker, _, db = build(
        pages=[make_items(10)],
        default=RateLimitedError("blocked"),
        batch_size=10,
        max_rate_limit_hits=3,
    )

    report = worker.run()

    assert worker.state is WorkerState.ERROR
    assert "Rate limiting detected" in report.stop_reason
    assert db.stats().processing == 0
    assert db.stats().pending == 10, "no progress was lost"


# ---------------------------------------------------------------------------
# Conditions needing a human, and fatal ones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error", [SessionExpiredError("expired"), CheckpointError("challenge")]
)
def test_authentication_problems_stop_the_run_and_keep_progress(build, error):
    worker, _, db = build(
        pages=[make_items(5)],
        outcomes=[UnlikeResult("x", Outcome.COMPLETED), error],
        batch_size=5,
    )

    report = worker.run()

    assert worker.state is WorkerState.ERROR
    assert db.stats().completed == 1
    assert db.stats().processing == 0
    assert str(error) in report.stop_reason


def test_a_browser_crash_stops_the_run_cleanly(build):
    worker, _, db = build(
        pages=[make_items(4)], default=BrowserCrashedError("gone"), batch_size=4
    )

    worker.run()

    assert worker.state is WorkerState.ERROR
    assert db.stats().processing == 0


# ---------------------------------------------------------------------------
# Pause, resume, stop
# ---------------------------------------------------------------------------
def test_stop_is_graceful_and_leaves_nothing_claimed(build):
    worker, _, db = build(pages=[make_items(20)], batch_size=20)

    def stop_after_three(snapshot):
        if snapshot.successful == 3:
            worker.request_stop("enough")

    worker._on_progress = stop_after_three
    report = worker.run()

    assert worker.state is WorkerState.STOPPED
    assert report.successful == 3
    assert db.stats().completed == 3
    assert db.stats().processing == 0
    assert db.stats().pending == 17


def test_pause_then_resume_continues_the_run(build):
    worker, _, _ = build(pages=[make_items(6)], batch_size=6)
    resumed = threading.Event()

    def pause_once(snapshot):
        if snapshot.successful == 2 and not resumed.is_set():
            worker.request_pause()
            resumed.set()
            threading.Timer(0.05, worker.request_resume).start()

    worker._on_progress = pause_once
    report = worker.run()

    assert report.successful == 6
    assert worker.state is WorkerState.FINISHED


def test_stop_while_paused_still_exits(build):
    worker, _, _ = build(pages=[make_items(10)], batch_size=10)

    def pause_then_stop(snapshot):
        if snapshot.successful == 1:
            worker.request_pause()
            threading.Timer(0.05, lambda: worker.request_stop("done")).start()

    worker._on_progress = pause_then_stop
    report = worker.run()

    assert worker.state is WorkerState.STOPPED
    assert report.successful >= 1


def test_stop_interrupts_a_long_backoff(build):
    """A stop must not wait out a 30-minute rate-limit pause."""
    worker, _, _ = build(
        pages=[make_items(5)],
        default=RateLimitedError(),
        batch_size=5,
        backoff_initial=600.0,
        backoff_max=600.0,
    )
    worker.rate = RateController(RateSettings.from_config(worker.config))
    threading.Timer(0.2, lambda: worker.request_stop("stop")).start()

    import time

    started = time.monotonic()
    worker.run()

    assert time.monotonic() - started < 10
    assert worker.state is WorkerState.STOPPED


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------
def test_a_second_run_resumes_without_repeating_work(build, tmp_path):
    worker, strategy, db = build(pages=[make_items(10), []], batch_size=3)
    worker.run(limit=4)
    assert db.stats().completed == 4

    worker.scanner.pages = [[]]
    worker.progress = type(worker.progress)()
    second = worker.run(discover_first=False)

    assert second.successful == 6
    assert db.stats().completed == 10
    assert len(strategy.seen) == 10, "nothing was processed twice"


def test_a_crash_leaves_in_flight_items_recoverable(build):
    worker, _, db = build(pages=[make_items(6)], batch_size=6)
    db.record_discovered(make_items(6))
    db.claim_batch(3)  # simulate a crash mid-batch
    assert db.stats().processing == 3

    worker.scanner.pages = [[]]
    report = worker.run(discover_first=False)

    assert report.successful == 6
    assert db.stats().processing == 0


def test_scan_only_records_without_touching_anything(build):
    worker, strategy, db = build(pages=[make_items(8)], dry_run=True)

    report = worker.scan_only()

    assert report.discovered == 8
    assert strategy.seen == []
    assert db.stats().pending == 8
    assert "No likes were removed" in report.stop_reason


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_events_are_persisted_for_the_audit_trail(build):
    worker, _, db = build(pages=[make_items(2)], batch_size=2)
    worker.run()

    kinds = {event["kind"] for event in db.recent_events(limit=50)}
    assert "batch_start" in kinds
    assert "strategy" in kinds


def test_progress_callbacks_fire_for_every_item(build):
    seen = []
    worker, _, _ = build(pages=[make_items(4)], batch_size=4)
    worker._on_progress = seen.append
    worker.run()

    assert len(seen) >= 4
    assert seen[-1].successful == 4


def test_report_summary_is_readable(build):
    worker, _, _ = build(pages=[make_items(3)])
    summary = worker.run().summary()

    assert "Live run" in summary
    assert "3 ok" in summary
