"""Browser lifecycle helpers and DOM candidate resolution.

No real browser here: these cover the pure logic around it — how launch
failures are explained, and the order in which selector candidates are tried.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import BrowserCrashedError, ElementNotFoundError, FatalError
from instagram import dom
from instagram.browser import BASE_ARGS, BrowserSession, clear_profile
from instagram.navigation import Navigator
from instagram.selectors import Selector, SelectorRegistry
from tests.fakes import fast_config


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
def test_launch_failures_are_explained(tmp_path):
    session = BrowserSession(fast_config(tmp_path))

    missing = session._launch_error(Exception("Executable doesn't exist at /x"))
    assert isinstance(missing, FatalError)
    assert "playwright install chromium" in str(missing)

    locked = session._launch_error(Exception("ProcessSingleton: profile in use"))
    assert "already" in str(locked) and "in use" in str(locked)

    headless = session._launch_error(Exception("Missing X server or $DISPLAY"))
    assert "headless" in str(headless)

    other = session._launch_error(Exception("kaboom"))
    assert "Could not launch the browser" in str(other)


def test_using_a_stopped_browser_raises_clearly(tmp_path):
    session = BrowserSession(fast_config(tmp_path))
    with pytest.raises(BrowserCrashedError):
        _ = session.page
    with pytest.raises(BrowserCrashedError):
        _ = session.context
    assert session.is_alive() is False


def test_launch_arguments_do_not_spoof_anything():
    """We automate the browser; we do not disguise it."""
    joined = " ".join(BASE_ARGS).lower()
    for forbidden in ("user-agent", "incognito", "proxy", "disable-web-security"):
        assert forbidden not in joined


def test_clearing_a_profile_is_explicit(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_text("not a real cookie jar")

    assert clear_profile(profile) is True
    assert not profile.exists()
    assert clear_profile(profile) is False


def test_current_url_is_safe_when_there_is_no_browser(tmp_path):
    assert BrowserSession(fast_config(tmp_path)).current_url() == ""


# ---------------------------------------------------------------------------
# DOM resolution
# ---------------------------------------------------------------------------
class FakeLocator:
    def __init__(
        self,
        count: int = 1,
        visible: bool = True,
        label: str = "",
        visible_at: set[int] | None = None,
    ):
        self._count = count
        self._visible = visible
        self.label = label
        #: Per-index visibility, for grids whose first node is a placeholder.
        self._visible_at = visible_at

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._visible

    @property
    def first(self) -> "FakeLocator":
        return self.nth(0)

    def nth(self, index: int) -> "FakeLocator":
        if self._visible_at is None:
            return self
        return FakeLocator(
            count=self._count,
            visible=index in self._visible_at,
            label=f"{self.label}[{index}]",
        )


class FakeScope:
    """Maps each selector spelling to a canned locator."""

    def __init__(self, mapping: dict[str, FakeLocator]):
        self.mapping = mapping
        self.asked: list[str] = []

    def _get(self, key: str) -> FakeLocator:
        self.asked.append(key)
        return self.mapping.get(key, FakeLocator(count=0))

    def locator(self, value: str) -> FakeLocator:
        return self._get(f"css:{value}")

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        return self._get(f"role:{role}:{name}")

    def get_by_text(self, value: str, exact: bool = False):
        return self._get(f"text:{value}")

    def get_by_label(self, value: str, exact: bool = False):
        return self._get(f"label:{value}")

    def get_by_placeholder(self, value: str, exact: bool = False):
        return self._get(f"placeholder:{value}")

    def get_by_test_id(self, value: str):
        return self._get(f"testid:{value}")


def test_candidates_are_tried_in_order():
    scope = FakeScope({"css:b": FakeLocator(label="second")})
    selectors = [Selector("css", "a"), Selector("css", "b"), Selector("css", "c")]

    locator, matched = dom.find_first(scope, selectors)

    assert matched.value == "b"
    assert scope.asked[:2] == ["css:a", "css:b"]


def test_a_hidden_early_match_does_not_mask_a_visible_later_one():
    scope = FakeScope(
        {
            "css:a": FakeLocator(count=1, visible=False),
            "css:b": FakeLocator(count=1, visible=True),
        }
    )
    _, matched = dom.find_first(scope, [Selector("css", "a"), Selector("css", "b")])
    assert matched.value == "b"


def test_a_hidden_first_match_does_not_discard_the_rest_of_the_grid():
    """The failure this exists for: a full grid reading as "nothing here".

    Instagram renders placeholder and prefetch nodes among the real ones. If
    only the first match were tested for visibility, thirty-six thumbnails
    behind one hidden node would count as no match at all.
    """
    scope = FakeScope({"css:a": FakeLocator(count=36, visible_at={1, 2, 3})})

    match = dom.find_first(scope, [Selector("css", "a")])

    assert match is not None, "a visible thumbnail behind a hidden one still counts"
    assert match[1].value == "a"


def test_visibility_sampling_is_bounded():
    """This runs in a polling loop, so it must not walk a huge grid."""
    probed: list[int] = []

    class CountingLocator(FakeLocator):
        def nth(self, index: int) -> "FakeLocator":
            probed.append(index)
            return FakeLocator(count=self._count, visible=False)

    scope = FakeScope({"css:a": CountingLocator(count=5000, visible=False)})
    assert dom.find_first(scope, [Selector("css", "a")]) is None
    assert len(probed) == dom.VISIBILITY_SAMPLE


def test_a_hidden_match_is_still_usable_when_visibility_is_not_required():
    scope = FakeScope({"css:a": FakeLocator(count=1, visible=False)})
    assert dom.find_first(scope, [Selector("css", "a")]) is None
    assert dom.find_first(scope, [Selector("css", "a")], require_visible=False) is not None


def test_resolve_explains_what_it_tried():
    registry = SelectorRegistry()
    scope = FakeScope({})

    with pytest.raises(ElementNotFoundError) as excinfo:
        dom.resolve(scope, registry, "post_unlike_control", what="the unlike button")

    message = str(excinfo.value)
    assert "the unlike button" in message
    assert "role=button" in message, "the message should name the strategies tried"
    assert "selectors.json" in message, "and how to fix it"


def test_try_resolve_and_is_present_do_not_raise():
    scope = FakeScope({})
    registry = SelectorRegistry()
    assert dom.try_resolve(scope, registry, "post_unlike_control") is None
    assert dom.is_present(scope, registry, "post_unlike_control") is False


def test_all_locators_collects_across_candidates():
    scope = FakeScope({"css:a": FakeLocator(count=2), "css:b": FakeLocator(count=3)})
    found = dom.all_locators(scope, [Selector("css", "a"), Selector("css", "b")])
    assert len(found) == 5


def test_role_selectors_pass_the_accessible_name_through():
    scope = FakeScope({"role:button:Unlike": FakeLocator()})
    locator = dom.to_locator(scope, Selector("role", "button", name="Unlike"))
    assert locator.count() == 1
    assert scope.asked == ["role:button:Unlike"]


def test_unsupported_kinds_are_rejected():
    """``to_locator`` is the last line of defence if a kind slips past validation."""
    bogus = Selector("css", "x")
    object.__setattr__(bogus, "kind", "telepathy")

    with pytest.raises(ValueError, match="Unsupported selector kind"):
        dom.to_locator(FakeScope({}), bogus)


# ---------------------------------------------------------------------------
# Diagnostic dumps (--debug)
# ---------------------------------------------------------------------------
class _RecordingSession:
    """Stands in for BrowserSession: records dump/screenshot calls, no Playwright.

    Deliberately has no ``.page`` — the summary's title and selector-count
    sections must degrade gracefully rather than blow up when page access
    fails, which is exactly the situation a half-crashed browser produces.
    """

    def __init__(self, url: str = "https://instagram.test/your_activity/interactions/likes/"):
        self.html_calls: list[Path] = []
        self.screenshot_calls: list[Path] = []
        self._url = url
        self.histogram: list[tuple[str, int]] = [("/p/", 3), ("/explore/", 1)]

    def dump_html(self, path):
        self.html_calls.append(Path(path))
        return Path(path)

    def screenshot(self, path, *, full_page: bool = False):
        self.screenshot_calls.append(Path(path))
        return Path(path)

    def current_url(self) -> str:
        return self._url

    def link_prefix_histogram(self):
        return self.histogram

    def structure_report(self, probes):
        return {
            "main_present": True,
            "scope_elements": 42,
            "tags": {"div": 30, "img": 7, "a": 5},
            "roles": {"button": 4, "link": 5},
            "probes": {selector: 0 for selector in probes},
        }


def test_diagnostics_are_not_dumped_unless_debug_is_enabled(tmp_path):
    """The default: a mismatch fails quietly, with no personal data written."""
    session = _RecordingSession()
    config = fast_config(tmp_path, debug=False)

    Navigator(session, config)._dump_diagnostics("your_activity/interactions/likes/")

    assert session.html_calls == []
    assert session.screenshot_calls == []


def test_debug_dumps_html_and_a_screenshot_for_the_failed_page(tmp_path):
    session = _RecordingSession()
    debug_dir = tmp_path / "dbg"
    config = fast_config(tmp_path, debug=True, debug_dir=debug_dir)

    Navigator(session, config)._dump_diagnostics("your_activity/interactions/likes/")

    assert len(session.html_calls) == 1
    assert len(session.screenshot_calls) == 1
    html_path, png_path = session.html_calls[0], session.screenshot_calls[0]
    assert html_path.suffix == ".html"
    assert png_path.suffix == ".png"
    assert html_path.parent == debug_dir
    assert "likes" in html_path.name


def test_debug_dump_filenames_are_filesystem_safe():
    """Labels come from URL paths, which contain '/'; that must never break a save."""
    session = _RecordingSession()
    config = fast_config(Path("/tmp"), debug=True, debug_dir=Path("/tmp/dbg"))

    Navigator(session, config)._dump_diagnostics("/your_activity/interactions/likes/")

    name = session.html_calls[0].name
    assert "/" not in name
    assert "\\" not in name


def test_debug_writes_a_short_text_summary_alongside_the_dumps(tmp_path):
    """The .txt summary is what makes a dump shareable without opening a file."""
    session = _RecordingSession()
    debug_dir = tmp_path / "dbg"
    config = fast_config(tmp_path, debug=True, debug_dir=debug_dir)

    Navigator(session, config)._dump_diagnostics("your_activity/interactions/likes/")

    txt_files = list(debug_dir.glob("*.txt"))
    assert len(txt_files) == 1
    content = txt_files[0].read_text(encoding="utf-8")
    assert "URL: https://instagram.test/your_activity/interactions/likes/" in content
    assert "/p/" in content and "3" in content, "the link-prefix histogram should be included"
    assert "likes_grid_item" in content, "selector group match counts should be included"


def test_summary_degrades_gracefully_without_a_page(tmp_path):
    """A half-crashed browser (page gone, session still around) must not stop the dump."""
    session = _RecordingSession()
    debug_dir = tmp_path / "dbg"
    config = fast_config(tmp_path, debug=True, debug_dir=debug_dir)

    # Must not raise, even though _RecordingSession has no .page:
    Navigator(session, config)._dump_diagnostics("your_activity/interactions/likes/")

    content = list(debug_dir.glob("*.txt"))[0].read_text(encoding="utf-8")
    assert "unavailable" in content
    assert len(session.html_calls) == 1 and len(session.screenshot_calls) == 1


def test_the_safe_summary_is_reported_even_without_debug(tmp_path, caplog):
    """A user who hits a mismatch must not be told to run the whole thing again.

    The counts-only summary carries nothing personal, so it is always logged;
    only the HTML and screenshot wait for --debug.
    """
    session = _RecordingSession()
    config = fast_config(tmp_path, debug=False, debug_dir=tmp_path / "dbg")

    with caplog.at_level("WARNING", logger="unliker.navigation"):
        summary = Navigator(session, config).report_diagnostics("likes")

    assert "Page structure" in summary
    assert summary in caplog.text, "the summary should reach the console"
    assert session.html_calls == [] and session.screenshot_calls == []
    assert not (tmp_path / "dbg").exists(), "no files without --debug"


def test_the_summary_describes_page_shape_without_content(tmp_path):
    session = _RecordingSession()
    config = fast_config(tmp_path, debug=False)

    summary = Navigator(session, config).report_diagnostics("likes")

    assert "<main> present: True" in summary
    assert "div=30" in summary and "img=7" in summary, "tag histogram"
    assert "Probe matches" in summary
    assert 'main div[role="button"]:has(img)' in summary


def test_summary_omits_link_prefixes_when_none_are_found(tmp_path):
    session = _RecordingSession()
    session.histogram = []
    debug_dir = tmp_path / "dbg"
    config = fast_config(tmp_path, debug=True, debug_dir=debug_dir)

    Navigator(session, config)._dump_diagnostics("your_activity/interactions/likes/")

    content = list(debug_dir.glob("*.txt"))[0].read_text(encoding="utf-8")
    assert "none found, or could not be read" in content
