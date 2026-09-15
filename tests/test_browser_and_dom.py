"""Browser lifecycle helpers and DOM candidate resolution.

No real browser here: these cover the pure logic around it — how launch
failures are explained, and the order in which selector candidates are tried.
"""

from __future__ import annotations

import pytest

from core.errors import BrowserCrashedError, ElementNotFoundError, FatalError
from instagram import dom
from instagram.browser import BASE_ARGS, BrowserSession, clear_profile
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
    def __init__(self, count: int = 1, visible: bool = True, label: str = ""):
        self._count = count
        self._visible = visible
        self.label = label

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._visible

    @property
    def first(self) -> "FakeLocator":
        return self

    def nth(self, _index: int) -> "FakeLocator":
        return self


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
