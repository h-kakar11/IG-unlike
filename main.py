#!/usr/bin/env python3
"""Instagram Unliker — bulk-remove your own Instagram likes, slowly and safely.

Run with no arguments for the interactive menu, or use a subcommand:

    python main.py scan                 # read-only: count what is there
    python main.py run                  # rehearse (dry run is the default)
    python main.py run --live           # actually remove likes (asks first)
    python main.py resume --live        # continue where the last run stopped
    python main.py status               # progress, throughput and ETA
    python main.py config               # show the resolved configuration

Safety, in short: dry run is on unless ``--live`` is passed, live mode asks for
a typed "yes", and the tool never handles your password — you log in yourself,
in the browser window it opens.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cli import display
from cli.app import Application
from config import Config, ConfigError
from core.errors import UnlikerError
from core.logging_setup import get_logger, setup_logging

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


def _common_options() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand.

    They default to ``SUPPRESS`` so that a value given before the subcommand is
    not wiped out by the subparser's own default — the usual argparse trap with
    shared options.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help="path to a JSON config file (default: config.json)",
    )
    common.add_argument(
        "--profile",
        metavar="DIR",
        default=argparse.SUPPRESS,
        help="browser profile directory to use",
    )
    common.add_argument(
        "--db", metavar="FILE", default=argparse.SUPPRESS, help="progress database path"
    )
    common.add_argument(
        "--log", metavar="FILE", default=argparse.SUPPRESS, help="log file path"
    )
    common.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=argparse.SUPPRESS,
        help="console and file log level (default: INFO)",
    )
    common.add_argument(
        "--headless",
        action="store_true",
        default=argparse.SUPPRESS,
        help="run the browser without a window (you cannot log in this way)",
    )
    common.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "when a page doesn't match any known selector, save its HTML, a "
            "screenshot and a short match-count summary under data/debug/ "
            "for inspection (local only, never uploaded; off by default "
            "because the HTML/screenshot contain personal content)"
        ),
    )
    common.add_argument(
        "--batch-size", type=int, metavar="N", default=argparse.SUPPRESS,
        help="items per batch",
    )
    common.add_argument(
        "--min-delay", type=float, metavar="SECONDS", default=argparse.SUPPRESS,
        help="shortest delay between actions",
    )
    common.add_argument(
        "--max-delay", type=float, metavar="SECONDS", default=argparse.SUPPRESS,
        help="longest delay between actions",
    )
    common.add_argument(
        "--strategy",
        choices=["auto", "item", "select"],
        default=argparse.SUPPRESS,
        help="which Instagram UI flow to use for unliking",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="instagram-unliker",
        parents=[common],
        description=(
            "Remove your own Instagram likes in bulk, with resumable progress "
            "and deliberately conservative pacing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Dry run is the default. Nothing is removed until you pass --live "
            "and confirm at the prompt."
        ),
    )

    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser(
        "scan", parents=[common], help="read-only scan of liked content"
    )
    scan.add_argument("--target", type=int, metavar="N", help="stop after finding N items")
    scan.add_argument(
        "--no-record",
        action="store_true",
        help="do not write discovered items to the database",
    )

    run = subparsers.add_parser(
        "run", parents=[common], help="process the queue (dry run unless --live)"
    )
    _add_run_flags(run)

    resume = subparsers.add_parser(
        "resume", parents=[common], help="continue the previous session"
    )
    _add_run_flags(resume)

    subparsers.add_parser(
        "status", parents=[common], help="show progress, throughput and ETA"
    )
    subparsers.add_parser(
        "config", parents=[common], help="show the resolved configuration"
    )
    dump = subparsers.add_parser(
        "dump-selectors",
        parents=[common],
        help="write the active selectors to a file for editing",
    )
    dump.add_argument(
        "--output", metavar="FILE", help="where to write (default: selectors.json)"
    )

    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually remove likes (without this, the run changes nothing)",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="process at most N items this run"
    )
    parser.add_argument(
        "--yes-i-understand",
        action="store_true",
        dest="assume_yes",
        help=(
            "skip the interactive confirmation. Only meaningful with --live, and "
            "only use it once you have seen a dry run do the right thing."
        ),
    )


def config_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Map CLI flags onto config fields. Absent flags stay absent."""
    overrides: dict[str, object] = {}
    mapping = {
        "profile": "browser_profile_dir",
        "db": "db_path",
        "log": "log_path",
        "log_level": "log_level",
        "headless": "headless",
        "debug": "debug",
        "batch_size": "batch_size",
        "min_delay": "min_delay",
        "max_delay": "max_delay",
        "strategy": "unlike_strategy",
    }
    for flag, field in mapping.items():
        value = getattr(args, flag, None)
        if value is not None and value is not False:
            overrides[field] = value
    # Dry run is only ever switched off by an explicit --live.
    if getattr(args, "live", False):
        overrides["dry_run"] = False
    return overrides


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.load(
            config_file=getattr(args, "config", None),
            overrides=config_overrides(args),
        )
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return EXIT_ERROR

    config.ensure_directories()
    setup_logging(
        config.log_path,
        level=config.log_level,
        console_level="WARNING",  # the CLI prints its own progress
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )
    log = get_logger("main")
    log.info(
        "Starting (command=%s, dry_run=%s, batch_size=%d, delay=%.1f-%.1fs)",
        args.command or "menu",
        config.dry_run,
        config.batch_size,
        config.min_delay,
        config.max_delay,
    )

    if args.command == "config":
        display.banner("Configuration")
        print(display.settings_block(config.to_dict(), sources=config.sources))
        return EXIT_OK

    if args.command == "dump-selectors":
        from instagram.selectors import SelectorRegistry

        destination = Path(args.output or config.selectors_file)
        SelectorRegistry.load(config.selectors_file).dump(destination)
        print(f"Wrote {destination}")
        print("Edit it to fix selectors without touching the code.")
        return EXIT_OK

    assume_yes = bool(getattr(args, "assume_yes", False))
    if assume_yes and not getattr(args, "live", False):
        print(
            "--yes-i-understand only applies to a live run; ignoring it.",
            file=sys.stderr,
        )
        assume_yes = False

    try:
        with Application(config, assume_yes=assume_yes) as app:
            if args.command == "scan":
                app.scan(target=args.target, record=not args.no_record)
            elif args.command == "run":
                app.start_unliking(limit=args.limit)
            elif args.command == "resume":
                app.resume(limit=args.limit)
            elif args.command == "status":
                app.show_progress()
            else:
                display.banner()
                return app.main_menu()
    except UnlikerError as exc:
        print(f"\n{type(exc).__name__}: {exc}", file=sys.stderr)
        log.error("Stopped: %s: %s", type(exc).__name__, exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; run 'resume' to continue.")
        log.warning("Interrupted by the user")
        return EXIT_INTERRUPTED
    finally:
        log.info("Shutdown complete")

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
