"""SQLite persistence.

This is what makes the tool survive a power cut at item 25,384 of 250,000.
Design rules:

* Every state change is a single committed transaction. There is no in-memory
  progress that matters; the database is the truth.
* ``content_identifier`` is UNIQUE, so rediscovering the same post after a
  scroll, a restart or a re-scan can never create duplicate work.
* Claiming work (``pending`` -> ``processing``) is itself a transaction, so a
  crash mid-batch leaves a recoverable marker rather than silent data loss.
* WAL journalling, so a reader (the stats screen) never blocks the worker and
  an abrupt kill cannot tear a write.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from core.logging_setup import get_logger

log = get_logger("database")

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

ALL_STATUSES = (
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
)

#: Statuses that mean "this item will never be worked on again".
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_SKIPPED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    content_identifier TEXT    NOT NULL UNIQUE,
    content_url        TEXT,
    media_type         TEXT,
    status             TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','processing','completed','failed','skipped')),
    attempts           INTEGER NOT NULL DEFAULT 0,
    first_seen         REAL    NOT NULL,
    last_attempt       REAL,
    completed_at       REAL,
    error              TEXT,
    error_code         TEXT,
    session_id         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_items_status  ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_claim   ON items(status, id);
CREATE INDEX IF NOT EXISTS idx_items_session ON items(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    dry_run       INTEGER NOT NULL DEFAULT 1,
    discovered    INTEGER NOT NULL DEFAULT 0,
    completed     INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    stop_reason   TEXT,
    config_digest TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    created_at REAL NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Item:
    """One liked post as recorded in the database."""

    id: int
    content_identifier: str
    content_url: str | None
    media_type: str | None
    status: str
    attempts: int
    first_seen: float
    last_attempt: float | None
    completed_at: float | None
    error: str | None
    error_code: str | None
    session_id: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Item":
        return cls(**{k: row[k] for k in row.keys() if k in cls.__annotations__})


@dataclass(frozen=True)
class Stats:
    """Aggregate counts used by the progress screen."""

    total: int = 0
    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def remaining(self) -> int:
        return self.pending + self.processing

    @property
    def finished(self) -> int:
        return self.completed + self.failed + self.skipped


class Database:
    """Thread-safe wrapper around the progress database.

    A single connection is shared behind a lock rather than one connection per
    thread: the workload is a handful of small writes per minute, and one
    connection makes transaction boundaries obvious.
    """

    def __init__(self, path: str | Path, *, timeout: float = 30.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=timeout, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers non-blocking and makes an abrupt kill recoverable.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()
        log.debug("Database ready at %s", self.path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        with self.transaction() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} was written by a newer version of this tool "
                    f"(schema {row['value']} > {SCHEMA_VERSION}). Refusing to "
                    "touch it."
                )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically: commit on success, roll back on any error."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def record_discovered(
        self,
        items: Iterable[Any],
        *,
        session_id: int | None = None,
    ) -> int:
        """Insert newly discovered items, ignoring ones already known.

        Accepts anything with ``identifier`` / ``url`` / ``media_type``
        attributes, or plain ``(identifier, url, media_type)`` tuples, or bare
        identifier strings. Returns the number of *new* rows.
        """
        rows: list[tuple[str, str | None, str | None, float]] = []
        now = time.time()
        for item in items:
            identifier, url, media_type = _unpack(item)
            if not identifier:
                continue
            rows.append((identifier, url, media_type, now))
        if not rows:
            return 0

        with self.transaction() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO items (content_identifier, content_url, media_type,
                                   status, attempts, first_seen)
                VALUES (?, ?, ?, 'pending', 0, ?)
                ON CONFLICT(content_identifier) DO NOTHING
                """,
                rows,
            )
            inserted = conn.total_changes - before
            if session_id is not None and inserted:
                conn.execute(
                    "UPDATE sessions SET discovered = discovered + ? WHERE id = ?",
                    (inserted, session_id),
                )
        if inserted:
            log.info("Recorded %d newly discovered item(s)", inserted)
        return inserted

    def known_identifiers(self, identifiers: Sequence[str]) -> set[str]:
        """Return the subset of ``identifiers`` already present in the database."""
        found: set[str] = set()
        if not identifiers:
            return found
        with self._lock:
            for chunk in _chunks(list(identifiers), 500):
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT content_identifier FROM items "
                    f"WHERE content_identifier IN ({placeholders})",
                    chunk,
                ).fetchall()
                found.update(row["content_identifier"] for row in rows)
        return found

    def identifiers_to_skip(self, identifiers: Sequence[str]) -> set[str]:
        """Identifiers already finished (completed/failed/skipped).

        Used by the worker so a re-scan does not re-open settled work.
        """
        found: set[str] = set()
        if not identifiers:
            return found
        placeholders_status = ",".join("?" * len(TERMINAL_STATUSES))
        with self._lock:
            for chunk in _chunks(list(identifiers), 500):
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT content_identifier FROM items "
                    f"WHERE content_identifier IN ({placeholders}) "
                    f"AND status IN ({placeholders_status})",
                    [*chunk, *TERMINAL_STATUSES],
                ).fetchall()
                found.update(row["content_identifier"] for row in rows)
        return found

    # ------------------------------------------------------------------
    # Work claiming and completion
    # ------------------------------------------------------------------
    def claim_batch(
        self,
        limit: int,
        *,
        session_id: int | None = None,
        max_attempts: int | None = None,
    ) -> list[Item]:
        """Atomically move up to ``limit`` pending items into ``processing``.

        The select-then-update pair runs inside one transaction so two workers
        (or a worker and a crashed predecessor) can never claim the same row.
        """
        if limit < 1:
            return []
        with self.transaction() as conn:
            params: list[Any] = [STATUS_PENDING]
            attempt_clause = ""
            if max_attempts is not None:
                attempt_clause = " AND attempts < ?"
                params.append(max_attempts)
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM items WHERE status = ?{attempt_clause} "
                f"ORDER BY id LIMIT ?",
                params,
            ).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" * len(ids))
            now = time.time()
            conn.execute(
                f"UPDATE items SET status = ?, last_attempt = ?, session_id = ? "
                f"WHERE id IN ({placeholders})",
                [STATUS_PROCESSING, now, session_id, *ids],
            )
            claimed = conn.execute(
                f"SELECT * FROM items WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
        log.debug("Claimed %d item(s) for processing", len(claimed))
        return [Item.from_row(row) for row in claimed]

    def mark_completed(self, item_id: int, *, session_id: int | None = None) -> None:
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items
                   SET status = ?, completed_at = ?, last_attempt = ?,
                       attempts = attempts + 1, error = NULL, error_code = NULL
                 WHERE id = ?
                """,
                (STATUS_COMPLETED, now, now, item_id),
            )
            if session_id is not None:
                conn.execute(
                    "UPDATE sessions SET completed = completed + 1 WHERE id = ?",
                    (session_id,),
                )

    def mark_failed(
        self,
        item_id: int,
        error: str,
        *,
        error_code: str = "error",
        session_id: int | None = None,
    ) -> None:
        """Terminal failure: the item will not be retried."""
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items
                   SET status = ?, last_attempt = ?, attempts = attempts + 1,
                       error = ?, error_code = ?
                 WHERE id = ?
                """,
                (STATUS_FAILED, now, _trim(error), error_code, item_id),
            )
            if session_id is not None:
                conn.execute(
                    "UPDATE sessions SET failed = failed + 1 WHERE id = ?",
                    (session_id,),
                )

    def mark_skipped(
        self,
        item_id: int,
        reason: str,
        *,
        session_id: int | None = None,
    ) -> None:
        """Nothing to do for this item (e.g. it is already not liked)."""
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE items
                   SET status = ?, last_attempt = ?, completed_at = ?,
                       attempts = attempts + 1, error = ?, error_code = 'skipped'
                 WHERE id = ?
                """,
                (STATUS_SKIPPED, now, now, _trim(reason), item_id),
            )
            if session_id is not None:
                conn.execute(
                    "UPDATE sessions SET skipped = skipped + 1 WHERE id = ?",
                    (session_id,),
                )

    def release_for_retry(
        self,
        item_id: int,
        error: str,
        *,
        error_code: str = "error",
        max_attempts: int | None = None,
        session_id: int | None = None,
        count_attempt: bool = True,
    ) -> str:
        """Return an item to ``pending`` after a retryable failure.

        If the attempt budget is exhausted the item becomes ``failed`` instead,
        which is what stops the tool retrying anything forever. Returns the
        status the item ended up in.

        ``count_attempt=False`` puts the item back without spending any of its
        budget — used when the cause was nothing to do with the item, such as
        the whole session being throttled.
        """
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT attempts FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + (1 if count_attempt else 0)
            exhausted = max_attempts is not None and attempts > max_attempts
            status = STATUS_FAILED if exhausted else STATUS_PENDING
            conn.execute(
                """
                UPDATE items
                   SET status = ?, attempts = ?, last_attempt = ?,
                       error = ?, error_code = ?
                 WHERE id = ?
                """,
                (status, attempts, now, _trim(error), error_code, item_id),
            )
            if exhausted and session_id is not None:
                conn.execute(
                    "UPDATE sessions SET failed = failed + 1 WHERE id = ?",
                    (session_id,),
                )
        return status

    def pending_after(self, after_id: int, limit: int) -> list[Item]:
        """Read a page of pending items *without* claiming them.

        This is how the dry run walks the queue: it reads, it never writes, so
        a rehearsal leaves the database byte-for-byte as it found it.
        """
        if limit < 1:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE status = ? AND id > ? ORDER BY id LIMIT ?",
                (STATUS_PENDING, after_id, limit),
            ).fetchall()
        return [Item.from_row(row) for row in rows]

    def release_all_processing(self, *, reason: str = "interrupted") -> int:
        """Return every ``processing`` row to ``pending``.

        Called on graceful shutdown and on startup recovery: an item that was
        in flight when the process died is re-tried, never lost.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE items SET status = ?, error = ?, error_code = 'interrupted' "
                "WHERE status = ?",
                (STATUS_PENDING, _trim(reason), STATUS_PROCESSING),
            )
            released = cursor.rowcount or 0
        if released:
            log.warning("Released %d in-flight item(s) back to pending (%s)", released, reason)
        return released

    def recover_stale_processing(self, older_than: float) -> int:
        """Release ``processing`` rows untouched for ``older_than`` seconds."""
        cutoff = time.time() - older_than
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE items SET status = ?, error = 'recovered from stale processing',
                       error_code = 'stale'
                 WHERE status = ? AND (last_attempt IS NULL OR last_attempt < ?)
                """,
                (STATUS_PENDING, STATUS_PROCESSING, cutoff),
            )
            released = cursor.rowcount or 0
        if released:
            log.warning("Recovered %d stale in-flight item(s)", released)
        return released

    def recover_abandoned(self, current_session_id: int, older_than: float) -> int:
        """Release in-flight rows left behind by an earlier run.

        A row is abandoned if it was claimed by a *different* session, or if it
        has simply sat in ``processing`` for too long. The session check is
        what makes recovery immediate: restarting ten seconds after a crash
        should not leave a batch stranded until a timeout expires.

        This assumes one worker per database — which is also enforced in
        practice, since a single browser profile can only be open once.
        """
        cutoff = time.time() - older_than
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE items
                   SET status = ?, error = 'recovered from an interrupted run',
                       error_code = 'recovered'
                 WHERE status = ?
                   AND (session_id IS NULL
                        OR session_id != ?
                        OR last_attempt IS NULL
                        OR last_attempt < ?)
                """,
                (STATUS_PENDING, STATUS_PROCESSING, current_session_id, cutoff),
            )
            released = cursor.rowcount or 0
        if released:
            log.warning(
                "Recovered %d item(s) left in flight by an interrupted run", released
            )
        return released

    def reset_failed_to_pending(self, *, reset_attempts: bool = True) -> int:
        """Give every failed item another chance (used by the CLI's retry action)."""
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE items SET status = ?, error = NULL, error_code = NULL"
                f"{', attempts = 0' if reset_attempts else ''} WHERE status = ?",
                (STATUS_PENDING, STATUS_FAILED),
            )
            return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Sessions and events
    # ------------------------------------------------------------------
    def start_session(self, *, dry_run: bool, config_digest: str = "") -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO sessions (started_at, dry_run, config_digest) VALUES (?, ?, ?)",
                (time.time(), 1 if dry_run else 0, config_digest),
            )
            session_id = int(cursor.lastrowid)
        log.info("Started session %d (dry_run=%s)", session_id, dry_run)
        return session_id

    def end_session(self, session_id: int, *, stop_reason: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, stop_reason = ? WHERE id = ?",
                (time.time(), stop_reason, session_id),
            )
        log.info("Ended session %d (%s)", session_id, stop_reason or "complete")

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def last_session(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def log_event(
        self,
        kind: str,
        detail: Any = None,
        *,
        session_id: int | None = None,
    ) -> None:
        """Record a durable audit event (rate limiting, pauses, stops...)."""
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events (session_id, created_at, kind, detail) "
                "VALUES (?, ?, ?, ?)",
                (session_id, time.time(), kind, _trim(detail) if detail else None),
            )

    def recent_events(self, limit: int = 20, *, session_id: int | None = None):
        query = "SELECT * FROM events"
        params: list[Any] = []
        if session_id is not None:
            query += " WHERE session_id = ?"
            params.append(session_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def stats(self) -> Stats:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
            ).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        return Stats(
            total=sum(counts.values()),
            pending=counts.get(STATUS_PENDING, 0),
            processing=counts.get(STATUS_PROCESSING, 0),
            completed=counts.get(STATUS_COMPLETED, 0),
            failed=counts.get(STATUS_FAILED, 0),
            skipped=counts.get(STATUS_SKIPPED, 0),
        )

    def get_item(self, identifier: str) -> Item | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items WHERE content_identifier = ?", (identifier,)
            ).fetchone()
        return Item.from_row(row) if row else None

    def items_by_status(self, status: str, limit: int = 100) -> list[Item]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE status = ? ORDER BY id LIMIT ?",
                (status, limit),
            ).fetchall()
        return [Item.from_row(row) for row in rows]

    def average_completion_seconds(self, sample: int = 200) -> float | None:
        """Mean gap between consecutive completions, over the last ``sample``.

        Used for the ETA: observed throughput, not a theoretical rate.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT completed_at FROM items WHERE status = ? AND completed_at "
                "IS NOT NULL ORDER BY completed_at DESC LIMIT ?",
                (STATUS_COMPLETED, sample),
            ).fetchall()
        stamps = sorted(row["completed_at"] for row in rows)
        if len(stamps) < 2:
            return None
        span = stamps[-1] - stamps[0]
        return span / (len(stamps) - 1) if span > 0 else None

    def has_unfinished_work(self) -> bool:
        stats = self.stats()
        return stats.remaining > 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _unpack(item: Any) -> tuple[str, str | None, str | None]:
    if isinstance(item, str):
        return item, None, None
    if isinstance(item, (tuple, list)):
        padded = list(item) + [None, None]
        return str(padded[0]), padded[1], padded[2]
    return (
        str(getattr(item, "identifier", "") or ""),
        getattr(item, "url", None),
        getattr(item, "media_type", None),
    )


def _trim(text: str | None, limit: int = 1000) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _chunks(values: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
