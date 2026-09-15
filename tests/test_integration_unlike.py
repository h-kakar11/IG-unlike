"""End-to-end unliking against the mock site, with a real browser.

These exercise the part of the contract that matters most: an unlike is only
recorded as done when the page itself confirms the state changed.
"""

from __future__ import annotations

import pytest

from core.errors import RateLimitedError
from core.worker import Worker, WorkerState
from database import Database
from instagram.browser import BrowserSession
from instagram.likes import (
    LikedItem,
    LikesScanner,
    Outcome,
    SelectModeStrategy,
    SinglePostStrategy,
    build_strategy,
)
from instagram.navigation import Navigator
from tests.mock_instagram import MockInstagram

pytestmark = pytest.mark.integration


@pytest.fixture
def live(make_config, tmp_path):
    """``(navigator, scanner, config, db)`` on a live, non-dry-run browser."""
    sessions = []

    def _open(mock: MockInstagram, **overrides):
        overrides.setdefault("dry_run", False)
        config = make_config(mock.base_url, **overrides)
        session = BrowserSession(config).start()
        sessions.append(session)
        navigator = Navigator(session, config)
        navigator.ensure_authenticated(prompt=lambda _: None)
        navigator.navigate_to_likes()
        scanner = LikesScanner(navigator, config)
        database = Database(config.db_path)
        return navigator, scanner, config, database

    yield _open
    for session in sessions:
        session.stop()


# ---------------------------------------------------------------------------
# Single-post strategy
# ---------------------------------------------------------------------------
def test_single_unlike_is_verified_against_the_page(live):
    with MockInstagram(item_count=3) as mock:
        navigator, _, config, _ = live(mock, unlike_strategy="item")
        strategy = SinglePostStrategy(navigator, config)
        target = LikedItem("p/MOCK0001", f"{mock.base_url}/p/MOCK0001/", "post")

        result = strategy.unlike(target)

        assert result.outcome is Outcome.COMPLETED
        assert "p/MOCK0001" not in mock.liked
        assert len(mock.liked) == 2, "only the targeted item should be affected"


def test_already_unliked_item_is_skipped_not_failed(live):
    with MockInstagram(item_count=2) as mock:
        navigator, _, config, _ = live(mock, unlike_strategy="item")
        strategy = SinglePostStrategy(navigator, config)
        target = LikedItem("p/MOCK0000", f"{mock.base_url}/p/MOCK0000/", "post")

        assert strategy.unlike(target).outcome is Outcome.COMPLETED
        second = strategy.unlike(target)

        assert second.outcome is Outcome.SKIPPED
        assert "already not liked" in second.detail


def test_unconfirmed_click_is_not_reported_as_success(live):
    """A UI that swallows the click must never be recorded as completed."""
    with MockInstagram(item_count=2, sticky={"p/MOCK0000"}) as mock:
        navigator, _, config, _ = live(mock, unlike_strategy="item")
        strategy = SinglePostStrategy(navigator, config)
        target = LikedItem("p/MOCK0000", f"{mock.base_url}/p/MOCK0000/", "post")

        with pytest.raises(Exception) as excinfo:
            strategy.unlike(target)

        assert "never changed" in str(excinfo.value)
        assert "p/MOCK0000" in mock.liked


def test_rate_limit_message_becomes_a_rate_limit_error(live):
    with MockInstagram(item_count=3, rate_limit_after=0) as mock:
        navigator, _, config, _ = live(mock, unlike_strategy="item")
        strategy = SinglePostStrategy(navigator, config)
        target = LikedItem("p/MOCK0000", f"{mock.base_url}/p/MOCK0000/", "post")

        with pytest.raises(RateLimitedError):
            strategy.unlike(target)

        assert mock.liked == ["p/MOCK0000", "p/MOCK0001", "p/MOCK0002"]


# ---------------------------------------------------------------------------
# Select-mode strategy
# ---------------------------------------------------------------------------
def test_select_mode_unlikes_a_chunk(live):
    with MockInstagram(item_count=6, page_size=6) as mock:
        navigator, scanner, config, _ = live(mock, unlike_strategy="select")
        strategy = SelectModeStrategy(navigator, config)
        targets = scanner.scan_visible()[:3]

        results = strategy.unlike_many(targets)

        assert [r.outcome for r in results] == [Outcome.COMPLETED] * 3
        assert len(mock.liked) == 3
        for target in targets:
            assert target.identifier not in mock.liked


def test_select_mode_is_used_automatically_when_available(live):
    with MockInstagram(item_count=4) as mock:
        navigator, scanner, config, _ = live(mock, unlike_strategy="auto")
        assert build_strategy(navigator, config, scanner.selectors).name == "select"


