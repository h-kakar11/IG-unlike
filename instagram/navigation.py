"""Getting to, and staying on, the right Instagram page.

Responsibilities, in the order the CLI uses them:

1. open Instagram;
2. work out the authentication state — and, if a human is needed, say so and
   wait rather than trying to do anything clever;
3. navigate to Your Activity -> Interactions -> Likes;
4. tell the caller whether there is any liked content to work with;
5. absorb loading states and the infinite-scroll pagination.

Nothing here modifies anything. Navigation is read-only by construction.
"""

from __future__ import annotations

import enum
import re
import time
from pathlib import Path
from typing import Any, Callable

from core.errors import (
    AuthenticationRequiredError,
    CheckpointError,
    RateLimitedError,
    ServerError,
    SessionExpiredError,
    UIChangedError,
    classify,
)
from core.logging_setup import get_logger, safe_url
from instagram import dom
from instagram.selectors import (
    CHECKPOINT_URL_MARKERS,
    LIKES_PATHS,
    LOGGED_OUT_URL_MARKERS,
    STRUCTURE_PROBES,
    SelectorRegistry,
)

log = get_logger("navigation")


class AuthState(enum.Enum):
    """What the current page says about our session."""

    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    CHECKPOINT = "checkpoint"
    UNKNOWN = "unknown"

    @property
    def needs_human(self) -> bool:
        return self in (AuthState.LOGGED_OUT, AuthState.CHECKPOINT)


