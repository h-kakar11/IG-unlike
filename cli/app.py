"""The application: wiring, menu actions, and the safety gates around them.

Every destructive path goes through :meth:`Application.start_unliking`, which
cannot proceed unless (a) live mode was explicitly requested and (b) the user
typed "yes" in full. Opening the program, scanning, resuming a *dry* job and
viewing progress can never remove a like.
"""

from __future__ import annotations

from typing import Any, Callable

from cli import display, prompts
from cli.runner import run_with_controls
from config import Config
from core.errors import UnlikerError
from core.logging_setup import get_logger
from core.worker import RunReport, Worker
from database import STATUS_FAILED, Database
from instagram.browser import BrowserSession, clear_profile
from instagram.likes import LikesScanner
from instagram.navigation import Navigator
from instagram.selectors import SelectorRegistry

log = get_logger("app")

MENU_OPTIONS = [
    "Scan likes (read-only)",
    "Start unliking",
    "Resume previous job",
    "View progress",
    "Settings",
    "Exit",
]


class Application:
    """Owns the long-lived objects and the menu loop."""

    def __init__(
        self,
        config: Config,
        *,
        input_fn: Callable[[str], str] | None = None,
        assume_yes: bool = False,
    ):
        self.config = config
        #: ``None`` means "resolve builtins.input at call time", which keeps the
        #: confirmation prompts substitutable in tests.
        self.input_fn = input_fn
        self.assume_yes = assume_yes
        self.selectors = SelectorRegistry.load(config.selectors_file)
        self.db = Database(config.db_path)
        self._session: BrowserSession | None = None
        self._navigator: Navigator | None = None
        self._scanner: LikesScanner | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._session is not None:
            self._session.stop()
            self._session = None
            self._navigator = None
            self._scanner = None
        self.db.close()

    def __enter__(self) -> "Application":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_browser(self) -> tuple[Navigator, LikesScanner]:
        """Open the browser and make sure a human has logged in."""
        if self._navigator is not None and self._session and self._session.is_alive():
            return self._navigator, self._scanner  # type: ignore[return-value]

        print("Opening browser...")
        self._session = BrowserSession(self.config).start()
        self._navigator = Navigator(self._session, self.config, self.selectors)
        self._scanner = LikesScanner(self._navigator, self.config, self.selectors)

        self._navigator.ensure_authenticated(
            prompt=lambda message: prompts.wait_for_enter(message, input_fn=self.input_fn)
        )
        print("Logged in.")
        return self._navigator, self._scanner

    def _build_worker(self) -> Worker:
        navigator, scanner = self._ensure_browser()
        return Worker(
            navigator=navigator,
            scanner=scanner,
            database=self.db,
            config=self.config,
        )

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------
    def scan(self, *, target: int | None = None, record: bool = True) -> RunReport:
        """Phase 3: read-only scan. Cannot remove anything."""
        display.banner()
        print("Scanning liked content...")
        navigator, scanner = self._ensure_browser()
        navigator.navigate_to_likes()

        if not navigator.has_liked_content():
            print("No liked content was found on this account.")
            print("Dry run complete.\nNo likes were removed.")
            return RunReport(session_id=-1, dry_run=True, stop_reason="nothing to scan")

        session_id = self.db.start_session(dry_run=True)
        try:
            items = scanner.discover(
                target=target,
                on_progress=lambda count: print(f"Currently detected: {count}", end="\r"),
            )
            print(" " * 40, end="\r")
            if record and items:
                new = self.db.record_discovered(items, session_id=session_id)
                log.info("Scan recorded %d new item(s)", new)
            report = scanner.dry_run_report(items)
            print(display.dry_run_result(report["detected"], report))
            return RunReport(
                session_id=session_id,
                dry_run=True,
                discovered=len(items),
                stop_reason="Dry run complete. No likes were removed.",
            )
        finally:
            self.db.end_session(session_id, stop_reason="scan complete")

    def start_unliking(self, *, limit: int | None = None) -> RunReport | None:
        """The only destructive entry point, and it is gated twice."""
        if self.config.dry_run:
            print()
            print(display.mode_line(True))
            print(
                "Live mode is off, so this will rehearse the run without removing\n"
                "anything. Enable it with --live (or in Settings) to remove likes."
            )
            return self._run(limit=limit)

        stats = self.db.stats()
        if not self.assume_yes:
            if not prompts.confirm_destructive(
                input_fn=self.input_fn, pending=stats.pending or None
            ):
                return None
        else:
            print(display.destructive_warning())
            print("\nProceeding: confirmation was given on the command line.")

        return self._run(limit=limit)

    def resume(self, *, limit: int | None = None) -> RunReport | None:
        """Phase 14: pick up exactly where the last run stopped."""
        stats = self.db.stats()
        if stats.total == 0:
            print("No previous session was found. Run a scan first.")
            return None

        if not self.assume_yes:
            if not prompts.confirm_resume(
                completed=stats.completed,
                failed=stats.failed,
                pending=stats.remaining,
                input_fn=self.input_fn,
            ):
                print("Not resuming.")
                return None

        if stats.remaining == 0:
            print("Nothing is pending. Scanning for more liked content...")

        # Resuming a live run is still destructive, so it is gated identically.
        if not self.config.dry_run and not self.assume_yes:
            if not prompts.confirm_destructive(
                input_fn=self.input_fn, pending=stats.remaining or None
            ):
                return None
        return self._run(limit=limit)

    def _run(self, *, limit: int | None) -> RunReport:
        worker = self._build_worker()
        report = run_with_controls(worker, limit=limit)
        print()
        print(report.summary())
        if report.failed:
            print(
                f"{report.failed:,} item(s) failed. They are recorded as 'failed' and "
                "can be retried from the progress screen."
            )
        return report

    def show_progress(self) -> None:
        """Phase 15 statistics, readable while a run is in progress."""
        stats = self.db.stats()
        average = self.db.average_completion_seconds()
        snapshot = _snapshot_from(stats, average)

        display.banner()
        print(
            display.statistics_block(
                snapshot,
                discovered=stats.total,
                completed=stats.completed,
                failed=stats.failed,
                skipped=stats.skipped,
            )
        )

        sessions = self.db.recent_sessions(5)
        if sessions:
            print("\nRecent sessions:")
            print(
                display.table(
                    [
                        [
                            session["id"],
                            "dry" if session["dry_run"] else "live",
                            session["completed"],
                            session["failed"],
                            (session["stop_reason"] or "")[:44],
                        ]
                        for session in sessions
                    ],
                    ["#", "mode", "done", "failed", "outcome"],
                )
            )

        failed = self.db.items_by_status(STATUS_FAILED, limit=5)
        if failed:
            print("\nMost recent failures:")
            print(
                display.table(
                    [
                        [item.content_identifier, item.attempts, (item.error or "")[:50]]
                        for item in failed
                    ],
                    ["item", "tries", "error"],
                )
            )
            if prompts.confirm_yes_no(
                f"Return {self.db.stats().failed:,} failed item(s) to the queue?",
                input_fn=self.input_fn,
                default=False,
            ):
                count = self.db.reset_failed_to_pending()
                print(f"{count:,} item(s) moved back to pending.")

    def show_settings(self) -> None:
        display.banner("Settings")
        print(display.mode_line(self.config.dry_run))
        print()
        print(display.settings_block(self.config.to_dict(), sources=self.config.sources))
        print()
        print(
            "Settings are read from config.json, .env and IGU_* environment\n"
            "variables — see docs/CONFIGURATION.md. Nothing here is a credential."
        )
        print()
        if prompts.confirm_yes_no(
            "Write the current selectors to selectors.json for editing?",
            input_fn=self.input_fn,
            default=False,
        ):
            self.selectors.dump(self.config.selectors_file)
            print(f"Wrote {self.config.selectors_file}")

        if prompts.confirm_yes_no(
            "Forget the saved browser profile (this logs you out of Instagram)?",
            input_fn=self.input_fn,
            default=False,
        ):
            if clear_profile(self.config.browser_profile_dir):
                print("Browser profile deleted. You will log in again next time.")
            else:
                print("No saved profile to delete.")

    # ------------------------------------------------------------------
    # Menu loop
    # ------------------------------------------------------------------
    def main_menu(self) -> int:
        while True:
            print()
            print(display.mode_line(self.config.dry_run))
            choice = prompts.choose(MENU_OPTIONS, input_fn=self.input_fn)
            if choice is None:
                continue
            try:
                if choice == 0:
                    self.scan()
                elif choice == 1:
                    self.start_unliking()
                elif choice == 2:
                    self.resume()
                elif choice == 3:
                    self.show_progress()
                elif choice == 4:
                    self.show_settings()
                elif choice == 5:
                    print("Goodbye.")
                    return 0
            except UnlikerError as exc:
                print(f"\n{type(exc).__name__}: {exc}\n")
                log.error("%s: %s", type(exc).__name__, exc)
                if exc.fatal:
                    return 1
            except KeyboardInterrupt:
                print("\nInterrupted. Progress is saved.")


def _snapshot_from(stats: Any, average: float | None):
    from core.progress import ProgressSnapshot

    return ProgressSnapshot(
        processed=stats.finished,
        successful=stats.completed,
        failed=stats.failed,
        skipped=stats.skipped,
        total_recorded=stats.total,
        remaining=stats.remaining,
        average_seconds=average,
        items_per_minute=(60.0 / average) if average else None,
        eta_seconds=(stats.remaining * average) if average else None,
        status="idle",
    )
