"""Persistence: claiming, completion, retry budgets and crash recovery."""

from __future__ import annotations

import sqlite3
import time

import pytest

from database import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SKIPPED,
    Database,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "progress.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_records_new_items(db):
    assert db.record_discovered(["p/A", "p/B"]) == 2
    assert db.stats().pending == 2


def test_rediscovery_never_duplicates(db):
    db.record_discovered(["p/A", "p/B"])
    assert db.record_discovered(["p/A", "p/B", "p/C"]) == 1
    assert db.stats().total == 3


def test_identifier_uniqueness_is_enforced_by_the_schema(db):
    db.record_discovered(["p/A"])
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO items (content_identifier, first_seen) VALUES ('p/A', 0)"
            )


def test_accepts_objects_tuples_and_strings(db):
    class Thing:
        identifier = "p/OBJ"
        url = "https://example.test/p/OBJ/"
        media_type = "post"

    assert db.record_discovered([Thing(), ("p/TUP", "u", "reel"), "p/STR"]) == 3
    assert db.get_item("p/OBJ").content_url == "https://example.test/p/OBJ/"
    assert db.get_item("p/TUP").media_type == "reel"


def test_rediscovery_does_not_reopen_finished_work(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    db.mark_completed(item.id)

    db.record_discovered(["p/A"])

    assert db.get_item("p/A").status == STATUS_COMPLETED
    assert db.identifiers_to_skip(["p/A", "p/B"]) == {"p/A"}


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------
def test_claim_moves_items_to_processing(db):
    db.record_discovered([f"p/{i}" for i in range(5)])
    claimed = db.claim_batch(3)

    assert len(claimed) == 3
    assert all(item.status == STATUS_PROCESSING for item in claimed)
    assert db.stats().pending == 2


def test_claim_never_hands_out_the_same_item_twice(db):
    db.record_discovered([f"p/{i}" for i in range(6)])
    first = {item.id for item in db.claim_batch(3)}
    second = {item.id for item in db.claim_batch(3)}

    assert first.isdisjoint(second)
    assert db.stats().pending == 0


def test_claim_respects_the_attempt_ceiling(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    for _ in range(3):
        db.release_for_retry(item.id, "boom", max_attempts=99)

    assert db.get_item("p/A").attempts == 3
    assert db.claim_batch(5, max_attempts=3) == []
    assert len(db.claim_batch(5, max_attempts=4)) == 1


def test_claim_on_empty_queue_returns_nothing(db):
    assert db.claim_batch(10) == []
    assert db.claim_batch(0) == []


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
def test_completion_is_recorded_with_a_timestamp(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    db.mark_completed(item.id)

    stored = db.get_item("p/A")
    assert stored.status == STATUS_COMPLETED
    assert stored.completed_at is not None
    assert stored.attempts == 1
    assert stored.error is None


def test_failure_records_the_reason_and_code(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    db.mark_failed(item.id, "element not found", error_code="element_not_found")

    stored = db.get_item("p/A")
    assert stored.status == STATUS_FAILED
    assert stored.error == "element not found"
    assert stored.error_code == "element_not_found"


def test_skip_is_terminal_but_not_a_failure(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    db.mark_skipped(item.id, "already not liked")

    stored = db.get_item("p/A")
    assert stored.status == STATUS_SKIPPED
    assert stored.completed_at is not None
    assert db.stats().failed == 0


def test_retry_returns_the_item_and_counts_the_attempt(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]

    assert db.release_for_retry(item.id, "timeout", max_attempts=3) == STATUS_PENDING
    assert db.get_item("p/A").attempts == 1
    assert db.stats().pending == 1


def test_retries_stop_at_the_budget(db):
    """Repeated failure must end in 'failed', never an endless retry loop."""
    db.record_discovered(["p/A"])
    statuses = []
    for _ in range(4):
        item = db.claim_batch(1)
        if not item:
            break
        statuses.append(db.release_for_retry(item[0].id, "boom", max_attempts=3))

    assert statuses == [STATUS_PENDING, STATUS_PENDING, STATUS_PENDING, STATUS_FAILED]
    assert db.stats().failed == 1
    assert db.stats().pending == 0


def test_failed_items_can_be_requeued(db):
    db.record_discovered(["p/A", "p/B"])
    for item in db.claim_batch(2):
        db.mark_failed(item.id, "boom")

    assert db.reset_failed_to_pending() == 2
    assert db.stats().pending == 2
    assert db.get_item("p/A").attempts == 0


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------
def test_interrupted_items_return_to_the_queue(db):
    db.record_discovered([f"p/{i}" for i in range(4)])
    db.claim_batch(4)

    assert db.release_all_processing(reason="crash") == 4
    assert db.stats().pending == 4
    assert db.stats().processing == 0


def test_stale_claims_are_recovered_by_age(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE items SET last_attempt = ? WHERE id = ?",
            (time.time() - 5000, item.id),
        )

    assert db.recover_stale_processing(older_than=900) == 1
    assert db.stats().pending == 1


def test_abandoned_claims_are_recovered_immediately_on_restart(db):
    """A fast restart after a crash must not strand the in-flight batch."""
    first_session = db.start_session(dry_run=False)
    db.record_discovered(["p/A", "p/B"])
    db.claim_batch(2, session_id=first_session)

    second_session = db.start_session(dry_run=False)
    recovered = db.recover_abandoned(second_session, older_than=900)

    assert recovered == 2
    assert db.stats().pending == 2


def test_recovery_leaves_the_current_session_alone(db):
    session = db.start_session(dry_run=False)
    db.record_discovered(["p/A"])
    db.claim_batch(1, session_id=session)

    assert db.recover_abandoned(session, older_than=900) == 0
    assert db.stats().processing == 1


def test_progress_survives_reopening_the_database(tmp_path):
    path = tmp_path / "progress.db"
    first = Database(path)
    first.record_discovered([f"p/{i}" for i in range(10)])
    for item in first.claim_batch(4):
        first.mark_completed(item.id)
    first.close()

    second = Database(path)
    try:
        stats = second.stats()
        assert stats.completed == 4
        assert stats.pending == 6
    finally:
        second.close()


def test_transaction_rolls_back_on_error(db):
    db.record_discovered(["p/A"])
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("UPDATE items SET status = 'completed'")
            raise RuntimeError("boom")

    assert db.get_item("p/A").status == STATUS_PENDING


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "progress.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="newer version"):
        Database(path)


# ---------------------------------------------------------------------------
# Dry-run paging and reporting
# ---------------------------------------------------------------------------
def test_pending_after_reads_without_claiming(db):
    db.record_discovered([f"p/{i}" for i in range(5)])

    page = db.pending_after(0, 2)

    assert len(page) == 2
    assert db.stats().pending == 5, "reading must not claim"
    assert [item.content_identifier for item in db.pending_after(page[-1].id, 2)] == [
        "p/2",
        "p/3",
    ]


def test_average_completion_needs_two_samples(db):
    db.record_discovered(["p/A", "p/B"])
    items = db.claim_batch(2)
    db.mark_completed(items[0].id)
    assert db.average_completion_seconds() is None

    time.sleep(0.02)
    db.mark_completed(items[1].id)
    assert db.average_completion_seconds() > 0


def test_session_counters_track_outcomes(db):
    session = db.start_session(dry_run=False)
    db.record_discovered(["p/A", "p/B", "p/C"], session_id=session)
    items = db.claim_batch(3, session_id=session)
    db.mark_completed(items[0].id, session_id=session)
    db.mark_failed(items[1].id, "boom", session_id=session)
    db.mark_skipped(items[2].id, "nothing to do", session_id=session)
    db.end_session(session, stop_reason="done")

    stored = db.get_session(session)
    assert (stored["discovered"], stored["completed"], stored["failed"], stored["skipped"]) == (3, 1, 1, 1)
    assert stored["ended_at"] is not None
    assert db.last_session()["id"] == session


def test_events_are_recorded_for_the_audit_trail(db):
    session = db.start_session(dry_run=True)
    db.log_event("rate_limited", "backing off", session_id=session)
    db.log_event("pause", {"seconds": 30}, session_id=session)

    events = db.recent_events(session_id=session)
    assert {event["kind"] for event in events} == {"rate_limited", "pause"}


def test_long_errors_are_trimmed(db):
    db.record_discovered(["p/A"])
    item = db.claim_batch(1)[0]
    db.mark_failed(item.id, "x" * 5000)

    assert len(db.get_item("p/A").error) <= 1000
