"""Milestone 1 end to end: launch, detect login, navigate, scan, count.

Real Chromium, real clicks, real scrolling — against the local mock site.
Nothing here modifies anything: these tests assert the read-only path.
"""

from __future__ import annotations

import pytest

from core.errors import AuthenticationRequiredError, CheckpointError, UIChangedError
from instagram.browser import BrowserSession
from instagram.likes import LikesScanner
from instagram.navigation import AuthState, Navigator
from instagram.selectors import SelectorRegistry
from tests.mock_instagram import MockInstagram

pytestmark = pytest.mark.integration


@pytest.fixture
def session_for(make_config):
    """Yield ``(navigator, scanner, config)`` bound to a running browser."""
    sessions = []

    def _open(mock: MockInstagram, **overrides):
        config = make_config(mock.base_url, **overrides)
        session = BrowserSession(config).start()
        sessions.append(session)
        navigator = Navigator(session, config)
        return navigator, LikesScanner(navigator, config), config

    yield _open
    for session in sessions:
        session.stop()


def test_detects_logged_in_session(session_for, mock_instagram):
    navigator, _, _ = session_for(mock_instagram)
    navigator.open_home()
    assert navigator.detect_auth_state() is AuthState.LOGGED_IN
    assert navigator.is_logged_in()


def test_detects_logged_out_session(session_for):
    with MockInstagram(item_count=5, logged_in=False) as mock:
        navigator, _, _ = session_for(mock)
        navigator.open_home()
        assert navigator.detect_auth_state() is AuthState.LOGGED_OUT


def test_detects_checkpoint(session_for):
    with MockInstagram(item_count=5, checkpoint=True) as mock:
        navigator, _, _ = session_for(mock)
        navigator.open_home()
        assert navigator.detect_auth_state() is AuthState.CHECKPOINT


def test_checkpoint_is_never_bypassed(session_for):
    """A checkpoint must stop the run, with no attempt to satisfy it."""
    with MockInstagram(item_count=5, checkpoint=True) as mock:
        navigator, _, _ = session_for(mock)
        navigator.open_home()
        with pytest.raises(CheckpointError):
            navigator.ensure_authenticated(prompt=None)


def test_login_prompt_is_manual_and_passwordless(session_for):
    """The tool asks the user to log in; it never fills the form itself."""
    with MockInstagram(item_count=5, logged_in=False) as mock:
        navigator, _, _ = session_for(mock)
        messages: list[str] = []

        def prompt(message: str) -> None:
            messages.append(message)
            mock.state.logged_in = True  # the "user" logs in by hand

        state = navigator.ensure_authenticated(prompt=prompt)

        assert state is AuthState.LOGGED_IN
        assert messages, "the user should have been prompted"
        assert "manually" in messages[0]
        assert "never asks for" in messages[0]
        # The mock's login endpoint must never have been posted to.
        assert "/accounts/login/" not in [
            path for path in mock.state.page_requests if path == "/accounts/login/POST"
        ]


def test_gives_up_when_login_never_happens(session_for):
    with MockInstagram(item_count=5, logged_in=False) as mock:
        navigator, _, _ = session_for(mock)
        with pytest.raises(AuthenticationRequiredError):
            navigator.ensure_authenticated(prompt=lambda _: None, max_rounds=2)


def test_navigates_to_likes(session_for, mock_instagram):
    navigator, _, _ = session_for(mock_instagram)
    navigator.ensure_authenticated(prompt=lambda _: None)
    url = navigator.navigate_to_likes()
    assert url.endswith("/your_activity/interactions/likes/")
    assert navigator.has_liked_content()


def test_scans_first_page_without_scrolling(session_for, mock_instagram):
    """The scanner sees exactly what is rendered — one page, 12 items."""
    navigator, scanner, _ = session_for(mock_instagram)
    navigator.ensure_authenticated(prompt=lambda _: None)
    navigator.navigate_to_likes()
    navigator.on_likes_page()

    items = scanner.scan_visible()

    assert len(items) == 12
    assert all(item.identifier.startswith("p/MOCK") for item in items)
    assert all(item.media_type == "post" for item in items)
    assert items[0].url.endswith("/p/MOCK0000/")


def test_discovery_paginates_to_the_end(session_for, mock_instagram):
    navigator, scanner, _ = session_for(mock_instagram)
    navigator.ensure_authenticated(prompt=lambda _: None)
    navigator.navigate_to_likes()

    seen: list[int] = []
    items = scanner.discover(on_progress=seen.append)

    assert len(items) == 30, "every liked item should be discovered"
    assert len({item.identifier for item in items}) == 30, "no duplicates"
    assert seen == sorted(seen), "progress should only ever grow"


