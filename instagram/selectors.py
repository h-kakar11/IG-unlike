"""Every assumption about Instagram's DOM, in one file.

Instagram's markup is generated: class names are hashed and change without
notice. Nothing here relies on them. Each logical target is a *list* of
candidate strategies, preferring in order:

1. accessible role + name (``button`` named "Unlike") — survives restyling,
   and is the same thing a screen-reader user would activate;
2. ARIA labels and stable attributes (``aria-label``, ``role``);
3. URL shape (``a[href*="/p/"]``) — the permalink format is long-lived;
4. visible text — last resort, and locale-dependent.

Candidates are tried in order and the first one that actually matches is used,
so a single changed control degrades to the next strategy instead of breaking
the run. When nothing matches, the caller raises rather than guessing:
clicking blindly on a page we cannot read is how a tool like this does damage.

Users can override any group from a JSON file (``selectors.json``) without
editing code — see docs/CONFIGURATION.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.logging_setup import get_logger

log = get_logger("selectors")

VALID_KINDS = ("css", "role", "label", "text", "testid", "placeholder", "xpath")


@dataclass(frozen=True)
class Selector:
    """One way of finding an element."""

    kind: str
    value: str
    name: str | None = None
    exact: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(
                f"Unknown selector kind {self.kind!r}; expected one of {VALID_KINDS}"
            )

    def describe(self) -> str:
        if self.kind == "role":
            return f"role={self.value}[name={self.name!r}]"
        return f"{self.kind}={self.value!r}"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Selector":
        return cls(
            kind=str(raw.get("kind", "css")),
            value=str(raw["value"]),
            name=raw.get("name"),
            exact=bool(raw.get("exact", False)),
            note=str(raw.get("note", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "value": self.value}
        if self.name is not None:
            out["name"] = self.name
        if self.exact:
            out["exact"] = True
        if self.note:
            out["note"] = self.note
        return out


def css(value: str, note: str = "") -> Selector:
    return Selector("css", value, note=note)


def role(value: str, name: str | None = None, *, exact: bool = False, note: str = "") -> Selector:
    return Selector("role", value, name=name, exact=exact, note=note)


def text(value: str, *, exact: bool = False, note: str = "") -> Selector:
    return Selector("text", value, exact=exact, note=note)


def label(value: str, *, exact: bool = False, note: str = "") -> Selector:
    return Selector("label", value, exact=exact, note=note)


# ---------------------------------------------------------------------------
# URL fragments
# ---------------------------------------------------------------------------
#: Paths that mean "you are not authenticated".
LOGGED_OUT_URL_MARKERS = ("/accounts/login", "/accounts/signup", "/accounts/emailsignup")

#: Paths that mean "a human has to deal with a security screen".
#: The tool stops at all of these and never attempts to satisfy them.
CHECKPOINT_URL_MARKERS = (
    "/challenge",
    "/checkpoint",
    "/accounts/suspended",
    "/accounts/disabled",
    "/two_factor",
    "/accounts/login/two_factor",
    "/consent",
)

#: Where liked content lives, most-preferred first.
LIKES_PATHS = (
    "/your_activity/interactions/likes/",
    "/your_activity/interactions/likes",
    "/your_activity/interactions/",
)

#: Permalink shapes we know how to turn into a stable identifier.
POST_PATH_PREFIXES = ("/p/", "/reel/", "/reels/", "/tv/")

#: Structural shapes counted when a page matches nothing we know.
#:
#: These are *diagnostics*, never used to act on a page. Each one is counted
#: and the count is reported, which is enough to tell "the grid is anchors"
#: from "the grid is divs wrapping images" from "the page is empty" without
#: reading a single character of the user's content.
STRUCTURE_PROBES: tuple[str, ...] = (
    "main",
    "main a",
    "main a[href]",
    'a[href*="/p/"]',
    'main a[href*="/p/"]',
    'a[href*="/reel/"]',
    'a[href*="/tv/"]',
    "main img",
    'img[src*="cdninstagram"]',
    "main [role]",
    'main [role="button"]',
    'main div[role="button"]',
    'main [role="link"]',
    'main [role="listitem"]',
    'main [role="grid"]',
    'main [role="tablist"]',
    "main button",
    'main [tabindex="0"]',
    'main [style*="aspect-ratio"]',
    "main video",
    "main canvas",
    'main input[type="checkbox"]',
    'main [role="checkbox"]',
    "main a:has(img)",
    'main a[role="link"]:has(img)',
    'main div[role="button"]:has(img)',
    "main div:has(> img)",
    "main [data-testid]",
    'div[role="dialog"]',
    "iframe",
)


# ---------------------------------------------------------------------------
# Selector groups
# ---------------------------------------------------------------------------
DEFAULT_SELECTORS: dict[str, tuple[Selector, ...]] = {
    # -- Authentication state ------------------------------------------
    "logged_out_indicator": (
        css('input[name="username"]', "login form username field"),
        css('input[name="password"]', "login form password field"),
        role("button", "Log in"),
        role("link", "Log in"),
        text("Sign up"),
    ),
    "logged_in_indicator": (
        css('svg[aria-label="Home"]', "main nav home glyph"),
        css('a[href="/direct/inbox/"]', "direct messages link"),
        css('svg[aria-label="New post"]'),
        css('a[href*="/explore/"]'),
        role("navigation"),
    ),
    "checkpoint_indicator": (
        text("Suspicious Login Attempt"),
        text("We Detected An Unusual Login Attempt"),
        text("Help Us Confirm It's You"),
        text("Enter Security Code"),
        text("Enter the code"),
        text("Your account has been suspended"),
        text("We suspended your account"),
        text("Confirm it's you"),
        css('input[name="verificationCode"]'),
        css('input[name="security_code"]'),
    ),
    # -- Throttling / server trouble -----------------------------------
    "rate_limit_indicator": (
        text("Please wait a few minutes before you try again"),
        text("Try Again Later"),
        text("Action Blocked"),
        text("We restrict certain activity"),
        text("You're Temporarily Blocked"),
        text("Limit reached"),
    ),
    "server_error_indicator": (
        text("Something went wrong"),
        text("Sorry, something went wrong"),
        text("Page Not Found"),
        text("5xx Server Error"),
    ),
    # -- Interstitials to dismiss --------------------------------------
    "dismiss_dialog": (
        role("button", "Not Now"),
        role("button", "Not now"),
        role("button", "Cancel"),
        role("button", "Close"),
        css('svg[aria-label="Close"]'),
        role("button", "Allow all cookies"),
        role("button", "Decline optional cookies"),
    ),
    # -- The Likes surface ---------------------------------------------
    "likes_container": (
        role("main"),
        css('main[role="main"]'),
        css("main"),
    ),
    "likes_grid_item": (
        css('a[href*="/p/"]', "post permalink in the likes grid"),
        css('a[href*="/reel/"]', "reel permalink in the likes grid"),
        css('a[href*="/tv/"]'),
        css('[data-testid="liked-item"]', "mock/integration test hook"),
        # Your Activity is a multi-select surface, so its tiles may be
        # clickable containers rather than permalink anchors — tapping one
        # toggles selection instead of navigating. These match that shape.
        # They are last on purpose: a permalink identifies a post exactly,
        # whereas a tile only yields a thumbnail-derived identifier.
        css('main a[role="link"]:has(img)', "tile as a link wrapping a thumbnail"),
        css('main div[role="button"]:has(img)', "tile as a button wrapping a thumbnail"),
    ),
    "likes_empty_state": (
        text("No likes yet"),
        text("You haven't liked any posts"),
        text("No posts yet"),
        text("Nothing to show here"),
        css('[data-testid="likes-empty"]'),
    ),
    "loading_indicator": (
        css('svg[aria-label="Loading..."]'),
        css('[data-visualcompletion="loading-state"]'),
        role("progressbar"),
        css('[data-testid="loading"]'),
    ),
    "load_more_button": (
        role("button", "Load more"),
        role("button", "Show more"),
        css('[data-testid="load-more"]'),
    ),
    # -- Native multi-select flow --------------------------------------
    "select_mode_button": (
        role("button", "Select"),
        text("Select", exact=True),
        css('[data-testid="select-mode"]'),
    ),
    "select_mode_active": (
        role("button", "Cancel"),
        css('[data-testid="select-mode-active"]'),
    ),
    "item_checkbox": (
        role("checkbox"),
        css('input[type="checkbox"]'),
        css('[data-testid="item-checkbox"]'),
    ),
    "bulk_unlike_button": (
        role("button", "Unlike"),
        text("Unlike", exact=True),
        css('[data-testid="bulk-unlike"]'),
    ),
    "confirm_dialog": (
        role("dialog"),
        css('[role="dialog"]'),
        css('[data-testid="confirm-dialog"]'),
    ),
    "confirm_unlike_button": (
        role("button", "Unlike"),
        role("button", "Confirm"),
        text("Unlike", exact=True),
        css('[data-testid="confirm-unlike"]'),
    ),
    # -- Single-post flow ----------------------------------------------
    "post_unlike_control": (
        role("button", "Unlike"),
        css('svg[aria-label="Unlike"]', "filled heart = currently liked"),
        css('[aria-label="Unlike"]'),
        css('[data-testid="unlike-button"]'),
    ),
    "post_like_control": (
        role("button", "Like"),
        css('svg[aria-label="Like"]', "outline heart = not currently liked"),
        css('[aria-label="Like"]'),
        css('[data-testid="like-button"]'),
    ),
    "post_dialog_close": (
        role("button", "Close"),
        css('svg[aria-label="Close"]'),
        css('[data-testid="close-post"]'),
    ),
}


class SelectorRegistry:
    """Holds selector groups and applies user overrides.

    Override file format (``selectors.json``)::

        {
          "likes_grid_item": [
            {"kind": "css", "value": "a[href*='/p/']"}
          ],
          "select_mode_button": {
            "replace": [{"kind": "role", "value": "button", "name": "Choose"}]
          }
        }

    A bare list is *prepended* to the built-in candidates, so an override wins
    but the shipped fallbacks still apply. ``{"replace": [...]}`` discards the
    built-ins for that group.
    """

    def __init__(self, groups: Mapping[str, Sequence[Selector]] | None = None):
        source = groups if groups is not None else DEFAULT_SELECTORS
        self._groups: dict[str, tuple[Selector, ...]] = {
            key: tuple(value) for key, value in source.items()
        }
        self.overridden: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "SelectorRegistry":
        registry = cls()
        if path is None:
            return registry
        path = Path(path)
        if not path.is_file():
            log.debug("No selector override file at %s; using built-in selectors", path)
            return registry
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        registry.apply_overrides(raw, source=str(path))
        return registry

    def apply_overrides(self, raw: Mapping[str, Any], *, source: str = "override") -> None:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{source}: expected a JSON object of selector groups")
        applied: list[str] = []
        for group, value in raw.items():
            if group.startswith("_"):
                continue
            if group not in self._groups:
                raise ValueError(
                    f"{source}: unknown selector group {group!r}. Known groups: "
                    + ", ".join(sorted(self._groups))
                )
            replace_existing = False
            candidates: Iterable[Any]
            if isinstance(value, Mapping):
                replace_existing = bool(value.get("replace") is not None)
                candidates = value.get("replace") or value.get("prepend") or []
            else:
                candidates = value
            parsed = tuple(Selector.from_dict(c) for c in candidates)
            if not parsed:
                continue
            self._groups[group] = (
                parsed if replace_existing else parsed + self._groups[group]
            )
            applied.append(group)
        self.overridden = tuple(applied)
        if applied:
            log.info("Applied selector overrides from %s: %s", source, ", ".join(applied))

    # ------------------------------------------------------------------
    def __contains__(self, group: str) -> bool:
        return group in self._groups

    def __getitem__(self, group: str) -> tuple[Selector, ...]:
        return self.get(group)

    def get(self, group: str) -> tuple[Selector, ...]:
        try:
            return self._groups[group]
        except KeyError:
            raise KeyError(
                f"Unknown selector group {group!r}. Known groups: "
                + ", ".join(sorted(self._groups))
            ) from None

    def group_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._groups))

    def with_extra(self, group: str, *selectors: Selector) -> "SelectorRegistry":
        """Return a copy with additional first-priority candidates for a group."""
        groups = dict(self._groups)
        groups[group] = tuple(selectors) + groups.get(group, ())
        clone = SelectorRegistry(groups)
        clone.overridden = self.overridden
        return clone

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            group: [s.to_dict() for s in selectors]
            for group, selectors in sorted(self._groups.items())
        }

    def dump(self, path: str | Path) -> None:
        """Write the active selectors out, as a starting point for overrides."""
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def shortcode_from_url(url: str) -> str | None:
    """Extract the post shortcode from a permalink.

    ``https://www.instagram.com/p/ABC123/?img_index=1`` -> ``p/ABC123``.
    The prefix is kept so a reel and a post can never collide, and the query
    string (which can carry tracking parameters) is discarded.
    """
    if not url:
        return None
    path = url.split("://", 1)[-1]
    path = path[path.index("/") :] if "/" in path else path
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    for index, part in enumerate(parts):
        if f"/{part}/" in POST_PATH_PREFIXES and index + 1 < len(parts):
            return f"{part}/{parts[index + 1]}"
    return None
