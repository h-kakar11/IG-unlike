# Instagram Unliker

A local, resumable tool for removing your own Instagram likes in bulk — built
for accounts with hundreds of thousands of them, where a run may last days and
will certainly be interrupted at some point.

It drives the ordinary Instagram web interface with
[Playwright](https://playwright.dev/python/), the same way you would with a
mouse, and records every outcome in SQLite so that a crash, a reboot or an
expired session costs you the item in flight and nothing more.

**Dry run is the default.** Nothing is removed until you explicitly ask for a
live run *and* type `yes` at the confirmation prompt.

---

## What it does and does not do

**It does:**

- open a real Chromium window with a persistent profile, so you log in once;
- detect whether you are logged in, logged out, or facing a security checkpoint;
- navigate to *Your activity → Interactions → Likes*;
- scan what is there, read-only, and tell you how much it found;
- remove likes through the normal UI controls, verifying each one afterwards;
- pace itself deliberately, back off when Instagram pushes back, and stop
  rather than keep hammering the site;
- resume exactly where it stopped, without repeating work.

**It deliberately does not:**

- ask for, store, or transmit your password — you log in yourself, in the
  browser window it opens;
- ask you to paste cookies, session identifiers or tokens;
- attempt to bypass CAPTCHA, 2FA, checkpoints or any other security control —
  it stops and hands the browser to you;
- try to defeat, evade or probe Instagram's rate limits;
- send anything anywhere. Everything stays on your machine.

Removing likes is not reversible by this tool. Instagram does not offer a
"re-like everything" button, and the scan does not archive the posts it finds.
Treat a live run as permanent.

---

## Requirements

- Python 3.11 or newer
- A desktop session (the browser must be visible for you to log in)
- ~100 MB of disk for Chromium, plus the browser profile

## Setup

```bash
git clone <this repository>
cd IG-unlike

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

---

## Running the dry-run scanner

This is where to start, and it cannot remove anything:

```bash
python main.py scan
```

What happens:

1. A Chromium window opens on Instagram.
2. If you are not logged in, the tool prints:

   ```
   Instagram login required.
   Please log in manually in the browser window that just opened.
   This application never asks for, sees or stores your password.
   Press ENTER when ready...
   ```

   Log in **in that window**, then press ENTER in the terminal.
3. It navigates to your Likes page, scrolls to load content, and reports:

   ```
   Instagram Unliker
   Scanning liked content...
   Currently detected: 25
   Dry run complete.
   No likes were removed.
   ```

Everything it found is recorded in `data/progress.db` as `pending`, ready for a
later run. Scan as often as you like; rediscovering an item never duplicates it
and never re-opens work that already finished.

To scan only the first N items instead of the whole history:

```bash
python main.py scan --target 500
```

### Rehearsing the full run

`python main.py run` (with no `--live`) walks the entire batching loop —
claiming, pacing, pausing, reporting — while touching nothing. It is the
closest thing to a live run that changes nothing, and the queue is left intact
afterwards.

---

## Actually removing likes

Once a dry run looks right:

```bash
python main.py run --live
```

You will get:

```
WARNING
This will remove likes from your Instagram account.
This action may be difficult or impossible to reverse.
Removed likes cannot be restored by this tool.

Items currently queued for removal: 12,482

Start bulk unliking? [yes/no]
```

Only the full word `yes` proceeds. `y`, an empty line and anything else abort.

While running:

```
Instagram Unliker
────────────────────────────────
Processed:        12,482
Successful:       12,431
Failed:           51
Skipped:          0
Session progress: 1,000
Total recorded:   12,482
Status:           Running

Commands:  p = pause   r = resume   s = stop (graceful)   Ctrl-C = stop
```

Stopping — by `s`, by Ctrl-C, or by closing the laptop lid — is safe. The
current item finishes, in-flight rows return to the queue, and the database is
committed.

Useful flags:

```bash
python main.py run --live --limit 500     # a cautious first live batch
python main.py run --live --strategy item # force the per-post flow
python main.py resume --live              # continue the previous session
python main.py status                     # progress, throughput and ETA
```

---

## Resuming

Every run picks up where the last one left off; there is no "start from zero".

```bash
python main.py resume --live
```

```
Previous session detected.
Completed: 25,384
Failed: 23
Pending: 224,593
Resume from previous session? [Y/n]
```

This works after a normal stop, a crash, a browser crash, a power cut, an
expired Instagram session or a period of rate limiting. Items that were in
flight when the process died are detected on the next start and returned to the
queue automatically.

---

## Progress and estimates

```bash
python main.py status
```

```
Total discovered:     250,000
Completed:            37,421
Failed:               23
Skipped:              0
Remaining:            ~212,579
Current session:      1,000
Average per item:     1.4s
Current throughput:   42 items/min
Estimated remaining:  ~3d 12h 21m
```

The throughput and the estimate are measured from recent completions, not from
the configured delays, so they already include page loads, retries and pauses.
They are still approximate and will move as conditions change.

---

## Interactive menu

Running `python main.py` with no subcommand gives the menu:

```
Instagram Unliker
────────────────────────────────
1. Scan likes (read-only)
2. Start unliking
3. Resume previous job
4. View progress
5. Settings
6. Exit
Select:
```

Option 2 still requires a live-mode configuration and the typed confirmation,
so opening the program can never start removing things.

---

## Configuration

Conservative defaults are built in; everything is overridable through
`config.json`, a `.env` file, `IGU_*` environment variables or CLI flags. See
**[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** for the full list.

The settings you are most likely to touch:

| Setting | Default | What it does |
|---|---|---|
| `batch_size` | 25 | Items processed before a pause |
| `min_delay` / `max_delay` | 3.0 / 7.0 s | Randomised gap between actions |
| `pause_after_batch` | 90 s | Rest between batches |
| `max_retries` | 3 | Attempts before an item is marked failed |
| `dry_run` | `true` | The safety default |

To go gentler still:

```bash
IGU_MIN_DELAY=8 IGU_MAX_DELAY=20 IGU_BATCH_SIZE=10 python main.py run --live
```

The tool refuses to be configured faster than an average of one action per
second. There is no high-rate mode, by design.

---

## When Instagram changes its interface

Every selector lives in `instagram/selectors.py`, each target expressed as a
list of strategies tried in order (accessible role and name first, then ARIA
labels, then URL shape, then visible text). You can override any of them
without touching the code:

```bash
python main.py dump-selectors      # writes selectors.json
# edit selectors.json
python main.py scan                # picks the overrides up automatically
```

If nothing matches, the tool stops with a message naming every strategy it
tried, rather than clicking on a page it cannot read.

### Diagnosing a mismatch

When a page matches nothing the tool knows, it prints a diagnostic summary
straight to your terminal — no flag needed, and no second run required:

```
--- Diagnostic summary: /your_activity/interactions/likes/ ---
URL: https://www.instagram.com/your_activity/interactions/likes/
Title: 'Likes'
Link prefixes on this page (route only, no post/user data):
  /p/                  36
Selector group match counts (0 means every candidate failed):
  likes_grid_item:
    css='a[href*="/p/"]'                        -> 0
Page structure (element counts only, no content):
  <main> present: True   elements within it: 812
  Tags: div=604, img=36, a=12, span=98
  Probe matches (-1 = selector unsupported here):
    main div[role="button"]:has(img)            -> 36
```

That is counts only — no captions, usernames or post codes — so it is safe
to paste into a bug report. It is normally enough to identify the selector
that needs fixing: in the example above, the tiles are clickable `div`s
rather than the permalink anchors the tool expected.

For the full picture, add `--debug` to also save the real page HTML and a
screenshot under `data/debug/`:

```bash
python main.py scan --debug
```

Those two stay behind the flag because they *do* contain personal content.
Everything written is local and gitignored, and nothing is ever uploaded.
Once you know what to match, fix the group in `selectors.json` — see
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the pieces fit together
- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — every setting
- **[docs/DEVELOPMENT_REPORT.md](docs/DEVELOPMENT_REPORT.md)** — what was built,
  what was tested, and which Instagram UI assumptions remain unverified

---

## Tests

```bash
pip install -r requirements.txt
playwright install chromium
python -m pytest              # everything
python -m pytest -m "not integration"   # logic only, no browser
```

The integration tests drive a real headless Chromium against a local mock
Instagram (`tests/mock_instagram/`) that reproduces the login page, the likes
grid with infinite scroll, the multi-select flow, post pages, checkpoints and
rate-limit responses. No test ever touches a real account.

---

## Where your data lives

| Path | Contents |
|---|---|
| `data/browser-profile/` | Chromium profile, including your Instagram session |
| `data/progress.db` | SQLite progress database |
| `data/logs/unliker.log` | Rotating log, with credentials redacted |

All of it is local and gitignored. Deleting `data/browser-profile/` logs you
out; deleting `data/progress.db` loses your progress.

---

## Safety notes

- Run one instance at a time. A single browser profile cannot be opened twice,
  and the progress database assumes one worker.
- Start with `--limit` on your first live run and check the result.
- If Instagram shows a checkpoint, resolve it yourself in the browser window.
  The tool will wait, and will never try to answer it for you.
- Automating an account carries some risk to that account. Conservative pacing
  reduces it; nothing eliminates it. Removing 250,000 likes at the default
  settings is a multi-day operation, and that is intentional.
