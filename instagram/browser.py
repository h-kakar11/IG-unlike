"""Playwright Chromium lifecycle with a persistent profile.

The persistent profile is the whole authentication story. Chromium keeps the
Instagram session in ``browser_profile_dir`` exactly as it would for a normal
browser window; this application never reads it, never copies it, and never
sends it anywhere. That is also why the browser is headed by default — the
user logs in themselves, in a real browser window, and the tool just waits.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Sequence

from core.errors import BrowserCrashedError, FatalError, classify
from core.logging_setup import get_logger, safe_url

log = get_logger("browser")

#: Chromium flags. Deliberately minimal: nothing here spoofs a user agent,
#: masks automation or otherwise tries to look like a different browser.
BASE_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",  # avoids a Chromium bug that breaks some dialogs
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
)


class BrowserSession:
    """Owns the Playwright driver, the persistent context and the active page."""

    def __init__(self, config: Any):
        self.config = config
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> "BrowserSession":
        """Launch Chromium with the persistent profile and open a page."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment problem
            raise FatalError(
                "Playwright is not installed. Run:\n"
                "    pip install -r requirements.txt\n"
                "    playwright install chromium"
            ) from exc

        profile_dir = Path(self.config.browser_profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._guard_profile_lock(profile_dir)

        log.info(
            "Launching Chromium (headless=%s, profile=%s)",
            self.config.headless,
            profile_dir,
        )
        self._playwright = sync_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": bool(self.config.headless),
            "args": list(BASE_ARGS) + list(self.config.browser_args),
            "viewport": {"width": 1280, "height": 900},
            "slow_mo": int(self.config.slow_mo_ms) or 0,
            # Instagram surfaces are locale-sensitive and so are our text
            # fallbacks; pin English so the shipped selectors line up.
            "locale": "en-US",
        }
        if self.config.browser_executable_path:
            launch_kwargs["executable_path"] = self.config.browser_executable_path
        if self.config.browser_channel:
            launch_kwargs["channel"] = self.config.browser_channel

        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            self._shutdown_playwright()
            raise self._launch_error(exc) from exc

        self._context.set_default_timeout(self.config.action_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.nav_timeout_ms)
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._context.on("close", lambda _: log.warning("Browser context closed"))
        self._page.on("crash", lambda _: log.error("Page crashed"))
        log.info("Browser ready")
        return self

    def _guard_profile_lock(self, profile_dir: Path) -> None:
        """Fail early and clearly if another Chromium owns this profile."""
        lock = profile_dir / "SingletonLock"
        if lock.exists() or lock.is_symlink():
            log.warning(
                "Profile lock present at %s — another browser may be using this "
                "profile. Close it if the launch fails.",
                lock,
            )

    def _launch_error(self, exc: Exception) -> FatalError:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            return FatalError(
                "Chromium is not installed for Playwright. Run:\n"
                "    playwright install chromium\n"
                "…or set browser_executable_path / browser_channel in your config."
            )
        if "ProcessSingleton" in message or "SingletonLock" in message:
            return FatalError(
                f"The browser profile at {self.config.browser_profile_dir} is already "
                "in use. Close any browser window using it and try again."
            )
        if "Missing X server" in message or "no display" in message.lower():
            return FatalError(
                "No display available for a headed browser. Either run this on a "
                "desktop session, or set headless=true (note: you cannot complete "
                "an Instagram login in headless mode)."
            )
        return FatalError(f"Could not launch the browser: {message}")

    def stop(self) -> None:
        """Close the browser, flushing the profile so the session survives."""
        if self._context is not None:
            try:
                self._context.close()
                log.info("Browser closed")
            except Exception as exc:  # noqa: BLE001
                log.warning("Error while closing the browser: %s", exc)
            finally:
                self._context = None
                self._page = None
        self._shutdown_playwright()

    def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("Playwright shutdown error: %s", exc)
            finally:
                self._playwright = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def page(self) -> Any:
        """The active page, raising if the browser is gone."""
        if self._page is None or self._context is None:
            raise BrowserCrashedError("The browser is not running")
        if self._page.is_closed():
            # A closed tab is recoverable as long as the context lives.
            log.warning("Active page was closed; opening a replacement")
            self._page = self._context.new_page()
        return self._page

    @property
    def context(self) -> Any:
        if self._context is None:
            raise BrowserCrashedError("The browser is not running")
        return self._context

    def is_alive(self) -> bool:
        if self._context is None or self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except Exception:  # noqa: BLE001
            return False

    def restart(self) -> "BrowserSession":
        """Recover from a crash by relaunching with the same profile."""
        log.warning("Restarting the browser")
        try:
            self.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ignoring error during restart shutdown: %s", exc)
        return self.start()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> Any:
        """Navigate, translating Playwright failures into our taxonomy."""
        log.debug("Navigating to %s", safe_url(url))
        try:
            return self.page.goto(
                url, wait_until=wait_until, timeout=self.config.nav_timeout_ms
            )
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc

    def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:  # noqa: BLE001
            return ""

    def screenshot(self, path: str | Path, *, full_page: bool = False) -> Path | None:
        """Capture a diagnostic screenshot. Never fails the caller.

        Screenshots of a logged-in Instagram page contain personal content, so
        they are only taken on explicit request (``--debug``) and are written
        inside the gitignored data directory, never uploaded anywhere.
        """
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=full_page)
            log.info("Saved diagnostic screenshot to %s", path)
            return path
        except Exception as exc:  # noqa: BLE001
            log.debug("Screenshot failed: %s", exc)
            return None

    def link_prefix_histogram(self, *, limit: int = 15) -> list[tuple[str, int]]:
        """Count on-page links by their first path segment, e.g. ``/p/`` -> 12.

        Route names are shared by every Instagram account and carry no
        personal content, unlike the full hrefs they come from (post codes,
        usernames). This is what makes a diagnostic summary safe to paste
        into a bug report: it shows what *kinds* of links a page has, never
        which ones. Never fails the caller — an empty list means "could not
        be read", same as a screenshot that could not be taken.
        """
        try:
            counts = self.page.evaluate(
                """() => {
                    const counts = {};
                    for (const a of document.querySelectorAll('a[href]')) {
                        let path = a.getAttribute('href') || '';
                        try { path = new URL(path, location.href).pathname; } catch (e) {}
                        const segment = path.split('/').filter(Boolean)[0] || '';
                        const key = '/' + segment + (segment ? '/' : '');
                        counts[key] = (counts[key] || 0) + 1;
                    }
                    return counts;
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Link prefix histogram failed: %s", exc)
            return []
        pairs = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        return pairs[:limit]

    def structure_report(self, probes: Sequence[str]) -> dict[str, Any]:
        """Describe the page's *shape* without reading any of its content.

        Returns element counts only — a tag histogram, a role histogram and a
        match count per probe selector. Counts cannot carry a caption, a
        username or a post code, which is what makes this safe to print and
        paste when a page fails to match. Never fails the caller.
        """
        try:
            return self.page.evaluate(
                """(probes) => {
                    const scope = document.querySelector('main') || document.body;
                    const tags = {};
                    const roles = {};
                    const all = scope ? scope.querySelectorAll('*') : [];
                    for (const el of all) {
                        const tag = el.tagName.toLowerCase();
                        tags[tag] = (tags[tag] || 0) + 1;
                        const role = el.getAttribute('role');
                        if (role) roles[role] = (roles[role] || 0) + 1;
                    }
                    const counts = {};
                    for (const selector of probes) {
                        try {
                            counts[selector] = document.querySelectorAll(selector).length;
                        } catch (e) {
                            counts[selector] = -1;  // unsupported by this browser
                        }
                    }
                    return {
                        main_present: !!document.querySelector('main'),
                        scope_elements: all.length,
                        tags: tags,
                        roles: roles,
                        probes: counts,
                    };
                }""",
                list(probes),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Structure report failed: %s", exc)
            return {}

    def dump_html(self, path: str | Path) -> Path | None:
        """Save the current page's rendered HTML. Never fails the caller.

        This is what makes a "selectors don't match" failure diagnosable
        without guessing: the actual markup Instagram served, saved locally
        and only on explicit request (``--debug``). It contains no image
        bytes, but page text can include personal content (captions,
        usernames), so it goes in the gitignored data directory and nowhere
        else — this tool never uploads anything on its own.
        """
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.page.content(), encoding="utf-8")
            log.info("Saved diagnostic HTML to %s", path)
            return path
        except Exception as exc:  # noqa: BLE001
            log.debug("HTML dump failed: %s", exc)
            return None


def clear_profile(profile_dir: str | Path) -> bool:
    """Delete a browser profile directory (logs the user out everywhere).

    Exposed for the CLI's "forget this browser profile" action; never called
    automatically.
    """
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return False
    shutil.rmtree(profile_dir)
    log.warning("Deleted browser profile at %s", profile_dir)
    return True
