# Architecture

## Shape of the program

```
main.py                CLI entry point: flags, subcommands, logging setup
│
├── cli/               Everything the user sees
│   ├── app.py         Wiring + the safety gates around destructive actions
│   ├── runner.py      Runs a worker with a live display and p/r/s controls
│   ├── prompts.py     Confirmations (injectable input, so they are testable)
│   └── display.py     Pure formatting
│
├── core/              Logic that knows nothing about browsers
│   ├── worker.py      The batch loop and the retry policy
│   ├── rate_controller.py  Pacing, exponential backoff, circuit breakers
│   ├── progress.py    Counters, observed throughput, ETA
│   ├── errors.py      Exception taxonomy = retry policy
│   └── logging_setup.py    Logging with mandatory credential redaction
│
├── instagram/         Everything that knows about Instagram
│   ├── browser.py     Playwright lifecycle, persistent profile
│   ├── navigation.py  Auth detection, routing to Likes, pagination
│   ├── likes.py       Read-only scanner + the two unlike strategies
│   ├── selectors.py   Every DOM assumption, as overridable data
│   └── dom.py         Candidate resolution policy
│
├── database/
│   └── database.py    SQLite: the only durable state
│
└── tests/
    ├── mock_instagram/  A local stand-in for Instagram's web UI
    └── fakes.py         In-memory Instagram layer for fast unit tests
```

The dependency direction is one-way: `cli → core → database`, and
`cli → instagram → core`. `core/` never imports `instagram/`, except for the
worker, which is the one place the two meet — and it takes its navigator,
scanner and strategy as constructor arguments, which is why it can be tested
exhaustively without a browser.

## The run loop

```
     ┌──────────────────────────────────────────────┐
     │                                              │
     ▼                                              │
  discover ──► record (INSERT OR IGNORE) ──► claim batch
     ▲                                              │
     │                                              ▼
     │                                     for each item:
     │                                       wait (rate controller)
     │                                       verify it is liked
     │                                       activate the UI control
     │                                       verify the state changed
     │                                       commit the outcome
     │                                              │
     └──────────── load more ◄──── pause ◄──────────┘
```

Each arrow that touches the database is a committed transaction. There is no
in-memory progress that matters: kill the process anywhere in that diagram and
the worst case is one item to redo.

## Key decisions

### The database is the only state
The worker holds counters for display, but every decision about what to do next
comes from SQLite. `content_identifier` is UNIQUE, so rediscovery is idempotent;
claiming is `UPDATE ... WHERE status='pending'` inside a transaction, so two
claims cannot overlap. WAL journalling keeps the stats screen readable while a
run is in progress and makes an abrupt kill recoverable.

### The exception type *is* the retry policy
`core/errors.py` answers three questions for every error: retryable, fatal,
needs-a-human. The worker branches on those three flags, never on message text.
Adding a new failure mode means adding a class, not editing the loop.

### Identity comes from the permalink
An item is identified by its shortcode (`p/ABC123`, `reel/XYZ`), parsed from the
`href` with the query string discarded. Positional identity would be wrong the
moment an item is removed and the grid reflows — and a wrong identifier means
unliking the wrong post. Where no permalink exists, the thumbnail's CDN path is
hashed instead; where neither exists, the item is skipped and counted rather
than guessed at.

### Verification is never optional
`SinglePostStrategy` confirms the post is currently liked before clicking, then
polls until the control reports "Like" — a click that produces no state change
raises `VerificationFailedError` rather than being recorded as success.
`SelectModeStrategy` verifies by the item leaving the grid. This is why an
Instagram UI that silently swallows clicks shows up as retries and then
failures, instead of as a database full of false "completed" rows.

### Rate control exists to slow things down
`RateController` has no mechanism for finding a maximum safe rate. It spaces
actions apart with randomised delays, rests between batches, multiplies its
delays on every throttle signal, and raises `CircuitBreakerTripped` after a
configured number of consecutive failures or rate-limit hits. Its sleep and
randomness are injected, so the policy is unit-tested without waiting.

### Selectors are data, not code
Instagram's class names are generated and change without notice, so nothing
depends on them. Each target is a list of strategies tried in order — role and
accessible name, then ARIA label, then URL shape, then visible text — and users
can prepend or replace any group from `selectors.json`. When no candidate
matches, the code raises with the full list of what it tried. It never falls
back to coordinates.

### Playwright stays on the thread that created it
The synchronous Playwright API is bound to its creating thread, so the worker
runs on the main thread and the *display* runs on a background thread, not the
other way round. The console control thread only calls `request_pause`,
`request_resume` and `request_stop`, which are `threading.Event`-based.

### Two unlike strategies
`select` uses Instagram's own multi-select flow: a few clicks per chunk instead
of a page load per item, which is much lighter on the site and far faster at
scale. `item` opens each post and toggles its like control, which is slower but
verifiable post-by-post and works wherever a post can be opened. `auto` (the
default) prefers `select` when the page offers it and falls back to `item`.

### Dry run is structural, not a flag check at the end
In dry-run mode the worker reads the queue instead of claiming it, and never
constructs an unlike strategy at all. The strategies additionally refuse to act
if `dry_run` is set, so a bug in the worker cannot turn a rehearsal into a real
run.

## Data model

```sql
items(
  id, content_identifier UNIQUE, content_url, media_type,
  status CHECK(pending|processing|completed|failed|skipped),
  attempts, first_seen, last_attempt, completed_at,
  error, error_code, session_id
)
sessions(id, started_at, ended_at, dry_run, discovered,
         completed, failed, skipped, stop_reason, config_digest)
events(id, session_id, created_at, kind, detail)
meta(key, value)          -- schema_version
```

### State transitions

```
                   claim
   pending ──────────────────► processing
      ▲                            │
      │  release_for_retry         ├─► completed   (verified)
      │  (attempts < budget)       ├─► skipped     (nothing to do)
      └────────────────────────────┤
                                   └─► failed      (budget exhausted,
   recover_abandoned ◄──────────────    or non-retryable)
   (different session, or stale)
```

`recover_abandoned` is what makes a fast restart work: a row claimed by an
earlier session is released immediately, without waiting for the staleness
timeout. The timeout remains as a backstop.

## Extending it

- **A GUI**: `Worker` already reports through `on_progress` / `on_event`
  callbacks and is controlled through three thread-safe methods. A GUI would
  replace `cli/runner.py` and reuse everything below it. Note the Playwright
  threading constraint: the worker must own the thread that created the browser.
- **A new unlike flow**: subclass `UnlikeStrategy`, implement `unlike` (and
  optionally `unlike_many` and `available`), and add it to `build_strategy`.
- **A different surface** (saved posts, comments): add selector groups, a
  scanner and a strategy. The worker, database, rate controller and CLI are
  surface-agnostic.