class Navigator:
    """Read-only navigation over an authenticated Instagram session."""

    def __init__(self, session: Any, config: Any, registry: SelectorRegistry | None = None):
        self.session = session
        self.config = config
        self.selectors = registry or SelectorRegistry()

    # ------------------------------------------------------------------
    # Page-level state
    # ------------------------------------------------------------------
    @property
    def page(self) -> Any:
        return self.session.page

    def open_home(self) -> None:
        self.session.goto(self.config.base_url + "/")
        self.settle()

    def settle(self, *, timeout: float | None = None) -> None:
        """Wait for the page to stop obviously loading.

        Instagram is a single-page app: ``networkidle`` never really arrives,
        so this waits for the DOM plus the disappearance of any known spinner,
        and gives up quietly rather than failing the caller.
        """
        deadline = time.monotonic() + (timeout or self.config.scroll_pause * 4)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.config.nav_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            log.debug("load-state wait ended early: %s", exc)
        while time.monotonic() < deadline:
            if not dom.is_present(self.page, self.selectors, "loading_indicator"):
                return
            time.sleep(0.25)
        log.debug("Loading indicator still present after settle timeout")

    def detect_auth_state(self) -> AuthState:
        """Classify the current page. Checks the URL first, then the DOM.

        Order matters: a checkpoint page can contain login-looking controls, so
        checkpoints are recognised before "logged out".
        """
        url = self.session.current_url()
        lowered = url.lower()

        if any(marker in lowered for marker in CHECKPOINT_URL_MARKERS):
            log.warning("Security checkpoint detected at %s", safe_url(url))
            return AuthState.CHECKPOINT
        if dom.is_present(self.page, self.selectors, "checkpoint_indicator"):
            log.warning("Security checkpoint detected on page")
            return AuthState.CHECKPOINT
        if any(marker in lowered for marker in LOGGED_OUT_URL_MARKERS):
            return AuthState.LOGGED_OUT
        if dom.is_present(self.page, self.selectors, "logged_in_indicator"):
            return AuthState.LOGGED_IN
        if dom.is_present(self.page, self.selectors, "logged_out_indicator"):
            return AuthState.LOGGED_OUT
        return AuthState.UNKNOWN

    def is_logged_in(self) -> bool:
        return self.detect_auth_state() is AuthState.LOGGED_IN

    def raise_for_page_state(self) -> None:
        """Convert a throttle/error/auth page into the matching exception.

        Called after every action so that "the click worked but Instagram
        showed a block dialog" is never mistaken for success.
        """
        if dom.is_present(self.page, self.selectors, "rate_limit_indicator"):
            raise RateLimitedError(
                "Instagram is showing an action-block or 'try again later' message"
            )
        state = self.detect_auth_state()
        if state is AuthState.CHECKPOINT:
            raise CheckpointError(
                "Instagram is showing a security checkpoint. Open the browser "
                "window and resolve it yourself; this tool will not attempt to."
            )
        if state is AuthState.LOGGED_OUT:
            raise SessionExpiredError("The Instagram session is no longer valid")
        if dom.is_present(self.page, self.selectors, "server_error_indicator"):
            raise ServerError("Instagram returned an error page")

    # ------------------------------------------------------------------
    # Authentication (manual, always)
    # ------------------------------------------------------------------
    def ensure_authenticated(
        self,
        *,
        prompt: Callable[[str], None] | None = None,
        max_rounds: int = 10,
    ) -> AuthState:
        """Make sure we are logged in, asking the user to do it by hand.

        This tool never types a password, never fills a login form and never
        attempts a challenge. If a human is required, ``prompt`` is called with
        an explanation and is expected to block until the user says they are
        done (the CLI uses "Press ENTER when ready...").
        """
        self.open_home()
        for attempt in range(1, max_rounds + 1):
            state = self.detect_auth_state()
            log.info("Authentication state: %s", state.value)

            if state is AuthState.LOGGED_IN:
                self.dismiss_interstitials()
                return state

            if prompt is None:
                if state is AuthState.CHECKPOINT:
                    raise CheckpointError(
                        "A security checkpoint is showing and no interactive prompt "
                        "is available. Run the tool interactively and resolve it in "
                        "the browser window."
                    )
                raise AuthenticationRequiredError(
                    "Instagram login required and no interactive prompt is available."
                )

            if state is AuthState.CHECKPOINT:
                prompt(
                    "Instagram is showing a security checkpoint (2FA, a challenge or "
                    "an account review).\n"
                    "Resolve it yourself in the browser window. This tool will not "
                    "attempt to bypass it.\n"
                    "Press ENTER when you are back on Instagram..."
                )
            else:
                prompt(
                    "Instagram login required.\n"
                    "Please log in manually in the browser window that just opened.\n"
                    "This application never asks for, sees or stores your password.\n"
                    "Press ENTER when ready..."
                )

            self.settle()
            if self.detect_auth_state() is not AuthState.LOGGED_IN:
                # Give the SPA a moment, then re-load before re-checking.
                self.open_home()
            log.debug("Re-checking authentication (round %d)", attempt)

        raise AuthenticationRequiredError(
            "Still not logged in after several attempts. Log in to Instagram in the "
            "browser window, then start the tool again."
        )

    def dismiss_interstitials(self, *, max_dialogs: int = 3) -> int:
        """Close cookie banners and "Save your login info?" style prompts.

        Only buttons in the ``dismiss_dialog`` group are ever clicked, and each
        is clicked at most once per call.
        """
        dismissed = 0
        for _ in range(max_dialogs):
            control = dom.try_resolve(self.page, self.selectors, "dismiss_dialog")
            if control is None:
                break
            label = dom.text_content(control, 40) or "dialog"
            try:
                dom.click(control, timeout=self.config.action_timeout_ms, what="dialog button")
            except Exception as exc:  # noqa: BLE001 - best effort by design
                log.debug("Could not dismiss %s: %s", label, exc)
                break
            log.info("Dismissed interstitial: %s", label)
            dismissed += 1
            self.settle(timeout=2.0)
        return dismissed

    #: Groups worth reporting match counts for when the likes surface fails
    #: to resolve. Kept short and targeted rather than "all 20 groups" so the
    #: summary stays small enough to paste into a bug report.
    _DIAGNOSTIC_GROUPS = (
        "likes_container",
        "likes_grid_item",
        "likes_empty_state",
        "loading_indicator",
        "logged_in_indicator",
    )

    def _diagnostic_summary(self, label: str) -> str:
        """A short, safe-to-paste report of what the page actually contains.

        Complements the HTML/screenshot dump rather than replacing it: those
        are the ground truth but are large and can carry personal content
        (captions, usernames), which makes them awkward to paste into a bug
        report or chat message. This contains only route names and match
        counts — never hrefs, text or usernames — so it is short and safe to
        share, and is logged at warning level so it lands directly in the
        console instead of only in a file.
        """
        lines = [f"--- Diagnostic summary: {label} ---", f"URL: {safe_url(self.session.current_url())}"]
        try:
            lines.append(f"Title: {self.page.title()!r}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Title: (unavailable: {exc})")

        lines.append("Link prefixes on this page (route only, no post/user data):")
        histogram = self.session.link_prefix_histogram()
        if histogram:
            for prefix, count in histogram:
                lines.append(f"  {prefix:<20} {count}")
        else:
            lines.append("  (none found, or could not be read)")

        lines.append("Selector group match counts (0 means every candidate failed):")
        for group in self._DIAGNOSTIC_GROUPS:
            lines.append(f"  {group}:")
            for selector in self.selectors.get(group):
                try:
                    count: object = dom.to_locator(self.page, selector).count()
                except Exception as exc:  # noqa: BLE001
                    count = f"error: {exc}"
                lines.append(f"    {selector.describe():<55} -> {count}")

        lines.extend(self._structure_lines())
        lines.append("--- end of diagnostic summary ---")
        return "\n".join(lines)

    def _structure_lines(self) -> list[str]:
        """Element counts describing the page's shape, never its content.

        This is what turns "nothing matched" into an answer: a grid of
        permalink anchors, a grid of clickable divs wrapping thumbnails and a
        genuinely empty page have three very different shapes, and the counts
        alone tell them apart.
        """
        report = self.session.structure_report(STRUCTURE_PROBES)
        if not report:
            return ["Page structure: (unavailable)"]

        lines = [
            "Page structure (element counts only, no content):",
            f"  <main> present: {report.get('main_present')}"
            f"   elements within it: {report.get('scope_elements')}",
        ]
        for label, key in (("Tags", "tags"), ("Roles", "roles")):
            counts = report.get(key) or {}
            ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:12]
            rendered = ", ".join(f"{name}={count}" for name, count in ranked)
            lines.append(f"  {label}: {rendered or '(none)'}")

        lines.append("  Probe matches (-1 = selector unsupported here):")
        for selector, count in (report.get("probes") or {}).items():
            lines.append(f"    {selector:<45} -> {count}")
        return lines

    def report_diagnostics(self, label: str) -> str:
        """Explain a page that matched nothing, right where it failed.

        The summary is *always* logged, because telling a user "re-run with a
        flag" at the moment something breaks wastes the run that already
        broke — and because this summary is counts-only, so there is nothing
        in it that needs a user's permission to print. The HTML and the
        screenshot stay behind ``--debug``: those carry real content.
        """
        summary = self._diagnostic_summary(label)
        log.warning("%s", summary)
        self._dump_diagnostics(label, summary=summary)
        return summary

    def _dump_diagnostics(self, label: str, *, summary: str | None = None) -> None:
        """Save the current page's HTML, a screenshot and a summary, if
        --debug is on.

        This is the difference between "nothing matched, guess why" and
        "here is exactly what Instagram rendered": a page that fails every
        known selector is otherwise undiagnosable without this. Opt-in and
        local-only — see :meth:`BrowserSession.dump_html`.
        """
        if not getattr(self.config, "debug", False):
            return
        debug_dir = Path(getattr(self.config, "debug_dir", None) or "data/debug")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-") or "page"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = debug_dir / f"{stamp}-{slug}"
        self.session.dump_html(base.with_suffix(".html"))
        self.session.screenshot(base.with_suffix(".png"), full_page=True)

        if summary is None:
            summary = self._diagnostic_summary(label)
            log.warning("%s", summary)
        try:
            base.with_suffix(".txt").parent.mkdir(parents=True, exist_ok=True)
            base.with_suffix(".txt").write_text(summary, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not write diagnostic summary file: %s", exc)

        log.warning("Saved diagnostic dump for inspection: %s.html / .png / .txt", base)

    # ------------------------------------------------------------------
    # The likes surface
    # ------------------------------------------------------------------
    def navigate_to_likes(self) -> str:
        """Open Your Activity -> Likes, trying each known path in turn.

        Returns the URL that worked. Raises :class:`UIChangedError` if none of
        them produce a page we recognise — better than scrolling an unknown
        page and clicking things on it.
        """
        base = self.config.base_url.rstrip("/")
        paths: list[str] = [self.config.likes_path]
        paths += [p for p in LIKES_PATHS if p != self.config.likes_path]

        last_error: Exception | None = None
        for path in paths:
            url = base + path
            log.info("Opening liked content at %s", safe_url(url))
            try:
                self.session.goto(url)
                self.settle()
                self.raise_for_page_state()
                self.dismiss_interstitials()
            except (RateLimitedError, CheckpointError, SessionExpiredError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = classify(exc)
                log.warning("Could not use %s: %s", safe_url(url), last_error)
                continue

            if self.on_likes_page():
                log.info("Liked content surface is open")
                return url

            # Some surfaces render their grid only once something scrolls —
            # an intersection observer never fires on a page nobody touches.
            # One nudge is cheap; concluding "unreadable" wrongly is not.
            self._nudge()
            if self.on_likes_page(timeout=5.0):
                log.info("Liked content appeared after a scroll nudge")
                return url

            log.warning(
                "%s loaded but no liked content or empty state was recognised",
                safe_url(url),
            )
            self.report_diagnostics(path)

        hint = (
            " A diagnostic summary of what each page actually contained was printed "
            "above — it is counts only, so it is safe to paste into a bug report."
        )
        if getattr(self.config, "debug", False):
            hint += f" The page HTML and a screenshot were also saved under {self.config.debug_dir}."
        else:
            hint += " Re-run with --debug to also save the page HTML and a screenshot."
        raise UIChangedError(
            "Could not open Instagram's liked-content page. Tried: "
            + ", ".join(base + p for p in paths)
            + ". Instagram may have moved or renamed this surface — check "
            "likes_path in your config and the selectors in selectors.json."
            + hint
            + (f" Last error: {last_error}" if last_error else "")
        )

    def _nudge(self) -> None:
        """Scroll once to wake a lazily-rendered grid. Never fails the caller.

        Read-only: scrolling changes nothing on the account, and it is the
        one interaction that reliably triggers the observers a single-page
        app uses to decide a list is worth rendering.
        """
        try:
            self.page.mouse.wheel(0, 1200)
            self.page.evaluate("() => window.scrollBy(0, 1200)")
        except Exception as exc:  # noqa: BLE001
            log.debug("Scroll nudge failed: %s", exc)

    def on_likes_page(self, *, timeout: float | None = None) -> bool:
        """True once either liked items or the empty state have rendered."""
        deadline = time.monotonic() + (timeout if timeout is not None else 15.0)
        while True:
            if dom.is_present(self.page, self.selectors, "likes_grid_item"):
                return True
            if dom.is_present(self.page, self.selectors, "likes_empty_state"):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def has_liked_content(self) -> bool:
        """Whether there is anything to work on (False on the empty state)."""
        if dom.is_present(self.page, self.selectors, "likes_grid_item"):
            return True
        if dom.is_present(self.page, self.selectors, "likes_empty_state"):
            log.info("Instagram reports no liked content")
            return False
        return False

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------
    def visible_item_count(self) -> int:
        total = 0
        for selector in self.selectors.get("likes_grid_item"):
            try:
                total += dom.to_locator(self.page, selector).count()
            except Exception as exc:  # noqa: BLE001
                raise classify(exc) from exc
        return total

    def load_more(self) -> bool:
        """Advance the infinite scroll by one step.

        Returns True if more content appeared. Prefers an explicit "Load more"
        control when one exists, because a real button is a far more reliable
        signal than "did scrolling produce anything".
        """
        before = self.visible_item_count()

        button = dom.try_resolve(self.page, self.selectors, "load_more_button")
        if button is not None:
            log.debug("Clicking the load-more control")
            try:
                dom.click(button, timeout=self.config.action_timeout_ms, what="load more")
            except Exception as exc:  # noqa: BLE001
                log.debug("Load-more click failed, falling back to scrolling: %s", exc)
            else:
                self._wait_for_growth(before)
                return self.visible_item_count() > before

        try:
            self.page.mouse.wheel(0, 20_000)
            self.page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc

        self._wait_for_growth(before)
        after = self.visible_item_count()
        log.debug("Scroll: %d -> %d visible item(s)", before, after)
        return after > before

    def _wait_for_growth(self, before: int) -> None:
        """Poll briefly for the item count to increase after a scroll."""
        deadline = time.monotonic() + max(self.config.scroll_pause, 0.2) * 4
        while time.monotonic() < deadline:
            time.sleep(min(self.config.scroll_pause, 0.5) or 0.1)
            try:
                if self.visible_item_count() > before:
                    return
            except Exception:  # noqa: BLE001
                return
            if dom.is_present(self.page, self.selectors, "loading_indicator"):
                continue

    def scroll_until_stable(
        self,
        *,
        max_scrolls: int | None = None,
        max_stalls: int | None = None,
        target: int | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> int:
        """Scroll until the list stops growing, a target is met, or a cap hits.

        Returns the number of items visible at the end. ``target`` exists
        because a user with 250,000 likes must not be made to scroll the whole
        history before the first batch can start.
        """
        max_scrolls = max_scrolls if max_scrolls is not None else self.config.max_scrolls_per_pass
        max_stalls = max_stalls if max_stalls is not None else self.config.max_scroll_stalls

        stalls = 0
        count = self.visible_item_count()
        if on_progress:
            on_progress(count)

        for iteration in range(max_scrolls):
            if target is not None and count >= target:
                log.debug("Reached the discovery target of %d item(s)", target)
                break
            self.load_more()
            new_count = self.visible_item_count()
            if new_count > count:
                stalls = 0
                count = new_count
                if on_progress:
                    on_progress(count)
            else:
                stalls += 1
                log.debug("Scroll %d produced nothing new (stall %d/%d)", iteration + 1, stalls, max_stalls)
                if stalls >= max_stalls:
                    log.info("Reached the end of the available liked content")
                    break
            # A block dialog can appear mid-scroll; notice it now, not later.
            self.raise_for_page_state()

        return count

    def open_post(self, url: str) -> None:
        """Navigate to a single post permalink."""
        self.session.goto(url)
        self.settle()
        self.raise_for_page_state()

    def go_back(self) -> None:
        try:
            self.page.go_back(timeout=self.config.nav_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc
        self.settle()
