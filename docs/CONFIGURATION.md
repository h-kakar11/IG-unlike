# Configuration

## Where settings come from

Later sources win:

1. built-in defaults (conservative, in `config.py`)
2. `config.json` — or the file named by `IGU_CONFIG_FILE` / `--config`
3. `.env` in the project root (`KEY=VALUE` lines)
4. `IGU_*` environment variables
5. command-line flags

`python main.py config` prints the resolved values and the sources they came
from.

Every setting has an environment variable named `IGU_` + the setting name in
upper case: `batch_size` → `IGU_BATCH_SIZE`.

## No credentials, ever

The tool refuses to start if configuration contains a key that looks like a
credential — `password`, `sessionid`, `csrftoken`, `cookie`, `access_token` and
similar. There is no setting for an Instagram password, because the application
never sees one: you log in yourself in the browser window, and Chromium keeps
the session in its profile directory exactly as it would for normal browsing.

If you ever pasted a credential into a config file, remove it and change your
password.

## Settings

### Browser

| Setting | Default | Notes |
|---|---|---|
| `browser_profile_dir` | `data/browser-profile` | Persistent Chromium profile — this is what keeps you logged in. Deleting it logs you out. |
| `headless` | `false` | Headed by default: you cannot complete a login or a checkpoint in a window you cannot see. |
| `browser_executable_path` | *(empty)* | Use a specific Chromium/Chrome binary instead of Playwright's. |
| `browser_channel` | *(empty)* | Playwright channel, e.g. `chrome`, `msedge`. |
| `browser_args` | *(empty)* | Extra Chromium flags, space-separated. `--no-sandbox` is often needed in containers. |
| `slow_mo_ms` | `0` | Artificial delay between Playwright operations. For watching a run, not for pacing — that is the rate controller's job. |
| `nav_timeout_ms` | `45000` | Page-navigation timeout. |
| `action_timeout_ms` | `15000` | Single-action timeout. |

### Pacing — the important ones

| Setting | Default | Notes |
|---|---|---|
| `min_delay` | `3.0` s | Lower bound of the randomised gap before each action. |
| `max_delay` | `7.0` s | Upper bound. The actual delay is uniform in `[min, max]`, multiplied by the current backoff. |
| `batch_size` | `25` | Items claimed and processed before a pause. |
| `pause_after_batch` | `90.0` s | Rest between batches. |
| `max_retries` | `3` | Attempts per item before it is marked `failed`. |
| `backoff_factor` | `2.0` | Multiplier applied per consecutive throttle signal. |
| `backoff_initial` | `30.0` s | First backoff wait. |
| `backoff_max` | `1800.0` s | Ceiling on any single wait. |
| `max_consecutive_failures` | `10` | Consecutive failures before the run stops itself. |
| `max_rate_limit_hits` | `5` | Rate-limit signals in one session before stopping. |
| `max_items_per_session` | `0` | Ceiling per session; `0` means no ceiling. |

Validation refuses an average delay below one second. The tool has no
high-rate mode and will not be configured into one.

At the defaults, expect roughly 8–12 items per minute including batch pauses —
about 6 hours per 5,000 likes. That is the intended trade-off.

### Discovery

| Setting | Default | Notes |
|---|---|---|
| `scroll_pause` | `1.5` s | Wait after each scroll for new content. |
| `max_scroll_stalls` | `3` | Consecutive fruitless scrolls before concluding the list has ended. |
| `max_scrolls_per_pass` | `200` | Safety ceiling per discovery pass. |

### Storage and logging

| Setting | Default | Notes |
|---|---|---|
| `db_path` | `data/progress.db` | Progress database. Delete it and you start over. |
| `log_path` | `data/logs/unliker.log` | Rotating log file. |
| `log_level` | `INFO` | `DEBUG` logs every selector resolution. |
| `log_max_bytes` | `5000000` | Rotation threshold. |
| `log_backup_count` | `5` | Rotated files kept. |
| `selectors_file` | `selectors.json` | Optional selector overrides. |
| `debug` | `false` | On a selector mismatch, also save the page's HTML and a screenshot to `debug_dir`. The counts-only diagnostic summary prints regardless of this setting; only these two files need it, because only they contain personal content. See "Diagnosing a mismatch" in the README. |
| `debug_dir` | `data/debug` | Where `--debug` dumps are written. |

### Safety and behaviour

| Setting | Default | Notes |
|---|---|---|
| `dry_run` | `true` | **The safety default.** Only `--live` turns it off. |
| `unlike_strategy` | `auto` | `auto`, `item` or `select` — see ARCHITECTURE.md. |
| `select_chunk_size` | `25` | Items ticked before submitting, in select mode. |
| `stale_processing_timeout` | `900.0` s | Backstop for recovering in-flight rows. |
| `base_url` | `https://www.instagram.com` | Also used to point tests at a mock. |
| `likes_path` | `/your_activity/interactions/likes/` | Tried first; known alternatives are tried after it. |

## Examples

### A cautious first live run

```bash
python main.py run --live --limit 100 --batch-size 10 --min-delay 8 --max-delay 20
```

### config.json

```json
{
  "batch_size": 15,
  "min_delay": 6.0,
  "max_delay": 15.0,
  "pause_after_batch": 180.0,
  "max_retries": 2,
  "log_level": "INFO"
}
```

`dry_run` is deliberately absent: leave the safety default in the file and turn
it off per-run with `--live`.

### .env

```
IGU_BROWSER_PROFILE_DIR=/home/me/.instagram-unliker-profile
IGU_DB_PATH=/home/me/instagram-progress.db
IGU_MIN_DELAY=5
IGU_MAX_DELAY=12
```

### Running in a container

```
IGU_HEADLESS=true
IGU_BROWSER_ARGS=--no-sandbox --disable-dev-shm-usage
```

Headless cannot complete an initial login. Log in once with a headed browser
against the same profile directory, then switch to headless.

## Selector overrides

When Instagram changes its interface:

```bash
python main.py dump-selectors     # writes selectors.json
```

Edit the group that broke. A bare list is **prepended** to the built-ins, so
your fix takes priority while the shipped fallbacks remain:

```json
{
  "likes_grid_item": [
    { "kind": "css", "value": "a[href*='/p/']" }
  ]
}
```

To discard the built-ins for a group entirely:

```json
{
  "select_mode_button": {
    "replace": [
      { "kind": "role", "value": "button", "name": "Choose" }
    ]
  }
}
```

Supported kinds: `role` (with `name` and optional `exact`), `label`, `text`,
`testid`, `placeholder`, `css`, `xpath`. Prefer `role` and `label`: they follow
the accessibility tree, which is far more stable than generated markup.

Run `python main.py scan` after editing — a read-only scan is the safe way to
check a selector change.
