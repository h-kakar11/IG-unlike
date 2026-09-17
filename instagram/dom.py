"""Turning :class:`~instagram.selectors.Selector` candidates into live elements.

Kept separate from ``selectors.py`` so that the selector definitions stay a
plain, reviewable data file with no Playwright import, and so that the
resolution *policy* — try candidates in order, prefer visible matches, never
guess — lives in one place.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from core.errors import ElementNotFoundError, classify
from core.logging_setup import get_logger
from instagram.selectors import Selector, SelectorRegistry

log = get_logger("dom")


def to_locator(scope: Any, selector: Selector) -> Any:
    """Build a Playwright locator for one candidate, relative to ``scope``.

    ``scope`` may be a Page, a Frame or another Locator — they all expose the
    same locator-building API.
    """
    if selector.kind == "css":
        return scope.locator(selector.value)
    if selector.kind == "xpath":
        return scope.locator(f"xpath={selector.value}")
    if selector.kind == "role":
        if selector.name:
            return scope.get_by_role(
                selector.value, name=selector.name, exact=selector.exact
            )
        return scope.get_by_role(selector.value)
    if selector.kind == "text":
        return scope.get_by_text(selector.value, exact=selector.exact)
    if selector.kind == "label":
        return scope.get_by_label(selector.value, exact=selector.exact)
    if selector.kind == "placeholder":
        return scope.get_by_placeholder(selector.value, exact=selector.exact)
    if selector.kind == "testid":
        return scope.get_by_test_id(selector.value)
    raise ValueError(f"Unsupported selector kind {selector.kind!r}")


def _count(locator: Any) -> int:
    try:
        return locator.count()
    except Exception as exc:  # noqa: BLE001 - a dead page is handled upstream
        raise classify(exc) from exc


#: How many matches of one candidate to test for visibility before moving on.
#: Instagram renders placeholder, prefetch and virtualisation nodes among the
#: real ones, so the *first* match being hidden says nothing about the rest.
#: Bounded because this runs in a polling loop.
VISIBILITY_SAMPLE = 8


def find_first(
    scope: Any,
    selectors: Sequence[Selector],
    *,
    require_visible: bool = True,
) -> tuple[Any, Selector] | None:
    """Return ``(locator, selector)`` for the first candidate that matches.

    Two passes: visible matches first across all candidates, then — if
    ``require_visible`` is False — any match at all. Without the two passes a
    hidden element from an early candidate would mask a usable later one.

    Within a candidate, several matches are sampled rather than only the
    first. A grid of thirty-six thumbnails whose first node happens to be a
    hidden placeholder is still a grid, and treating it as "no match" is how
    a page that is plainly full of content reads as unrecognisable.
    """
    attached: tuple[Any, Selector] | None = None
    for selector in selectors:
        try:
            locator = to_locator(scope, selector)
        except ValueError:
            log.warning("Skipping malformed selector %s", selector.describe())
            continue
        count = _count(locator)
        if count == 0:
            continue
        if attached is None and not require_visible:
            # Only the relaxed pass ever uses this, and resolving it costs a
            # round trip on a path that polls.
            attached = (locator.first, selector)
        for index in range(min(count, VISIBILITY_SAMPLE)):
            element = locator.nth(index)
            try:
                if element.is_visible():
                    return element, selector
            except Exception:  # noqa: BLE001 - an unreadable node is a miss
                continue
    if not require_visible and attached is not None:
        return attached
    return None


def resolve(
    scope: Any,
    registry: SelectorRegistry,
    group: str,
    *,
    require_visible: bool = True,
    what: str | None = None,
) -> Any:
    """Locate ``group`` or raise :class:`ElementNotFoundError`.

    The error names every strategy that was tried, which is what makes a
    changed Instagram UI diagnosable from the log alone.
    """
    match = find_first(scope, registry.get(group), require_visible=require_visible)
    if match is not None:
        locator, selector = match
        log.debug("Resolved %s via %s", group, selector.describe())
        return locator
    tried = ", ".join(s.describe() for s in registry.get(group))
    raise ElementNotFoundError(
        f"Could not find {what or group}. Tried: {tried}. If Instagram's "
        "interface has changed, override this group in selectors.json."
    )


def try_resolve(
    scope: Any,
    registry: SelectorRegistry,
    group: str,
    *,
    require_visible: bool = True,
) -> Any | None:
    """Like :func:`resolve` but returns ``None`` instead of raising."""
    match = find_first(scope, registry.get(group), require_visible=require_visible)
    return match[0] if match else None


def is_present(
    scope: Any,
    registry: SelectorRegistry,
    group: str,
    *,
    require_visible: bool = True,
) -> bool:
    """Whether any candidate in ``group`` currently matches."""
    return (
        find_first(scope, registry.get(group), require_visible=require_visible)
        is not None
    )


def matched_selector(
    scope: Any, registry: SelectorRegistry, group: str
) -> Selector | None:
    """Which candidate matched — used by diagnostics and the dev report."""
    match = find_first(scope, registry.get(group))
    return match[1] if match else None


def all_locators(scope: Any, selectors: Iterable[Selector]) -> list[Any]:
    """Every element matching any candidate, de-duplicated by element handle."""
    found: list[Any] = []
    for selector in selectors:
        try:
            locator = to_locator(scope, selector)
        except ValueError:
            continue
        count = _count(locator)
        for index in range(count):
            found.append(locator.nth(index))
    return found


def click(locator: Any, *, timeout: float | None = None, what: str = "element") -> None:
    """Click through the accessibility tree, never by coordinates.

    Playwright's ``click`` performs actionability checks (visible, stable,
    enabled, receives events) before dispatching, which is exactly the
    behaviour we want: if the control is not genuinely clickable we get an
    exception instead of a click landing somewhere unintended.
    """
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:  # noqa: BLE001 - scrolling is best-effort
        pass
    try:
        locator.click(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        error = classify(exc)
        error.args = (f"clicking {what}: {error.args[0] if error.args else exc}",)
        raise error from exc


def text_content(locator: Any, limit: int = 200) -> str:
    try:
        return (locator.inner_text(timeout=2000) or "").strip()[:limit]
    except Exception:  # noqa: BLE001
        return ""
