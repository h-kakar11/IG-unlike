"""Terminal rendering. Pure formatting — no I/O side effects beyond printing.

Kept free of worker/database imports so every function here is trivially
unit-testable with plain data.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable, Mapping, Sequence

from core.progress import ProgressSnapshot, format_count, format_duration, format_rate

TITLE = "Instagram Unliker"
RULE = "─" * 32


def header(subtitle: str = "") -> str:
    lines = [TITLE, RULE]
    if subtitle:
        lines.append(subtitle)
    return "\n".join(lines)


def banner(subtitle: str = "") -> None:
    print(header(subtitle))


def rule() -> None:
    print(RULE)


def mode_line(dry_run: bool) -> str:
    return "Dry run: ON  (nothing will be removed)" if dry_run else "Dry run: OFF — LIVE MODE"


def status_block(
    snapshot: ProgressSnapshot,
    *,
    dry_run: bool = False,
    status: str | None = None,
) -> str:
    """The live run display."""
    rows: list[tuple[str, str]] = [
        ("Processed", format_count(snapshot.processed)),
    ]
    if dry_run:
        rows.append(("Would unlike", format_count(snapshot.previewed)))
    else:
        rows.extend(
            [
                ("Successful", format_count(snapshot.successful)),
                ("Failed", format_count(snapshot.failed)),
                ("Skipped", format_count(snapshot.skipped)),
            ]
        )
    rows.extend(
        [
            ("Session progress", format_count(snapshot.session_processed)),
            ("Total recorded", format_count(snapshot.total_recorded)),
            ("Status", (status or snapshot.status).capitalize()),
        ]
    )
    width = max(len(label) for label, _ in rows) + 1
    body = "\n".join(f"{label + ':':<{width}} {value}" for label, value in rows)
    return f"{header()}\n{body}"


def statistics_block(
    snapshot: ProgressSnapshot,
    *,
    discovered: int,
    completed: int,
    failed: int,
    skipped: int,
) -> str:
    """The 'View progress' screen (Phase 15).

    Throughput and ETA come from observed completions, so both are labelled
    approximate: the true figure moves with backoff, pauses and page latency.
    """
    lines = [
        f"Total discovered:     {format_count(discovered)}",
        f"Completed:            {format_count(completed)}",
        f"Failed:               {format_count(failed)}",
        f"Skipped:              {format_count(skipped)}",
        f"Remaining:            ~{format_count(snapshot.remaining)}",
        f"Current session:      {format_count(snapshot.session_processed)}",
        f"Average per item:     {_average(snapshot.average_seconds)}",
        f"Current throughput:   {format_rate(snapshot.items_per_minute)}",
        f"Estimated remaining:  ~{format_duration(snapshot.eta_seconds)}",
        "",
        "Throughput and the estimate are approximate: both are measured from",
        "recent completions and will change with pauses, retries and backoff.",
    ]
    return "\n".join(lines)


def _average(seconds: float | None) -> str:
    if not seconds:
        return "measuring..."
    return f"{seconds:.1f}s"


def dry_run_result(detected: int, extra: Mapping[str, Any] | None = None) -> str:
    lines = [f"Currently detected: {format_count(detected)}"]
    if extra:
        by_type = extra.get("by_media_type") or {}
        for media_type, count in by_type.items():
            lines.append(f"  {media_type:<10} {format_count(count)}")
    lines.append("Dry run complete.")
    lines.append("No likes were removed.")
    return "\n".join(lines)


def destructive_warning() -> str:
    return (
        "WARNING\n"
        "This will remove likes from your Instagram account.\n"
        "This action may be difficult or impossible to reverse.\n"
        "Removed likes cannot be restored by this tool."
    )


def resume_block(completed: int, failed: int, pending: int) -> str:
    return (
        "Previous session detected.\n"
        f"Completed: {format_count(completed)}\n"
        f"Failed: {format_count(failed)}\n"
        f"Pending: {format_count(pending)}"
    )


def settings_block(values: Mapping[str, Any], *, sources: Sequence[str] = ()) -> str:
    width = max((len(k) for k in values), default=10) + 2
    lines = [f"{key:<{width}} {value}" for key, value in values.items()]
    if sources:
        lines.append("")
        lines.append(f"Loaded from: {', '.join(sources)}")
    return "\n".join(lines)


def menu(options: Sequence[str]) -> str:
    lines = [header()]
    lines += [f"{index}. {label}" for index, label in enumerate(options, 1)]
    return "\n".join(lines)


def table(rows: Iterable[Sequence[Any]], headers: Sequence[str]) -> str:
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  ".join("-" * width for width in widths))
    out += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join(out)


def clear_screen() -> None:
    """Redraw in place when attached to a terminal; stay quiet in a pipe."""
    if sys.stdout.isatty():
        print("\033[H\033[J", end="")


def notice(message: str) -> None:
    print(f"\n{message}\n")
