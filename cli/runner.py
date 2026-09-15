"""Running a worker while the terminal stays responsive.

Threading note, and it is not an arbitrary choice: **Playwright's synchronous
API is bound to the thread that created the browser**, so the worker — which
drives Playwright — runs on the main thread. The parts that can live on
background threads are the ones that only read and print: a display thread
redraws the status block, and a stdin thread turns ``p`` / ``r`` / ``s`` into
calls on the worker's control surface (all of which are Event-based and safe
to call from anywhere).

Ctrl-C is wired to the same graceful stop as ``s``: the current item finishes,
in-flight rows go back to the queue, and the database is left consistent.
"""

from __future__ import annotations

import signal
import sys
import threading
from typing import Any

from cli import display
from core.logging_setup import get_logger
from core.progress import ProgressSnapshot
from core.worker import RunReport, Worker

log = get_logger("cli")

HELP_LINE = "Commands:  p = pause   r = resume   s = stop (graceful)   Ctrl-C = stop"


class _ConsoleControl(threading.Thread):
    """Reads console commands and redraws the status block.

    Daemon, so a blocked ``readline`` can never keep the process alive, and
    read-only with respect to the worker apart from the three control calls.
    """

    def __init__(self, worker: Worker, *, refresh: float, show_controls: bool):
        super().__init__(name="unliker-console", daemon=True)
        self.worker = worker
        self.refresh = refresh
        self.show_controls = show_controls
        self.snapshot: ProgressSnapshot | None = None
        self.events: list[str] = []
        self._finished = threading.Event()
        self._interactive = bool(sys.stdin) and sys.stdin.isatty()

    # -- called from the worker (main) thread ---------------------------
    def on_progress(self, snapshot: ProgressSnapshot) -> None:
        self.snapshot = snapshot

    def on_event(self, _kind: str, message: str) -> None:
        self.events.append(message)

    def finish(self) -> None:
        self._finished.set()

    # -- the thread itself ----------------------------------------------
    def run(self) -> None:
        if self._interactive:
            threading.Thread(target=self._read_commands, daemon=True).start()
        while not self._finished.wait(self.refresh):
            self.draw()

    def _read_commands(self) -> None:
        while not self._finished.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:  # noqa: BLE001 - stdin closed under us
                return
            if not line:
                return
            self._dispatch(line.strip().lower())

    def _dispatch(self, command: str) -> None:
        if command in ("p", "pause"):
            self.worker.request_pause()
        elif command in ("r", "resume"):
            self.worker.request_resume()
        elif command in ("s", "stop", "q", "quit"):
            print("Stopping gracefully — finishing the current item...")
            self.worker.request_stop("stopped from the console")

    def draw(self, *, final: bool = False) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        display.clear_screen()
        print(
            display.status_block(
                snapshot,
                dry_run=bool(self.worker.config.dry_run),
                status=self.worker.state.value,
            )
        )
        if self.events:
            print()
            for message in self.events[-3:]:
                print(f"  · {message}")
        if self.show_controls and not final and self._interactive:
            print()
            print(HELP_LINE)


def run_with_controls(
    worker: Worker,
    *,
    limit: int | None = None,
    refresh: float = 1.0,
    show_controls: bool = True,
) -> RunReport:
    """Run ``worker`` on this thread, with a live display and console controls."""
    console = _ConsoleControl(worker, refresh=refresh, show_controls=show_controls)
    worker._on_progress = console.on_progress
    worker._on_event = console.on_event

    previous_handler: Any = None
    installed = False

    def handle_sigint(_signum: int, _frame: Any) -> None:
        print("\nStopping gracefully — finishing the current item...")
        worker.request_stop("interrupted with Ctrl-C")

    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handle_sigint)
        installed = True
    except ValueError:  # pragma: no cover - not running on the main thread
        log.debug("Could not install the SIGINT handler on this thread")

    console.start()
    try:
        return worker.run(limit=limit)
    finally:
        console.finish()
        console.join(timeout=2)
        console.draw(final=True)
        if installed:
            try:
                signal.signal(signal.SIGINT, previous_handler)
            except ValueError:  # pragma: no cover
                pass
