"""Threaded HTTP server serving a miniature Instagram.

Surfaces implemented:

* ``/accounts/login/``                       — logged-out login form
* ``/``                                      — home (nav present when logged in)
* ``/your_activity/interactions/likes/``     — the likes grid, paginated
* ``/p/<code>/``                             — a post with a like/unlike control
* ``/challenge/``                            — a checkpoint screen
* ``/api/*``                                 — the mock's own JSON endpoints

Server-side state is a set of liked shortcodes, so an unlike performed through
the UI is still gone after a reload — which is what makes verification tests
meaningful.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOGIN_COOKIE = "mock_session"


@dataclass
class MockState:
    """Everything a test might want to arrange or assert on."""

    liked: list[str] = field(default_factory=list)
    logged_in: bool = False
    #: Simulated security checkpoint: every page redirects to /challenge/.
    checkpoint: bool = False
    #: Show an action-block message after this many unlikes (None = never).
    rate_limit_after: int | None = None
    #: Items rendered per page of the likes grid.
    page_size: int = 12
    #: Force the grid to render without the multi-select control.
    select_mode_enabled: bool = True
    #: Milliseconds of artificial latency before the grid renders.
    load_delay_ms: int = 0
    #: Refuse to actually unlike these (simulates a UI that ignores the click).
    sticky: set[str] = field(default_factory=set)

    unlike_calls: int = 0
    page_requests: list[str] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    state: MockState

    # Silence the default stderr access log.
    def log_message(self, *args: object) -> None:  # noqa: D102
        pass

    # -- helpers --------------------------------------------------------
    def _send(self, body: str, *, status: int = 200, content_type: str = "text/html; charset=utf-8", cookie: str | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, data: object, *, status: int = 200) -> None:
        self._send(json.dumps(data), status=status, content_type="application/json")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @property
    def _logged_in(self) -> bool:
        if self.state.logged_in:
            return True
        return LOGIN_COOKIE in (self.headers.get("Cookie") or "")

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self.state.page_requests.append(path)

        if path.startswith("/api/"):
            return self._api_get(path, parse_qs(parsed.query))

        if self.state.checkpoint and not path.startswith("/challenge"):
            return self._redirect("/challenge/")

        if path in ("/accounts/login", "/accounts/login/"):
            return self._send(_login_page())
        if path.startswith("/challenge"):
            return self._send(_checkpoint_page())

        if not self._logged_in:
            return self._redirect("/accounts/login/")

        if path in ("/", "/feed/"):
            return self._send(_home_page())
        if path.rstrip("/") == "/your_activity/interactions/likes":
            return self._send(_likes_page(self.state))
        if path.startswith("/p/") or path.startswith("/reel/"):
            code = path.strip("/").split("/", 1)[-1]
            prefix = "p" if path.startswith("/p/") else "reel"
            return self._send(_post_page(f"{prefix}/{code}", self.state))

        return self._send("<h1>Page Not Found</h1>", status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if parsed.path in ("/accounts/login", "/accounts/login/"):
            # The mock accepts any input: no real credential is ever involved,
            # and the application under test never posts to this endpoint.
            self.state.logged_in = True
            return self._redirect("/")

        if parsed.path == "/api/unlike":
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, status=400)
            return self._unlike(payload.get("ids") or [])

        return self._send("", status=404)

    # -- endpoints ------------------------------------------------------
    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/likes":
            offset = int((query.get("offset") or ["0"])[0])
            page = self.state.liked[offset : offset + self.state.page_size]
            return self._json(
                {
                    "items": [{"id": code} for code in page],
                    "next": offset + self.state.page_size
                    if offset + self.state.page_size < len(self.state.liked)
                    else None,
                }
            )
        if path == "/api/state":
            return self._json({"liked": self.state.liked, "unlike_calls": self.state.unlike_calls})
        return self._json({"error": "unknown"}, status=404)

    def _unlike(self, ids: list[str]) -> None:
        self.state.unlike_calls += 1
        limit = self.state.rate_limit_after
        if limit is not None and self.state.unlike_calls > limit:
            return self._json({"blocked": True}, status=429)
        removed = []
        for code in ids:
            if code in self.state.sticky:
                continue
            if code in self.state.liked:
                self.state.liked.remove(code)
                removed.append(code)
        return self._json({"removed": removed, "remaining": len(self.state.liked)})


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------
_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; }}
 nav {{ display:flex; gap:1rem; padding:.75rem 1rem; border-bottom:1px solid #ddd; }}
 #grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:4px; padding:1rem; }}
 #grid a {{ position:relative; display:block; background:#eee; aspect-ratio:1; }}
 #grid img {{ width:100%; height:100%; object-fit:cover; }}
 .bar {{ position:sticky; bottom:0; background:#fff; border-top:1px solid #ddd; padding:.75rem; }}
 dialog[open] {{ display:block; border:1px solid #333; padding:1rem; }}
 .spinner {{ padding:1rem; }}
</style></head><body>{body}</body></html>"""

