"""Selector registry: ordering, overrides and permalink parsing."""

from __future__ import annotations

import json

import pytest

from instagram.selectors import (
    CHECKPOINT_URL_MARKERS,
    DEFAULT_SELECTORS,
    LOGGED_OUT_URL_MARKERS,
    Selector,
    SelectorRegistry,
    shortcode_from_url,
)


def test_every_group_has_at_least_one_candidate():
    registry = SelectorRegistry()
    for group in registry.group_names():
        assert registry.get(group), f"{group} has no candidates"


def test_accessible_strategies_come_before_css_where_both_exist():
    """Role and aria-label survive a restyle; generated class names do not."""
    registry = SelectorRegistry()
    for group in ("post_unlike_control", "bulk_unlike_button", "select_mode_button"):
        kinds = [selector.kind for selector in registry.get(group)]
        assert kinds[0] in ("role", "label"), f"{group} should prefer an accessible name"


def test_no_selector_depends_on_a_generated_class_name():
    """Instagram's class names are hashed; anything matching them is a trap."""
    for group, selectors in DEFAULT_SELECTORS.items():
        for selector in selectors:
            if selector.kind != "css":
                continue
            assert not selector.value.startswith("."), (
                f"{group} uses a bare class selector: {selector.value}"
            )


def test_unknown_kinds_are_rejected():
    with pytest.raises(ValueError, match="Unknown selector kind"):
        Selector("telepathy", "value")


def test_overrides_are_prepended_by_default():
    registry = SelectorRegistry()
    original = len(registry.get("likes_grid_item"))

    registry.apply_overrides({"likes_grid_item": [{"kind": "css", "value": "a.mine"}]})

    candidates = registry.get("likes_grid_item")
    assert candidates[0].value == "a.mine"
    assert len(candidates) == original + 1, "the shipped fallbacks are kept"


def test_replace_discards_the_built_ins():
    registry = SelectorRegistry()
    registry.apply_overrides(
        {"likes_grid_item": {"replace": [{"kind": "css", "value": "a.only"}]}}
    )
    assert [s.value for s in registry.get("likes_grid_item")] == ["a.only"]


def test_overriding_an_unknown_group_is_an_error():
    with pytest.raises(ValueError, match="unknown selector group"):
        SelectorRegistry().apply_overrides({"not_a_group": [{"kind": "css", "value": "x"}]})


def test_overrides_load_from_a_file(tmp_path):
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps({"likes_grid_item": [{"kind": "css", "value": "a.file"}]}))

    registry = SelectorRegistry.load(path)

    assert registry.get("likes_grid_item")[0].value == "a.file"
    assert registry.overridden == ("likes_grid_item",)


def test_a_missing_override_file_is_fine(tmp_path):
    registry = SelectorRegistry.load(tmp_path / "nope.json")
    assert registry.get("likes_grid_item")


def test_a_malformed_override_file_is_reported(tmp_path):
    path = tmp_path / "selectors.json"
    path.write_text("{oops")
    with pytest.raises(ValueError, match="not valid JSON"):
        SelectorRegistry.load(path)


def test_dump_round_trips(tmp_path):
    path = tmp_path / "selectors.json"
    SelectorRegistry().dump(path)

    reloaded = SelectorRegistry()
    reloaded.apply_overrides(
        {k: {"replace": v} for k, v in json.loads(path.read_text()).items()}
    )

    assert reloaded.to_dict() == SelectorRegistry().to_dict()


def test_unknown_group_lookup_lists_the_valid_ones():
    with pytest.raises(KeyError, match="likes_grid_item"):
        SelectorRegistry().get("nope")


# ---------------------------------------------------------------------------
# Permalink parsing — this is how items are identified, so it must be exact
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.instagram.com/p/ABC123/", "p/ABC123"),
        ("https://www.instagram.com/p/ABC123/?img_index=1", "p/ABC123"),
        ("/p/ABC123/", "p/ABC123"),
        ("/reel/XYZ_9/", "reel/XYZ_9"),
        ("/tv/Q1/", "tv/Q1"),
        ("https://instagram.com/reel/AAA/#comments", "reel/AAA"),
        ("https://www.instagram.com/someuser/", None),
        ("https://www.instagram.com/", None),
        ("", None),
        ("/p/", None),
    ],
)
def test_shortcode_extraction(url, expected):
    assert shortcode_from_url(url) == expected


def test_posts_and_reels_with_the_same_code_do_not_collide():
    assert shortcode_from_url("/p/SAME/") != shortcode_from_url("/reel/SAME/")


def test_tracking_parameters_never_change_the_identifier():
    plain = shortcode_from_url("https://www.instagram.com/p/ABC/")
    tracked = shortcode_from_url("https://www.instagram.com/p/ABC/?utm_source=x&igsh=y")
    assert plain == tracked


def test_url_markers_are_present_for_the_states_we_must_detect():
    assert any("login" in marker for marker in LOGGED_OUT_URL_MARKERS)
    for expected in ("/challenge", "/accounts/suspended", "/two_factor"):
        assert expected in CHECKPOINT_URL_MARKERS