def test_auto_falls_back_when_select_is_absent(live):
    with MockInstagram(item_count=4, select_mode_enabled=False) as mock:
        navigator, scanner, config, _ = live(mock, unlike_strategy="auto")
        assert build_strategy(navigator, config, scanner.selectors).name == "item"


# ---------------------------------------------------------------------------
# Worker over the real stack
# ---------------------------------------------------------------------------
def test_dry_run_leaves_instagram_and_the_queue_untouched(live):
    with MockInstagram(item_count=8, page_size=8) as mock:
        navigator, scanner, config, db = live(mock, dry_run=True, batch_size=4)
        worker = Worker(navigator=navigator, scanner=scanner, database=db, config=config)

        report = worker.run()

        assert mock.state.unlike_calls == 0
        assert len(mock.liked) == 8
        assert report.previewed == 8
        assert report.successful == 0
        # Crucially: the queue is still full, ready for a later live run.
        assert db.stats().pending == 8
        assert "nothing was changed" in report.summary()


def test_live_run_unlikes_everything_and_records_it(live):
    with MockInstagram(item_count=9, page_size=5) as mock:
        navigator, scanner, config, db = live(
            mock, batch_size=4, unlike_strategy="item"
        )
        worker = Worker(navigator=navigator, scanner=scanner, database=db, config=config)

        report = worker.run()

        assert mock.liked == []
        assert report.successful == 9
        assert report.failed == 0
        assert db.stats().completed == 9
        assert db.stats().remaining == 0
        assert worker.state is WorkerState.FINISHED


def test_run_respects_its_limit(live):
    with MockInstagram(item_count=10, page_size=10) as mock:
        navigator, scanner, config, db = live(
            mock, batch_size=3, unlike_strategy="item"
        )
        worker = Worker(navigator=navigator, scanner=scanner, database=db, config=config)

        report = worker.run(limit=4)

        assert report.successful == 4
        assert len(mock.liked) == 6
        assert db.stats().pending >= 6


def test_interrupted_run_resumes_without_repeating_work(live, tmp_path):
    """The recovery requirement: stop at N, restart, continue from N."""
    with MockInstagram(item_count=10, page_size=10) as mock:
        navigator, scanner, config, db = live(
            mock, batch_size=2, unlike_strategy="item"
        )

        first = Worker(navigator=navigator, scanner=scanner, database=db, config=config)
        first.run(limit=4)
        assert first.progress.successful == 4
        completed_after_first = db.stats().completed

        # A crash leaves rows claimed; simulate that before resuming.
        db.claim_batch(2)
        assert db.stats().processing == 2

        second = Worker(navigator=navigator, scanner=scanner, database=db, config=config)
        report = second.run()

        assert mock.liked == []
        assert completed_after_first == 4
        assert db.stats().completed == 10
        # 10 unlike calls total, not 14: nothing was processed twice.
        assert mock.state.unlike_calls == 10
        assert report.successful == 6


def test_stop_is_graceful_and_preserves_progress(live):
    with MockInstagram(item_count=12, page_size=12) as mock:
        navigator, scanner, config, db = live(
            mock, batch_size=10, unlike_strategy="item"
        )
        worker = Worker(navigator=navigator, scanner=scanner, database=db, config=config)

        def stop_after_three(snapshot):
            if snapshot.successful >= 3:
                worker.request_stop("stopped by test")

        worker._on_progress = stop_after_three
        report = worker.run()

        assert worker.state is WorkerState.STOPPED
        assert report.successful >= 3
        assert db.stats().processing == 0, "no item may be left claimed"
        assert db.stats().completed == report.successful
        assert db.stats().pending == 12 - report.successful


def test_rate_limiting_backs_off_and_stops_safely(live):
    with MockInstagram(item_count=8, page_size=8, rate_limit_after=2) as mock:
        navigator, scanner, config, db = live(
            mock,
            batch_size=8,
            unlike_strategy="item",
            max_rate_limit_hits=2,
        )
        worker = Worker(navigator=navigator, scanner=scanner, database=db, config=config)

        report = worker.run()

        assert "Rate limiting detected" in report.stop_reason
        assert worker.state is WorkerState.ERROR
        assert db.stats().completed == 2
        # The blocked item went back to the queue rather than being lost.
        assert db.stats().pending == 6
        assert db.stats().processing == 0


def test_session_expiry_mid_run_stops_and_keeps_progress(live):
    with MockInstagram(item_count=8, page_size=8) as mock:
        navigator, scanner, config, db = live(
            mock, batch_size=8, unlike_strategy="item"
        )
        worker = Worker(navigator=navigator, scanner=scanner, database=db, config=config)

        def expire_after_two(snapshot):
            if snapshot.successful == 2:
                mock.log_out()

        worker._on_progress = expire_after_two
        report = worker.run()

        assert worker.state is WorkerState.ERROR
        assert db.stats().completed == 2
        assert db.stats().processing == 0
        assert report.successful == 2