_NAV = """
<nav role="navigation">
  <a href="/"><svg aria-label="Home" width="16" height="16"></svg></a>
  <a href="/direct/inbox/">Messages</a>
  <a href="/explore/">Explore</a>
  <a href="/your_activity/interactions/likes/">Your activity</a>
</nav>"""


def _login_page() -> str:
    body = """
    <main role="main">
      <h1>Instagram</h1>
      <form method="post" action="/accounts/login/">
        <label>Username <input name="username" autocomplete="username"></label>
        <label>Password <input name="password" type="password" autocomplete="current-password"></label>
        <button type="submit">Log in</button>
      </form>
      <p>Don't have an account? <a href="/accounts/signup/">Sign up</a></p>
    </main>"""
    return _SHELL.format(title="Login • Instagram", body=body)


def _checkpoint_page() -> str:
    body = """
    <main role="main">
      <h1>Help Us Confirm It's You</h1>
      <p>We Detected An Unusual Login Attempt</p>
      <label>Enter Security Code <input name="security_code"></label>
      <button type="submit">Confirm</button>
    </main>"""
    return _SHELL.format(title="Security check • Instagram", body=body)


def _home_page() -> str:
    return _SHELL.format(
        title="Instagram",
        body=_NAV + "<main role='main'><h1>Feed</h1></main>",
    )


def _likes_page(state: MockState) -> str:
    select_button = (
        '<button type="button" id="select-btn">Select</button>'
        if state.select_mode_enabled
        else ""
    )
    body = (
        _NAV
        + f"""
    <main role="main">
      <h1>Likes</h1>
      {select_button}
      <button type="button" id="cancel-btn" hidden>Cancel</button>
      <div id="loading" class="spinner"><svg aria-label="Loading..." width="16" height="16"></svg></div>
      <div id="grid"></div>
      <div id="empty" hidden data-testid="likes-empty">No likes yet</div>
      <div class="bar" id="bulk-bar" hidden>
        <button type="button" id="bulk-unlike">Unlike</button>
      </div>
      <dialog id="confirm" role="dialog" aria-label="Unlike posts">
        <p>Unlike these posts?</p>
        <button type="button" id="confirm-unlike">Unlike</button>
        <button type="button" id="confirm-cancel">Cancel</button>
      </dialog>
      <div id="blocked" hidden>
        <p>Please wait a few minutes before you try again</p>
      </div>
    </main>
    <script>
      const LOAD_DELAY = {state.load_delay_ms};
      let next = 0, selecting = false, done = false, loading = false;
      const grid = document.getElementById('grid');

      async function loadPage() {{
        if (done || loading) return;
        loading = true;
        document.getElementById('loading').hidden = false;
        if (LOAD_DELAY) await new Promise(r => setTimeout(r, LOAD_DELAY));
        const res = await fetch('/api/likes?offset=' + next);
        const data = await res.json();
        for (const item of data.items) {{
          const a = document.createElement('a');
          const [kind, code] = item.id.split('/');
          a.href = '/' + kind + '/' + code + '/';
          a.dataset.id = item.id;
          a.innerHTML = '<img alt="liked post" src="/static/' + code + '.jpg">' +
            '<span role="checkbox" aria-checked="false" aria-label="Select post" hidden></span>';
          a.addEventListener('click', ev => {{
            if (!selecting) return;
            ev.preventDefault();
            toggle(a);
          }});
          grid.appendChild(a);
        }}
        if (data.next === null) {{ done = true; }} else {{ next = data.next; }}
        document.getElementById('loading').hidden = true;
        document.getElementById('empty').hidden = grid.children.length > 0;
        loading = false;
      }}

      function toggle(a) {{
        const box = a.querySelector('[role=checkbox]');
        const on = box.getAttribute('aria-checked') === 'true';
        box.setAttribute('aria-checked', on ? 'false' : 'true');
        a.style.outline = on ? '' : '3px solid dodgerblue';
        document.getElementById('bulk-bar').hidden =
          document.querySelectorAll('[role=checkbox][aria-checked=true]').length === 0;
      }}

      const selectBtn = document.getElementById('select-btn');
      if (selectBtn) selectBtn.addEventListener('click', () => {{
        selecting = true;
        selectBtn.hidden = true;
        document.getElementById('cancel-btn').hidden = false;
        document.querySelectorAll('[role=checkbox]').forEach(b => b.hidden = false);
      }});
      document.getElementById('cancel-btn').addEventListener('click', () => {{
        selecting = false;
        if (selectBtn) selectBtn.hidden = false;
        document.getElementById('cancel-btn').hidden = true;
        document.getElementById('bulk-bar').hidden = true;
        document.querySelectorAll('[role=checkbox]').forEach(b => {{
          b.hidden = true; b.setAttribute('aria-checked', 'false');
        }});
        document.querySelectorAll('#grid a').forEach(a => a.style.outline = '');
      }});

      document.getElementById('bulk-unlike').addEventListener('click', () => {{
        document.getElementById('confirm').setAttribute('open', 'open');
      }});
      document.getElementById('confirm-cancel').addEventListener('click', () => {{
        document.getElementById('confirm').removeAttribute('open');
      }});
      document.getElementById('confirm-unlike').addEventListener('click', async () => {{
        document.getElementById('confirm').removeAttribute('open');
        const chosen = [...document.querySelectorAll('[role=checkbox][aria-checked=true]')]
          .map(b => b.closest('a'));
        const res = await fetch('/api/unlike', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ids: chosen.map(a => a.dataset.id)}})
        }});
        if (res.status === 429) {{
          document.getElementById('blocked').hidden = false;
          return;
        }}
        const data = await res.json();
        for (const id of data.removed) {{
          const el = grid.querySelector('[data-id="' + id + '"]');
          if (el) el.remove();
        }}
        document.getElementById('bulk-bar').hidden = true;
        document.getElementById('empty').hidden = grid.children.length > 0;
      }});

      window.addEventListener('scroll', () => {{
        if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 200) loadPage();
      }});
      loadPage();
    </script>"""
    )
    return _SHELL.format(title="Likes • Instagram", body=body)


