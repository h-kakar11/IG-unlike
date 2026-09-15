"""In-memory stand-ins for the Instagram layer.

These let the worker's control flow — batching, retries, backoff, stopping —
be tested exhaustively and instantly, with no browser involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from config import Config
from instagram.likes import LikedItem, Outcome, UnlikeResult


@dataclass
class FakeNavigator:
    """Records navigation calls; never touches a network."""

    on_likes: bool = True
    likes_calls: int = 0

    def on_likes_page(self, timeout: float | None = None) -> bool:
        return self.on_likes

    def navigate_to_likes(self) -> str:
        self.likes_calls += 1
        self.on_likes = True
        return "https://example.test/your_activity/interactions/likes/"

    def has_liked_content(self) -> bool:
        return True

    def settle(self, **_: Any) -> None:
        pass

    def raise_for_page_state(self) -> None:
        pass


@dataclass
class FakeScanner:
    """Serves pre-canned pages of liked items."""

    pages: list[list[LikedItem]] = field(default_factory=list)
    selectors: Any = None
    calls: int = 0

    def discover(
        self,
        *,
        target: int | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[LikedItem]:
        self.calls += 1
        page = self.pages.pop(0) if self.pages else []
        if on_progress:
            on_progress(len(page))
        return page

    def scan_visible(self) -> list[LikedItem]:
        return self.pages[0] if self.pages else []


class FakeStrategy:
    """Returns scripted outcomes, one per call, and records what it saw."""

    name = "fake"

    def __init__(self, outcomes: Iterable[Any] | None = None, default: Any = None):
        self._outcomes = list(outcomes or [])
        self._default = default
        self.seen: list[str] = []

    def unlike(self, item: LikedItem) -> UnlikeResult:
        self.seen.append(item.identifier)
        outcome = self._outcomes.pop(0) if self._outcomes else self._default
        if outcome is None:
            return UnlikeResult(item.identifier, Outcome.COMPLETED)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, UnlikeResult):
            return outcome
        if isinstance(outcome, Outcome):
            return UnlikeResult(item.identifier, outcome, "scripted", "scripted")
        raise TypeError(f"unusable scripted outcome: {outcome!r}")

    def unlike_many(self, items: Sequence[LikedItem]) -> list[UnlikeResult]:
        return [self.unlike(item) for item in items]


def make_items(count: int, prefix: str = "p/ITEM") -> list[LikedItem]:
    return [
        LikedItem(f"{prefix}{index:03d}", f"https://example.test/p/X{index}/", "post")
        for index in range(count)
    ]


def fast_config(tmp_path, **overrides) -> Config:
    """A Config with all real waiting removed, for unit tests."""
    values: dict[str, Any] = {
        "db_path": tmp_path / "progress.db",
        "log_path": tmp_path / "logs" / "test.log",
        "browser_profile_dir": tmp_path / "profile",
        "min_delay": 0.0,
        "max_delay": 0.0,
        "pause_after_batch": 0.0,
        "backoff_initial": 0.001,
        "backoff_max": 0.002,
        "scroll_pause": 0.0,
        "dry_run": False,
    }
    values.update(overrides)
    return Config(**values)
