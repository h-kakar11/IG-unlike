"""Shared fixtures.

The browser-driven tests run headless against the local mock site. They are
skipped automatically when Playwright or a Chromium build is unavailable, so
the pure-logic suite still runs anywhere.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from config import Config
from core.logging_setup import setup_logging
from tests.mock_instagram import MockInstagram


def _find_chromium() -> str:
    """Locate a Chromium for Playwright to drive.

    Honours ``IGU_BROWSER_EXECUTABLE_PATH`` first, then the usual Playwright
    browser cache, then a system install.
    """
    explicit = os.environ.get("IGU_BROWSER_EXECUTABLE_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    roots = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")),
        Path.home() / ".cache" / "ms-playwright",
        Path("/opt/pw-browsers"),
    ]
    for root in roots:
        if not root or not root.exists():
            continue
        for candidate in sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True):
            return str(candidate)
        for candidate in sorted(root.glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"), reverse=True):
            return str(candidate)

    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


@pytest.fixture(scope="session")
def chromium_path() -> str:
    pytest.importorskip("playwright", reason="Playwright is not installed")
    path = _find_chromium()
    if not path:
        pytest.skip("No Chromium available; run 'playwright install chromium'")
    return path


@pytest.fixture
def mock_instagram():
    """A fresh mock Instagram, logged in, with 30 liked posts."""
    with MockInstagram(item_count=30, page_size=12) as server:
        yield server


@pytest.fixture
def make_config(tmp_path, chromium_path):
    """Build a Config wired to a temp profile/db/log and the mock site."""

    def _make(base_url: str, **overrides):
        values = {
            "browser_profile_dir": tmp_path / "profile",
            "db_path": tmp_path / "progress.db",
            "log_path": tmp_path / "logs" / "test.log",
            "browser_executable_path": chromium_path,
            "headless": True,
            "base_url": base_url,
            "browser_args": ("--no-sandbox", "--disable-dev-shm-usage"),
            # Tests must not actually wait out the production pacing.
            "min_delay": 0.0,
            "max_delay": 0.0,
            "pause_after_batch": 0.0,
            "backoff_initial": 0.01,
            "backoff_max": 0.05,
            "scroll_pause": 0.2,
            "nav_timeout_ms": 20_000,
            "action_timeout_ms": 8_000,
        }
        values.update(overrides)
        config = Config(**values)
        # The average-delay floor exists to stop real runs hammering Instagram;
        # against a local mock there is nothing to protect.
        config.validate = lambda: config  # type: ignore[method-assign]
        config.ensure_directories()
        return config

    return _make


@pytest.fixture(autouse=True)
def quiet_logging(tmp_path):
    setup_logging(tmp_path / "logs" / "unliker.log", level="DEBUG", console=False)