def _post_page(identifier: str, state: MockState) -> str:
    liked = identifier in state.liked
    label = "Unlike" if liked else "Like"
    body = (
        _NAV
        + f"""
    <main role="main">
      <article>
        <img alt="post" src="/static/{identifier.split('/')[-1]}.jpg" width="200">
        <button type="button" id="like-toggle" aria-label="{label}">
          <svg aria-label="{label}" width="16" height="16"></svg>
        </button>
      </article>
      <div id="blocked" hidden><p>Please wait a few minutes before you try again</p></div>
    </main>
    <script>
      const btn = document.getElementById('like-toggle');
      btn.addEventListener('click', async () => {{
        const wasLiked = btn.getAttribute('aria-label') === 'Unlike';
        if (!wasLiked) return;
        const res = await fetch('/api/unlike', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ids: ['{identifier}']}})
        }});
        if (res.status === 429) {{
          document.getElementById('blocked').hidden = false;
          return;
        }}
        const data = await res.json();
        if (data.removed.includes('{identifier}')) {{
          btn.setAttribute('aria-label', 'Like');
          btn.querySelector('svg').setAttribute('aria-label', 'Like');
        }}
      }});
    </script>"""
    )
    return _SHELL.format(title="Post • Instagram", body=body)


# ----------------------------------------------------------------------
# Server wrapper
# ----------------------------------------------------------------------
class MockInstagram:
    """Start/stop helper used by the integration tests."""

    def __init__(
        self,
        *,
        item_count: int = 30,
        logged_in: bool = True,
        page_size: int = 12,
        rate_limit_after: int | None = None,
        checkpoint: bool = False,
        select_mode_enabled: bool = True,
        load_delay_ms: int = 0,
        sticky: set[str] | None = None,
        media_kind: str = "p",
    ):
        self.state = MockState(
            liked=[f"{media_kind}/MOCK{index:04d}" for index in range(item_count)],
            logged_in=logged_in,
            page_size=page_size,
            rate_limit_after=rate_limit_after,
            checkpoint=checkpoint,
            select_mode_enabled=select_mode_enabled,
            load_delay_ms=load_delay_ms,
            sticky=sticky or set(),
        )
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def liked(self) -> list[str]:
        return list(self.state.liked)

    def start(self) -> "MockInstagram":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "MockInstagram":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def log_out(self) -> None:
        """Simulate a session expiring mid-run."""
        self.state.logged_in = False

    def trigger_checkpoint(self) -> None:
        self.state.checkpoint = True
