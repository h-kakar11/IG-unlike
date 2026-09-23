"""Reading liked content, and — only when explicitly enabled — unliking it.

The scanner and the unliker are separate classes on purpose. The scanner is
pure observation and is what the default dry run exercises; the unliker is the
only code in the project that changes anything on Instagram, and it refuses to
act while ``config.dry_run`` is set.

Every unlike follows the same five steps, and none of them are skipped:

1. locate the item through a stable selector (never a coordinate);
2. confirm it is *currently liked* before touching it;
3. activate the normal UI control a person would use;
4. verify the resulting state actually changed;
5. hand a result back for the worker to persist.
"""

from __future__ import annotations

import enum
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from core.errors import (
    ElementNotFoundError,
    FatalError,
    RateLimitedError,
    UIChangedError,
    VerificationFailedError,
    classify,
)
from core.logging_setup import get_logger, safe_url
from instagram import dom
from instagram.navigation import Navigator
from instagram.selectors import SelectorRegistry, shortcode_from_url

log = get_logger("likes")


class Outcome(str, enum.Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class LikedItem:
    """A liked post as seen in the grid."""

    identifier: str
    url: str | None = None
    media_type: str | None = None

    @property
    def absolute_url(self) -> str | None:
        return self.url


@dataclass(frozen=True)
class UnlikeResult:
    identifier: str
    outcome: Outcome
    detail: str = ""
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.COMPLETED


def _media_type(url: str | None) -> str:
    if not url:
        return "unknown"
    lowered = url.lower()
    if "/reel" in lowered:
        return "reel"
    if "/tv/" in lowered:
        return "igtv"
    if "/p/" in lowered:
        return "post"
    return "unknown"


class LikesScanner:
    """Read-only discovery of liked posts. Never clicks anything."""

    def __init__(self, navigator: Navigator, config: Any, registry: SelectorRegistry | None = None):
        self.navigator = navigator
        self.config = config
        self.selectors = registry or navigator.selectors

    @property
    def page(self) -> Any:
        return self.navigator.page

    # ------------------------------------------------------------------
    def scan_visible(self) -> list[LikedItem]:
        """Every liked item currently rendered in the DOM.

        De-duplicated by identifier: Instagram renders the same permalink more
        than once (carousel children, hover overlays), and counting those twice
        would inflate every number the user sees.
        """
        items: dict[str, LikedItem] = {}
        unidentifiable = 0

        for locator in dom.all_locators(self.page, self.selectors.get("likes_grid_item")):
            try:
                href = locator.get_attribute("href")
            except Exception as exc:  # noqa: BLE001
                raise classify(exc) from exc

            if not href:
                # A tile may be a container rather than the link itself. Its
                # nested permalink is the same post, so preferring it keeps
                # one post to one identifier — otherwise the container and
                # the anchor inside it would each be counted separately.
                href = self._nested_permalink(locator)

            identifier = shortcode_from_url(href or "")
            url = self._absolute(href) if href else None

            if identifier is None:
                identifier = self._fallback_identifier(locator)
                if identifier is None:
                    unidentifiable += 1
                    continue

            if identifier not in items:
                items[identifier] = LikedItem(
                    identifier=identifier, url=url, media_type=_media_type(href)
                )

        if unidentifiable:
            # Not fatal, but worth surfacing: it usually means a markup change.
            log.warning(
                "%d rendered item(s) had no usable identifier and were ignored",
                unidentifiable,
            )
        return list(items.values())

    def _absolute(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return self.config.base_url.rstrip("/") + "/" + href.lstrip("/")

    def _nested_permalink(self, locator: Any) -> str | None:
        """The first post permalink inside this element, if it has one."""
        try:
            anchor = locator.locator('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]').first
            if anchor.count() == 0:
                return None
            return anchor.get_attribute("href")
        except Exception:  # noqa: BLE001
            return None

    def _fallback_identifier(self, locator: Any) -> str | None:
        """Derive an identifier from the thumbnail when there is no permalink.

        Uses the CDN path (without its signed query string, which rotates), so
        the same media yields the same identifier across runs. Returns None
        rather than inventing a positional id — positions shift as items are
        removed, and a wrong identifier means processing the wrong post.
        """
        try:
            image = locator.locator("img").first
            if image.count() == 0:
                return None
            src = image.get_attribute("src") or ""
        except Exception:  # noqa: BLE001
            return None
        path = src.split("?", 1)[0]
        if not path:
            return None
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        return f"media/{digest}"

    # ------------------------------------------------------------------
    def discover(
        self,
        *,
        target: int | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[LikedItem]:
        """Scroll the likes surface and return everything found.

        ``target`` stops the scrolling early; with hundreds of thousands of
        likes, loading the whole history before doing any work would be both
        slow and fragile.
        """
        if not self.navigator.has_liked_content():
            log.info("No liked content is available to scan")
            self._report_if_not_genuinely_empty()
            return []

        def report(count: int) -> None:
            if on_progress:
                on_progress(count)

        self.navigator.scroll_until_stable(target=target, on_progress=report)
        items = self.scan_visible()
        log.info("Scan found %d distinct liked item(s)", len(items))
        if not items:
            # The page looked like the likes surface but yielded nothing, so
            # something matched that should not have. Say what is really
            # there rather than reporting a confident zero.
            self.navigator.report_diagnostics("likes-page-scanned-zero-items")
        return items

    def _report_if_not_genuinely_empty(self) -> None:
        """Explain a "nothing here" that is not Instagram's own empty state."""
        if dom.is_present(self.page, self.selectors, "likes_empty_state"):
            return
        self.navigator.report_diagnostics("likes-page-no-content-recognised")

    def dry_run_report(self, items: Sequence[LikedItem]) -> dict[str, Any]:
        """Summary for the dry-run screen. Reads nothing beyond ``items``."""
        by_type: dict[str, int] = {}
        for item in items:
            by_type[item.media_type or "unknown"] = by_type.get(item.media_type or "unknown", 0) + 1
        return {
            "detected": len(items),
            "by_media_type": dict(sorted(by_type.items())),
            "sample": [item.identifier for item in items[:10]],
        }


class UnlikeStrategy:
    """Common behaviour for the two ways of removing a like."""

    name = "base"

    def __init__(self, navigator: Navigator, config: Any, registry: SelectorRegistry | None = None):
        self.navigator = navigator
        self.config = config
        self.selectors = registry or navigator.selectors

    @property
    def page(self) -> Any:
        return self.navigator.page

    def available(self) -> bool:
        return True

    def unlike(self, item: LikedItem) -> UnlikeResult:  # pragma: no cover - interface
        raise NotImplementedError

    def unlike_many(self, items: Sequence[LikedItem]) -> list[UnlikeResult]:
        return [self.unlike(item) for item in items]

    # -- shared helpers -------------------------------------------------
    def _guard(self) -> None:
        """Refuse to act in dry-run mode, wherever we are called from."""
        if getattr(self.config, "dry_run", True):
            raise FatalError(
                "Refusing to unlike: dry-run mode is enabled. This is a bug — the "
                "worker should not have reached a strategy in dry-run mode."
            )

    def _confirm_if_asked(self) -> bool:
        """Accept a confirmation dialog if Instagram shows one."""
        dialog = dom.try_resolve(self.page, self.selectors, "confirm_dialog")
        if dialog is None:
            return False
        button = dom.try_resolve(dialog, self.selectors, "confirm_unlike_button")
        if button is None:
            raise UIChangedError(
                "A confirmation dialog appeared but it has no recognisable "
                "confirm control; stopping rather than clicking blindly."
            )
        log.debug("Confirming unlike in dialog")
        dom.click(button, timeout=self.config.action_timeout_ms, what="confirm unlike")
        self.navigator.settle(timeout=3.0)
        return True


class SinglePostStrategy(UnlikeStrategy):
    """Open each post and use its own like control.

    The most verifiable route: the heart control reports the current state
    before and after, so "did it work?" is answered by the page rather than
    assumed from the click.
    """

    name = "item"

    def unlike(self, item: LikedItem) -> UnlikeResult:
        self._guard()
        if not item.url:
            return UnlikeResult(
                item.identifier,
                Outcome.FAILED,
                "no permalink recorded for this item",
                "no_url",
            )

        log.debug("Opening %s", safe_url(item.url))
        self.navigator.open_post(item.url)

        # Step 3 of the contract: confirm it is currently liked.
        control = dom.try_resolve(self.page, self.selectors, "post_unlike_control")
        if control is None:
            if dom.is_present(self.page, self.selectors, "post_like_control"):
                log.info("%s is already not liked; skipping", item.identifier)
                return UnlikeResult(
                    item.identifier, Outcome.SKIPPED, "already not liked", "not_liked"
                )
            raise ElementNotFoundError(
                f"No like/unlike control found on {safe_url(item.url)}"
            )

        dom.click(control, timeout=self.config.action_timeout_ms, what="unlike control")
        self._confirm_if_asked()

        # Step 5: never trust the click.
        if self._verify_unliked():
            return UnlikeResult(item.identifier, Outcome.COMPLETED)

        self.navigator.raise_for_page_state()  # raises on a block dialog
        raise VerificationFailedError(
            f"Clicked unlike on {item.identifier} but the control never changed to "
            "'Like'; treating it as unconfirmed rather than done."
        )

    def _verify_unliked(self, timeout: float = 8.0) -> bool:
        """Poll until the control reports the post is no longer liked."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if dom.is_present(self.page, self.selectors, "post_like_control"):
                if not dom.is_present(self.page, self.selectors, "post_unlike_control"):
                    return True
            if dom.is_present(self.page, self.selectors, "rate_limit_indicator"):
                raise RateLimitedError("Instagram blocked the unlike action")
            time.sleep(0.3)
        return False


class SelectModeStrategy(UnlikeStrategy):
    """Use Instagram's own multi-select flow on the Likes page.

    This is the route Instagram provides for removing many likes, and it is far
    gentler on the site than opening one post per like: a handful of clicks and
    one submission per chunk, instead of a full page load per item.
    """

    name = "select"

    def available(self) -> bool:
        return dom.is_present(self.page, self.selectors, "select_mode_button")

    def unlike(self, item: LikedItem) -> UnlikeResult:
        return self.unlike_many([item])[0]

    def unlike_many(self, items: Sequence[LikedItem]) -> list[UnlikeResult]:
        self._guard()
        if not items:
            return []

        chunk = list(items)[: self.config.select_chunk_size]
        results: dict[str, UnlikeResult] = {}

        self._enter_select_mode()

        selected: list[LikedItem] = []
        for item in chunk:
            tile = self._find_tile(item)
            if tile is None:
                results[item.identifier] = UnlikeResult(
                    item.identifier,
                    Outcome.SKIPPED,
                    "no longer present on the likes page",
                    "absent",
                )
                continue
            try:
                dom.click(tile, timeout=self.config.action_timeout_ms, what=f"tile {item.identifier}")
            except Exception as exc:  # noqa: BLE001
                error = classify(exc)
                results[item.identifier] = UnlikeResult(
                    item.identifier, Outcome.FAILED, str(error), error.code
                )
                continue
            selected.append(item)

        if not selected:
            self._leave_select_mode()
            return [results.get(i.identifier, UnlikeResult(i.identifier, Outcome.SKIPPED, "not selected", "absent")) for i in chunk]

        submit = dom.try_resolve(self.page, self.selectors, "bulk_unlike_button")
        if submit is None:
            self._leave_select_mode()
            raise UIChangedError(
                f"Selected {len(selected)} item(s) but found no bulk Unlike control."
            )
        dom.click(submit, timeout=self.config.action_timeout_ms, what="bulk unlike")
        self._confirm_if_asked()
        self.navigator.settle(timeout=5.0)
        self.navigator.raise_for_page_state()

        # Verification: a successfully unliked item leaves the likes grid.
        for item in selected:
            if self._verify_gone(item):
                results[item.identifier] = UnlikeResult(item.identifier, Outcome.COMPLETED)
            else:
                results[item.identifier] = UnlikeResult(
                    item.identifier,
                    Outcome.FAILED,
                    "still present on the likes page after submitting",
                    "verification_failed",
                )

        self._leave_select_mode()
        return [
            results.get(i.identifier, UnlikeResult(i.identifier, Outcome.FAILED, "no result recorded", "unknown"))
            for i in chunk
        ]

    # -- internals ------------------------------------------------------
    def _enter_select_mode(self) -> None:
        if dom.is_present(self.page, self.selectors, "select_mode_active"):
            return
        button = dom.try_resolve(self.page, self.selectors, "select_mode_button")
        if button is None:
            raise UIChangedError(
                "Instagram's 'Select' control was not found on the likes page."
            )
        dom.click(button, timeout=self.config.action_timeout_ms, what="select mode")
        self.navigator.settle(timeout=3.0)

    def _leave_select_mode(self) -> None:
        cancel = dom.try_resolve(self.page, self.selectors, "select_mode_active")
        if cancel is not None:
            try:
                dom.click(cancel, timeout=self.config.action_timeout_ms, what="leave select mode")
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not leave select mode: %s", exc)

    def _tile_locator(self, item: LikedItem) -> Any:
        """Locate a tile by its permalink — an attribute, never a position."""
        return self.page.locator(f'a[href*="/{item.identifier}/"]').first

    def _find_tile(self, item: LikedItem) -> Any | None:
        locator = self._tile_locator(item)
        try:
            if locator.count() == 0:
                return None
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc
        # Prefer an explicit checkbox inside the tile when the UI offers one.
        checkbox = dom.try_resolve(locator, self.selectors, "item_checkbox")
        return checkbox if checkbox is not None else locator

    def _verify_gone(self, item: LikedItem, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._tile_locator(item).count() == 0:
                    return True
            except Exception:  # noqa: BLE001
                return False
            time.sleep(0.3)
        return False


def build_strategy(
    navigator: Navigator,
    config: Any,
    registry: SelectorRegistry | None = None,
) -> UnlikeStrategy:
    """Pick a strategy according to ``config.unlike_strategy``.

    ``auto`` prefers Instagram's native multi-select flow when the page offers
    it (fewer page loads, less load on the site) and falls back to the
    per-post route, which works anywhere a post can be opened.
    """
    choice = getattr(config, "unlike_strategy", "auto")
    if choice == "item":
        return SinglePostStrategy(navigator, config, registry)
    if choice == "select":
        return SelectModeStrategy(navigator, config, registry)

    select = SelectModeStrategy(navigator, config, registry)
    if select.available():
        log.info("Using Instagram's multi-select flow")
        return select
    log.info("Multi-select is unavailable here; using the per-post flow")
    return SinglePostStrategy(navigator, config, registry)
