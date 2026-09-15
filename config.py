"""Configuration for the Instagram bulk unliker.

Settings are resolved from, in increasing order of precedence:

1. The conservative defaults in :class:`Config`.
2. A JSON config file (``config.json`` by default, or ``$IGU_CONFIG_FILE``).
3. A ``.env`` file in the project root (simple ``KEY=VALUE`` lines).
4. Real process environment variables.
5. Explicit overrides passed by the CLI.

Every setting has an ``IGU_``-prefixed environment variable. No credential of
any kind is part of this configuration: the tool never sees, stores or asks
for an Instagram password, cookie or token. Authentication lives exclusively
in the browser profile directory, which is managed by Chromium itself.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PREFIX = "IGU_"

#: Names that must never appear in a config file or environment override.
#: They are rejected loudly so that a user who pastes credentials into
#: ``config.json`` finds out immediately instead of having them silently
#: persisted to disk.
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "username_password",
        "session_id",
        "sessionid",
        "csrftoken",
        "cookie",
        "cookies",
        "auth_token",
        "access_token",
        "bearer",
        "ds_user_id",
    }
)

VALID_STRATEGIES = ("auto", "item", "select")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed or self-contradictory."""


@dataclass
class Config:
    """Runtime configuration.

    Defaults are deliberately conservative: dry-run is on, delays are measured
    in seconds rather than milliseconds, and batches are small. The intent is
    that an accidental run does nothing destructive and touches the site
    gently.
    """

    # ---- Browser -------------------------------------------------------
    #: Persistent Chromium profile. Keeping this stable is what lets the user
    #: log in once, by hand, and stay logged in across runs.
    browser_profile_dir: Path = PROJECT_ROOT / "data" / "browser-profile"
    #: Headed by default. A human has to be able to see the browser to log in,
    #: solve a checkpoint, or notice that something has gone wrong.
    headless: bool = False
    #: Optional path to a Chromium/Chrome binary. Empty means "use the browser
    #: that ``playwright install chromium`` downloaded".
    browser_executable_path: str = ""
    #: Optional Playwright channel, e.g. "chrome" or "msedge".
    browser_channel: str = ""
    #: Artificial delay Playwright inserts between operations, in ms. Useful
    #: when watching a run; 0 in normal operation because rate control is
    #: handled by the rate controller, not here.
    slow_mo_ms: int = 0
    #: Navigation / action timeouts in milliseconds.
    nav_timeout_ms: int = 45_000
    action_timeout_ms: int = 15_000
    #: Extra Chromium command-line arguments (rarely needed; e.g. --no-sandbox
    #: inside a container).
    browser_args: tuple[str, ...] = ()

    # ---- Throughput / rate control -------------------------------------
    #: Items claimed from the database and processed before the worker pauses
    #: and reloads the likes surface.
    batch_size: int = 25
    #: Bounds of the randomised delay inserted before every action, seconds.
    min_delay: float = 3.0
    max_delay: float = 7.0
    #: Seconds to idle between batches.
    pause_after_batch: float = 90.0
    #: Per-item retry budget before an item is marked failed for good.
    max_retries: int = 3
    #: Multiplier applied to the backoff window on each consecutive failure.
    backoff_factor: float = 2.0
    #: First backoff wait, seconds, and the ceiling it may grow to.
    backoff_initial: float = 30.0
    backoff_max: float = 1800.0
    #: Consecutive failures (or rate-limit signals) tolerated before the run
    #: stops itself rather than keep hammering the site.
    max_consecutive_failures: int = 10
    max_rate_limit_hits: int = 5
    #: Optional ceiling on how many items a single session may process.
    #: 0 means "no session ceiling".
    max_items_per_session: int = 0

    # ---- Scrolling / discovery -----------------------------------------
    #: Seconds to wait after each scroll for new content to render.
    scroll_pause: float = 1.5
    #: Consecutive scrolls yielding no new items before discovery concludes
    #: the end of the list has been reached.
    max_scroll_stalls: int = 3
    #: Safety ceiling on scroll iterations per discovery pass.
    max_scrolls_per_pass: int = 200

    # ---- Storage / logging ---------------------------------------------
    db_path: Path = PROJECT_ROOT / "data" / "progress.db"
    log_path: Path = PROJECT_ROOT / "data" / "logs" / "unliker.log"
    log_level: str = "INFO"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5
    #: Optional JSON file of selector overrides, so a UI change can be fixed
    #: without editing code.
    selectors_file: Path = PROJECT_ROOT / "selectors.json"

    # ---- Safety --------------------------------------------------------
    #: THE important default. Nothing destructive happens until this is
    #: explicitly turned off *and* the user confirms at the prompt.
    dry_run: bool = True
    #: "auto" prefers Instagram's native multi-select flow and falls back to
    #: opening items one at a time; "item" and "select" force one of the two.
    unlike_strategy: str = "auto"
    #: How many items the select-mode strategy will tick before submitting.
    select_chunk_size: int = 25
    #: Rows left in 'processing' by a crash are returned to 'pending' after
    #: this many seconds.
    stale_processing_timeout: float = 900.0

    # ---- Instagram surfaces --------------------------------------------
    base_url: str = "https://www.instagram.com"
    likes_path: str = "/your_activity/interactions/likes/"

    # Populated by :meth:`load` for diagnostics; not user-settable.
    sources: tuple[str, ...] = field(default=(), repr=False)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        *,
        config_file: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        project_root: Path | None = None,
    ) -> "Config":
        """Build a validated :class:`Config` from files, environment and flags."""
        env = os.environ if env is None else env
        root = project_root or PROJECT_ROOT
        values: dict[str, Any] = {}
        sources: list[str] = ["defaults"]

        path = config_file or env.get(f"{ENV_PREFIX}CONFIG_FILE") or root / "config.json"
        path = Path(path)
        if path.is_file():
            values.update(_read_json_config(path))
            sources.append(str(path))

        dotenv = root / ".env"
        if dotenv.is_file():
            file_env = _read_dotenv(dotenv)
            values.update(_from_env(file_env))
            sources.append(str(dotenv))

        values.update(_from_env(env))
        if any(k.startswith(ENV_PREFIX) for k in env):
            sources.append("environment")

        if overrides:
            values.update({k: v for k, v in overrides.items() if v is not None})
            sources.append("cli")

        _reject_forbidden(values)
        config = cls(**_coerce(values, root=root))
        config.sources = tuple(sources)
        config.validate()
        return config

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self) -> "Config":
        """Raise :class:`ConfigError` if any setting is unusable.

        Returns ``self`` so it can be chained.
        """
        problems: list[str] = []

        if self.min_delay < 0:
            problems.append("min_delay must be >= 0")
        if self.max_delay < 0:
            problems.append("max_delay must be >= 0")
        if self.max_delay < self.min_delay:
            problems.append(
                f"max_delay ({self.max_delay}) must be >= min_delay ({self.min_delay})"
            )
        if self.batch_size < 1:
            problems.append("batch_size must be >= 1")
        if self.select_chunk_size < 1:
            problems.append("select_chunk_size must be >= 1")
        if self.pause_after_batch < 0:
            problems.append("pause_after_batch must be >= 0")
        if self.max_retries < 0:
            problems.append("max_retries must be >= 0")
        if self.backoff_factor < 1:
            problems.append("backoff_factor must be >= 1 (a factor < 1 shrinks waits)")
        if self.backoff_initial <= 0:
            problems.append("backoff_initial must be > 0")
        if self.backoff_max < self.backoff_initial:
            problems.append("backoff_max must be >= backoff_initial")
        if self.max_consecutive_failures < 1:
            problems.append("max_consecutive_failures must be >= 1")
        if self.max_rate_limit_hits < 1:
            problems.append("max_rate_limit_hits must be >= 1")
        if self.max_items_per_session < 0:
            problems.append("max_items_per_session must be >= 0 (0 disables the cap)")
        if self.scroll_pause < 0:
            problems.append("scroll_pause must be >= 0")
        if self.max_scroll_stalls < 1:
            problems.append("max_scroll_stalls must be >= 1")
        if self.max_scrolls_per_pass < 1:
            problems.append("max_scrolls_per_pass must be >= 1")
        if self.nav_timeout_ms < 1000:
            problems.append("nav_timeout_ms must be >= 1000")
        if self.action_timeout_ms < 500:
            problems.append("action_timeout_ms must be >= 500")
        if self.slow_mo_ms < 0:
            problems.append("slow_mo_ms must be >= 0")
        if self.stale_processing_timeout <= 0:
            problems.append("stale_processing_timeout must be > 0")
        if self.log_max_bytes < 1024:
            problems.append("log_max_bytes must be >= 1024")
        if self.log_backup_count < 0:
            problems.append("log_backup_count must be >= 0")
        if self.unlike_strategy not in VALID_STRATEGIES:
            problems.append(
                f"unlike_strategy must be one of {VALID_STRATEGIES}, got "
                f"{self.unlike_strategy!r}"
            )
        if self.log_level.upper() not in VALID_LOG_LEVELS:
            problems.append(
                f"log_level must be one of {VALID_LOG_LEVELS}, got {self.log_level!r}"
            )
        if not self.base_url.startswith(("http://", "https://")):
            problems.append("base_url must start with http:// or https://")
        if not self.likes_path.startswith("/"):
            problems.append("likes_path must start with '/'")

        # A tool that deletes things at speed is a tool that gets an account
        # flagged. Refuse to run faster than one action per second on average.
        if self.max_delay > 0 and (self.min_delay + self.max_delay) / 2 < 1.0:
            problems.append(
                "average delay is under 1s; raise min_delay/max_delay — this tool "
                "deliberately does not support high-rate operation"
            )

        if problems:
            raise ConfigError(
                "Invalid configuration:\n  - " + "\n  - ".join(problems)
            )
        self.log_level = self.log_level.upper()
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def ensure_directories(self) -> None:
        """Create the directories the tool writes into."""
        for path in (
            self.browser_profile_dir,
            self.db_path.parent,
            self.log_path.parent,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    @property
    def likes_url(self) -> str:
        return self.base_url.rstrip("/") + self.likes_path

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view, used by the settings screen and by logging."""
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "sources":
                continue
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            out[f.name] = value
        return out

    def describe(self) -> str:
        lines = [f"{k:<26} {v}" for k, v in self.to_dict().items()]
        lines.append(f"{'(loaded from)':<26} {', '.join(self.sources)}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------
_FIELD_TYPES = {f.name: f.type for f in fields(Config)}


def _read_json_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    unknown = set(raw) - set(_FIELD_TYPES) - FORBIDDEN_KEYS
    if unknown:
        raise ConfigError(
            f"{path} contains unknown settings: {', '.join(sorted(unknown))}"
        )
    return raw


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` file. No interpolation, no export syntax."""
    out: dict[str, str] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{line_no}: expected KEY=VALUE, got {line!r}")
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Pick up ``IGU_*`` variables and map them onto field names."""
    out: dict[str, Any] = {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].lower()
        if name == "config_file":
            continue
        if name in FORBIDDEN_KEYS:
            out[name] = value  # surfaced by _reject_forbidden
            continue
        if name not in _FIELD_TYPES or name == "sources":
            raise ConfigError(f"Unknown environment setting {key}")
        out[name] = value
    return out


def _reject_forbidden(values: Mapping[str, Any]) -> None:
    found = sorted(set(values) & FORBIDDEN_KEYS)
    if found:
        raise ConfigError(
            "Refusing to start: configuration contains credential-like keys "
            f"({', '.join(found)}). This tool never accepts an Instagram "
            "password, cookie or session token — log in through the browser "
            "window instead. Remove these keys (and rotate anything you pasted)."
        )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    raise ConfigError(f"{value!r} is not a boolean (use true/false)")


def _as_number(value: Any, cast: Callable[[Any], Any], name: str) -> Any:
    try:
        if isinstance(value, str):
            value = value.strip().replace("_", "").replace(",", "")
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: {value!r} is not a valid number") from exc


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        parts: Iterable[str] = value.split()
    else:
        parts = value
    return tuple(str(p) for p in parts if str(p).strip())


def _coerce(values: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    """Convert raw strings/JSON values into the types the dataclass expects."""
    out: dict[str, Any] = {}
    for name, value in values.items():
        if name in FORBIDDEN_KEYS or name == "sources":
            continue
        declared = _FIELD_TYPES.get(name)
        if declared is None:
            raise ConfigError(f"Unknown setting {name!r}")
        if declared == "Path":
            path = Path(str(value)).expanduser()
            out[name] = path if path.is_absolute() else (root / path).resolve()
        elif declared == "bool":
            out[name] = _as_bool(value)
        elif declared == "int":
            out[name] = _as_number(value, lambda v: int(float(v)), name)
        elif declared == "float":
            out[name] = _as_number(value, float, name)
        elif declared == "tuple[str, ...]":
            out[name] = _as_tuple(value)
        else:
            out[name] = str(value)
    return out
