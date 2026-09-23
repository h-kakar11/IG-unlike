# Development report

## Summary

All fifteen phases are implemented: browser/session handling, navigation,
dry-run scanner, unliking with verification, SQLite progress tracking, rate
control, batching, CLI, confirmation safeguards, error handling, logging,
tests, configuration, recovery and statistics.

The read-only milestone — launch, manual login, session detection, navigation
to Likes, read-only detection, and a CLI count of what was found — works and is
covered end to end by browser-driven tests. The destructive functionality is
built but gated: `dry_run` defaults to `true`, only `--live` turns it off, and
a live run additionally requires the word `yes` typed in full.

**The one thing this report cannot tell you is whether the shipped selectors
match the live Instagram.** Everything was verified against a local mock that
reproduces Instagram's *assumed* structure. The assumptions are listed below,
with a procedure for checking each one against the real site.

---

## What was built

| Phase | Status | Where |
|---|---|---|
| 1 Browser/session | Done | `instagram/browser.py` — persistent profile, headed default, launch-failure diagnosis |
| 2 Navigation | Done | `instagram/navigation.py` — auth states, Likes routing with fallbacks, pagination |
| 3 Dry-run scanner | Done | `instagram/likes.py::LikesScanner` — read-only, deduplicated |
| 4 Unliking | Done | `instagram/likes.py` — two strategies, both verified post-action |
| 5 SQLite | Done | `database/database.py` — WAL, transactional claims, UNIQUE identifiers |
| 6 Rate control | Done | `core/rate_controller.py` — randomised delays, exponential backoff, two circuit breakers |
| 7 Batching | Done | `core/worker.py` — claim → process → persist → top up |
| 8 CLI | Done | `main.py`, `cli/` — menu, subcommands, pause/resume/stop |
| 9 Safeguards | Done | `cli/prompts.py` — dry-run default, typed `yes` |
| 10 Error handling | Done | `core/errors.py` — 18 classes, each classified for retry |
| 11 Logging | Done | `core/logging_setup.py` — rotating file log, mandatory redaction |
| 12 Testing | Done | 298 tests; mock Instagram site for integration |
| 13 Configuration | Done | `config.py` — four sources, validated, credential-rejecting |
| 14 Recovery | Done | Session-aware recovery of abandoned claims |
| 15 Statistics | Done | `core/progress.py` — observed throughput, ETA |

About 5,300 lines of application code and 3,400 lines of tests and test
infrastructure. 20 selector groups covering 85 candidate strategies.

---

## What was tested, and how

```
298 tests:  255 logic-only (no browser)  +  43 browser-driven
```

| File | Tests | Covers |
|---|---|---|
| `test_config.py` | 37 | Precedence, coercion, validation, credential rejection |
| `test_safeguards.py` | 32 | Confirmation prompts, resume prompt, display |
| `test_worker.py` | 31 | Batching, retries, state transitions, pause/resume/stop |
| `test_database.py` | 27 | Claiming, outcomes, retry budgets, crash recovery |
| `test_selectors.py` | 25 | Candidate ordering, overrides, permalink parsing |
| `test_errors.py` | 24 | Classification of Playwright and unknown errors |
| `test_rate_controller.py` | 21 | Delays, backoff, caps, jitter, circuit breakers |
| `test_progress.py` | 19 | Counters, rolling window, ETA, formatting |
| `test_browser_and_dom.py` | 21 | Launch diagnosis, candidate resolution, diagnostic summary |
| `test_logging.py` | 18 | Redaction, rotation, exception scrubbing |
| `test_integration_scan.py` | 18 | **Browser**: login detection, navigation, scanning, probes |
| `test_integration_unlike.py` | 14 | **Browser**: unliking, verification, recovery |
| `test_integration_cli.py` | 11 | **Browser**: the CLI end to end |

### The mock Instagram

`tests/mock_instagram/` is a threaded HTTP server serving a miniature
Instagram: a login page with `input[name=username]`, a likes grid that
paginates on scroll, post pages whose like control flips between
`aria-label="Unlike"` and `aria-label="Like"`, the multi-select flow with its
confirmation dialog, a checkpoint screen, and 429 responses that render an
action-block message. State is server-side, so an unlike performed through the
UI is still gone after a reload — which is what makes the verification tests
meaningful rather than circular.

The integration tests run real Chromium (headless) against it. They cover:

- login detected, logged-out detected, checkpoint detected;
- the login prompt is shown and the tool never posts to the login endpoint;
- a checkpoint raises and is never answered;
- navigation to Likes, empty-state handling, slow loads, reels;
- scanning finds exactly what is rendered, deduplicated, and modifies nothing;
- pagination reaches the end, and stops early when given a target;
- a single unlike is verified against the page;
- an already-unliked item is *skipped*, not failed;
- a UI that swallows the click produces a failure, not a false success;
- rate-limit messages become `RateLimitedError` with no items removed;
- select-mode unlikes a chunk; `auto` picks it, and falls back when it is absent;
- a dry run leaves both Instagram and the queue untouched;
- a live run removes everything and records it;
- an interrupted run resumes with **no repeated work** (asserted by counting
  the mock's unlike calls: 10 items → exactly 10 calls across two runs);
- a graceful stop leaves nothing in `processing`;
- rate limiting backs off then stops safely, with progress preserved;
- a session expiring mid-run stops the run and keeps what was done;
- the CLI cannot remove anything without `--live` **and** a typed `yes`.

### What the tests cannot tell you

They validate internal consistency — that the code does the right thing given
the DOM it expects. They cannot validate that Instagram's DOM matches. That gap
is the subject of the next section.

---

## Instagram UI assumptions

Every assumption below is encoded in `instagram/selectors.py` as an ordered
list of candidates, and **none has been verified against the live site from
this environment** (no Instagram account, and automating a real one to check is
exactly what the tool is careful about). Each is a best-effort reading of
Instagram's public web interface, chosen to prefer things that change slowly.

### Routing

| Assumption | Confidence | Fallback |
|---|---|---|
| Liked content lives at `/your_activity/interactions/likes/` | Medium | Two alternative paths are tried, then the run stops with a clear error |
| Post permalinks are `/p/<code>/`, reels `/reel/<code>/`, IGTV `/tv/<code>/` | High | Thumbnail-URL hash; otherwise the item is skipped and counted |
| Query strings on permalinks are tracking only | High | Stripped before the identifier is derived |

### Authentication

| Assumption | Confidence |
|---|---|
| Logged out ⇒ URL contains `/accounts/login`, or `input[name="username"]` exists | High |
| Logged in ⇒ `svg[aria-label="Home"]`, `a[href="/direct/inbox/"]`, or a `navigation` role | Medium-high |
| Checkpoints appear at `/challenge`, `/checkpoint`, `/accounts/suspended`, `/two_factor` | Medium |
| Checkpoint text includes "Help Us Confirm It's You", "Enter Security Code" | Low — wording changes and is localised |

### Likes surface

| Assumption | Confidence | Notes |
|---|---|---|
| The grid renders anchors to post permalinks | High | The primary identification route |
| Infinite scroll, no explicit pager | Medium | A "Load more" button is tried first if present |
| An empty account shows recognisable empty-state text | Low | Wording is likely to differ |
| A "Select" control enables multi-select | Medium | `auto` falls back to per-post if absent |
| After selecting, a bulk "Unlike" appears, then a `role="dialog"` confirmation | Medium | Missing confirm control ⇒ stop, never blind-click |
| Instagram caps how many can be selected at once | Assumed | `select_chunk_size` (25) is conservative |

### Post page

| Assumption | Confidence |
|---|---|
| A liked post's control is labelled `Unlike`; unliked, `Like` | High — this is long-standing and is the accessibility contract |
| The label flips in place after the action | Medium-high — the basis of verification |
| Blocks show "Please wait a few minutes before you try again" / "Action Blocked" | Medium |

### Localisation

The text-based fallbacks are English. The browser is launched with
`locale="en-US"` so the shipped selectors line up. Role- and label-based
candidates are tried first and are also English-dependent, since accessible
names are localised too. **A non-English Instagram account will likely need
`selectors.json` overrides.** This is the single most likely cause of a failed
first run.

### How to verify before a live run

1. `python main.py scan` — if it reports a plausible number, routing and the
   grid selectors are right.
2. `python main.py run --live --limit 1` — one item, checked by hand.
3. `python main.py status` — confirm it recorded `completed`, not `failed`.
4. If step 1 fails: `python main.py scan --debug`. This saves the actual page
   HTML, a screenshot and a short match-count summary to `data/debug/` for
   each URL tried, so the fix comes from what Instagram really rendered
   rather than a guess. The summary is also printed straight to the console
   (and is small and free of personal content, unlike the HTML/screenshot),
   so it is usually enough on its own — paste it into a bug report or chat
   without needing to attach a file. Compare it against
   `instagram/selectors.py`, then `python main.py dump-selectors` and fix the
   group that no longer matches. `docs/CONFIGURATION.md` documents the format.

The error messages are built for this: when nothing matches, the exception
names every strategy that was tried and points at `selectors.json`.

### Update: the first live run hit exactly this gap

On the first real run against a live account, `navigate_to_likes` raised
`UIChangedError` — the page loaded (no login redirect, no checkpoint) but
nothing on it matched `likes_grid_item` or `likes_empty_state`. This is the
one thing this report always flagged as unverified, now confirmed to matter
in practice. Rather than guess at a fix blind, a `--debug` mode was added
(`python main.py scan --debug`) that saves the actual rendered HTML and a
screenshot for every URL tried, so the next fix can be based on what
Instagram really sent rather than assumption. It is opt-in and local-only —
see the README's "Diagnosing a mismatch" section.

A second live run confirmed the diagnosis was still out of reach: all three
known paths loaded while logged in, and none matched. It also exposed a
design fault in the tool's own response — it *told the user to run it again*
with `--debug`, spending a whole run to produce nothing. The counts-only
part of the diagnostic is now emitted unconditionally, at the moment of
failure, and the flag controls only the files that carry real content. A
structural probe sweep was added alongside it: match counts for ~30 candidate
shapes plus tag and role histograms within `<main>`, which distinguish a grid
of permalink anchors from a grid of clickable containers from a genuinely
empty page — without reading one character of the user's content. Two
container-shaped candidates (`main a[role="link"]:has(img)`,
`main div[role="button"]:has(img)`) were added to `likes_grid_item` on the
theory that Your Activity, being a multi-select surface, may not use plain
permalink anchors; the scanner now resolves a container to its nested
permalink so a tile and the anchor inside it can never be counted as two
different posts.

The HTML/screenshot dump turned out to have a practical problem of its own:
a real Instagram page's HTML is large and can carry personal content
(captions, usernames), which makes it awkward to get from wherever the tool
runs into a report or a chat message — exactly the channel this gap needs to
be closed through. A short text summary was added alongside the two dumps:
the resolved URL, the page title, a histogram of on-page link *route
prefixes* only (`/p/` -> 12, never the full permalink or who posted it), and
the live match count for every candidate in the selector groups the likes
surface depends on. It is logged at warning level — shown on the console by default, no
`--log-level` flag needed — as well as saved to `data/debug/*.txt`, so it can
be copied straight out of the terminal. The actual selector fix, once the summary or the full dump is
inspected, belongs in `selectors.json` or `instagram/selectors.py`, not in
this report.

---

## Bugs found and fixed during development

Each of these was found by a test and would have been a real defect.

**A dry run consumed the queue.** The first implementation marked previewed
items `skipped`, so rehearsing left nothing for the real run. Then, after being
changed to release items back to `pending`, the worker immediately re-claimed
them and looped forever. Fixed properly: in dry-run mode the worker *reads* a
page of pending rows (`pending_after`) and never claims at all, advancing a
cursor. A dry run now leaves the database byte-for-byte as it found it.
(`test_dry_run_terminates`, `test_dry_run_leaves_instagram_and_the_queue_untouched`)

**Discovery assumed it was still on the Likes page.** The per-post strategy
navigates away to do its work. When the queue emptied, the worker scrolled for
more content on whatever post page happened to be open, found nothing, and
declared the run finished with items still outstanding. `_discover` now checks
and re-navigates. (`test_interrupted_run_resumes_without_repeating_work`)

**A fast restart stranded the in-flight batch.** Recovery keyed only on a
900-second staleness timeout, so restarting shortly after a crash left the
claimed batch stuck in `processing` for the whole next run. Replaced with
`recover_abandoned`, which also releases rows claimed by a *different* session —
immediate recovery, with the timeout kept as a backstop.
(`test_abandoned_claims_are_recovered_immediately_on_restart`)

**Rate limiting spent the item's retry budget.** Throttling requeued the item
but still incremented `attempts`, so a few session-wide blocks could
permanently fail items that were never at fault. `release_for_retry` gained
`count_attempt=False`. (`test_rate_limiting_does_not_spend_the_items_retry_budget`)

**Playwright was being driven from the wrong thread.** The worker originally
ran on a background thread so the main thread could redraw — but Playwright's
sync API is bound to its creating thread, and every live run died with
`greenlet.error: cannot switch to a different thread`. Inverted: the worker owns
the main thread, the display and the keyboard reader are the background threads.
This is documented in `cli/runner.py` because it is easy to undo by accident.

**One hidden node discarded a whole grid.** `find_first` tested only
`locator.first` for visibility. A candidate matching thirty-six thumbnails
whose first node happened to be a placeholder — which is how a single-page
app renders, with prefetch and virtualisation nodes among the real ones —
was therefore treated as no match at all, and the page read as
unrecognisable while plainly full of content. This is a strong candidate for
the live `UIChangedError`, since the shipped `a[href*="/p/"]` would have
matched the real grid all along. Several matches per candidate are now
sampled, bounded because the check runs in a polling loop.
(`test_a_hidden_first_match_does_not_discard_the_rest_of_the_grid`)

**Confirmation prompts could not be substituted.** `input_fn: Callable = input`
captured the builtin at import time, so the safeguards were untestable and
could not be redirected. Changed to late binding via `builtins.input`.

---

## Known limitations

**Scale.** At the default pacing, roughly 8–12 items per minute including batch
pauses. 250,000 likes is therefore a multi-day operation. That is deliberate —
the alternative is acting fast enough to look abusive — but it does mean the
tool has to run for a long time, which is why resumability got the attention it
did. The `select` strategy is substantially faster than `item` because it does
not load a page per item.

**Instagram may not expose the full history.** The Likes surface has been
reported to show a bounded window rather than everything ever liked. The tool
does not assume otherwise: it processes what it can see, then scrolls for more,
and simply finishes when no more appears. If Instagram caps the window, the tool
will exhaust what is visible and stop — it cannot reach what is not served.

**No concurrency.** One worker, one browser profile, one database. Two
instances against the same database would fight over claims; the recovery logic
assumes a single worker. Chromium's own profile lock makes this hard to do by
accident.

**Headless cannot log in.** A checkpoint or a login needs a visible window. Log
in once headed against the profile directory, then headless runs work until the
session expires.

**Unverified live selectors.** Repeated above because it is the main risk.

**The ETA is an estimate.** Derived from the last 200 completions. It moves with
backoff and pauses, and is labelled approximate everywhere it appears.

**Failed items are not retried automatically across runs.** They stay `failed`
with the reason recorded; `python main.py status` offers to requeue them. This
is intentional: an item that failed three times usually needs a person to look
at it.

---

## Security review

| Requirement | How it is met |
|---|---|
| Never collect/store the password | No password field exists anywhere. The login prompt says so explicitly, and a test asserts the tool never posts to the login endpoint. |
| Never request auth cookies | Configuration *rejects* credential-shaped keys (`password`, `sessionid`, `csrftoken`, `cookie`, `access_token`…) with an explanatory error. |
| Never bypass CAPTCHA/2FA/checkpoints | Checkpoints raise `CheckpointError` and hand the browser to the user. There is no code path that fills a challenge field. |
| Never defeat rate limits | The rate controller only ever slows down or stops. No proxy rotation, no user-agent spoofing, no retry-until-through. A test asserts the launch flags spoof nothing. |
| No credential-stealing techniques | The profile directory is Chromium's; the app never reads or copies it. |
| Never upload account data | No outbound requests except the browser's own navigation. No telemetry. |
| Local-first | SQLite and a log file on disk, both gitignored. |
| Don't log secrets | A redaction filter on the handlers scrubs session ids, CSRF tokens, cookies, bearer tokens, JWT-shaped strings and long hex blobs. Tested, including exception text. URLs are logged without query strings. |

Diagnostic screenshots and HTML dumps would contain personal content, so they
are never taken automatically — only on explicit `--debug`. The one piece of
diagnostic output that *is* logged unconditionally when a dump happens (the
`.txt` summary) is deliberately built to carry none: link counts are grouped
by route prefix only, never the full href, so it cannot contain a post code,
a username or a caption.

---

## Suggested next steps

1. **Verify the selectors against a real account** using the four-step
   procedure above. Until that is done, treat the shipped selectors as a
   starting point.
2. **Run `--limit 1`, then `--limit 25`**, checking `status` after each.
3. **Consider `IGU_MIN_DELAY=8 IGU_MAX_DELAY=20`** for the first long run.
   Faster can come later; a blocked account cannot be undone.
4. **A GUI**, if wanted, should sit where `cli/runner.py` does — the worker
   already exposes callbacks and thread-safe pause/resume/stop. Mind the
   Playwright threading rule.