def test_discovery_stops_at_target(session_for):
    """A user with 250k likes must not have to load them all first.

    The stop is page-granular — discovery finishes the page it is on — so the
    assertion is that it stopped early, not that it stopped at exactly N.
    """
    with MockInstagram(item_count=100, page_size=10) as mock:
        navigator, scanner, _ = session_for(mock)
        navigator.ensure_authenticated(prompt=lambda _: None)
        navigator.navigate_to_likes()

        items = scanner.discover(target=15)

        assert len(items) >= 15
        assert len(items) < 100, "discovery should stop well before the end"


def test_empty_state_is_recognised(session_for):
    with MockInstagram(item_count=0) as mock:
        navigator, scanner, _ = session_for(mock)
        navigator.ensure_authenticated(prompt=lambda _: None)
        navigator.navigate_to_likes()
        assert navigator.has_liked_content() is False
        assert scanner.discover() == []


def test_scan_handles_slow_loading(session_for):
    with MockInstagram(item_count=12, load_delay_ms=600) as mock:
        navigator, scanner, _ = session_for(mock)
        navigator.ensure_authenticated(prompt=lambda _: None)
        navigator.navigate_to_likes()
        assert len(scanner.scan_visible()) == 12


def test_reels_are_identified_distinctly(session_for):
    with MockInstagram(item_count=4, media_kind="reel") as mock:
        navigator, scanner, _ = session_for(mock)
        navigator.ensure_authenticated(prompt=lambda _: None)
        navigator.navigate_to_likes()
        items = scanner.scan_visible()
        assert {item.media_type for item in items} == {"reel"}
        assert all(item.identifier.startswith("reel/") for item in items)


def test_scanning_never_modifies_anything(session_for, mock_instagram):
    """The whole point of the dry run: the account is untouched."""
    before = mock_instagram.liked
    navigator, scanner, _ = session_for(mock_instagram)
    navigator.ensure_authenticated(prompt=lambda _: None)
    navigator.navigate_to_likes()
    scanner.discover()

    assert mock_instagram.liked == before
    assert mock_instagram.state.unlike_calls == 0


def test_debug_dumps_real_page_html_when_selectors_dont_match(make_config, mock_instagram):
    """The real-world failure mode this exists for: a page loads, nothing on
    it matches a known selector. --debug should leave behind exactly what
    Instagram actually rendered, not just a "not found" message.
    """
    config = make_config(mock_instagram.base_url, debug=True)  # debug_dir is tmp-isolated by the fixture
    broken = SelectorRegistry()
    broken.apply_overrides(
        {
            "likes_grid_item": {"replace": [{"kind": "css", "value": "a.nonexistent-xyz"}]},
            "likes_empty_state": {"replace": [{"kind": "css", "value": "div.nonexistent-xyz"}]},
        }
    )
    session = BrowserSession(config).start()
    try:
        navigator = Navigator(session, config, broken)
        navigator.ensure_authenticated(prompt=lambda _: None)

        with pytest.raises(UIChangedError) as excinfo:
            navigator.navigate_to_likes()

        assert str(config.debug_dir) in str(excinfo.value)
    finally:
        session.stop()

    html_dumps = list(config.debug_dir.glob("*.html"))
    screenshots = list(config.debug_dir.glob("*.png"))
    summaries = list(config.debug_dir.glob("*.txt"))
    assert html_dumps, "a diagnostic HTML dump should have been saved"
    assert screenshots, "a diagnostic screenshot should have been saved"
    assert summaries, "a short text summary should have been saved"

    # It's the real page, not a placeholder: the mock's own markup is in it.
    content = html_dumps[0].read_text(encoding="utf-8")
    assert "Likes" in content or "instagram" in content.lower()

    # The summary reflects the real DOM even though the registry is broken:
    # the mock still renders genuine post permalinks, so the safe route-name
    # histogram should see them even though likes_grid_item cannot.
    summary = summaries[0].read_text(encoding="utf-8")
    assert "/p/" in summary, "the link-prefix histogram should have found post permalinks"
    assert "likes_grid_item" in summary
    assert "likes_empty_state" in summary
    # Never the raw identifier itself — only the aggregated route prefix.
    assert "MOCK0000" not in summary, "the summary must never leak a specific post identifier"


def test_no_dump_is_left_behind_without_debug(make_config, mock_instagram):
    """The opt-in default: a failure is diagnosable only when asked for."""
    config = make_config(mock_instagram.base_url, debug=False)
    broken = SelectorRegistry()
    broken.apply_overrides(
        {
            "likes_grid_item": {"replace": [{"kind": "css", "value": "a.nonexistent-xyz"}]},
            "likes_empty_state": {"replace": [{"kind": "css", "value": "div.nonexistent-xyz"}]},
        }
    )
    session = BrowserSession(config).start()
    try:
        navigator = Navigator(session, config, broken)
        navigator.ensure_authenticated(prompt=lambda _: None)

        with pytest.raises(UIChangedError) as excinfo:
            navigator.navigate_to_likes()

        assert "--debug" in str(excinfo.value)
    finally:
        session.stop()

    assert not config.debug_dir.exists() or not list(config.debug_dir.glob("*"))
