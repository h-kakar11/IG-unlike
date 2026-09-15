"""A small, local stand-in for Instagram's web UI.

It exists so the browser-driving code can be tested end to end — real
Playwright, real Chromium, real clicks — without touching a real account.
The markup mirrors the *assumptions* the selectors encode (accessible names,
aria-labels, permalink shapes), so a test failure here means our assumptions
are internally inconsistent; it cannot tell us whether they match the real
Instagram, which only a manual run against the live site can.
"""

from tests.mock_instagram.server import MockInstagram

__all__ = ["MockInstagram"]
