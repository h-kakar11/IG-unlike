"""Interactive prompts, including the confirmations that gate destruction.

Every prompt takes its input function as an argument so the whole confirmation
flow can be unit-tested without a terminal — the safeguards are the part of
this program that most needs tests.
"""

from __future__ import annotations

import builtins
from typing import Callable

from cli import display

InputFn = Callable[[str], str]


def _resolve(input_fn: InputFn | None) -> InputFn:
    """Late-bind the reader.

    Resolving ``builtins.input`` at call time rather than capturing it in a
    default argument keeps these prompts substitutable — which is how the
    confirmation safeguards get tested without a terminal.
    """
    return input_fn if input_fn is not None else builtins.input


def ask(question: str, *, input_fn: InputFn | None = None, default: str = "") -> str:
    input_fn = _resolve(input_fn)
    try:
        answer = input_fn(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def confirm_yes_no(
    question: str, *, input_fn: InputFn | None = None, default: bool = False
) -> bool:
    """A ``[Y/n]``-style prompt. Anything unrecognised repeats the question."""
    suffix = "[Y/n]" if default else "[y/N]"
    for _ in range(5):
        answer = ask(f"{question} {suffix} ", input_fn=input_fn).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")
    return default


def confirm_destructive(
    *,
    input_fn: InputFn | None = None,
    pending: int | None = None,
) -> bool:
    """The gate in front of the first real run.

    Requires the word "yes" in full: ``y`` is not accepted, and neither is an
    empty line. There is no flag that skips this except an explicit
    ``--yes-i-understand`` on the command line, which is itself an act of
    deliberate consent.
    """
    print()
    print(display.destructive_warning())
    if pending is not None:
        print(f"\nItems currently queued for removal: {pending:,}")
    print()
    answer = ask("Start bulk unliking? [yes/no] ", input_fn=input_fn).lower()
    if answer == "yes":
        return True
    print("Not starting. (Type 'yes' in full to confirm.)")
    return False


def confirm_resume(
    *,
    completed: int,
    failed: int,
    pending: int,
    input_fn: InputFn | None = None,
) -> bool:
    print()
    print(display.resume_block(completed, failed, pending))
    return confirm_yes_no(
        "Resume from previous session?", input_fn=input_fn, default=True
    )


def wait_for_enter(message: str, *, input_fn: InputFn | None = None) -> None:
    """Block until the user says they have finished doing something by hand."""
    input_fn = _resolve(input_fn)
    print()
    print(message)
    try:
        input_fn("")
    except (EOFError, KeyboardInterrupt):
        print()


def choose(options: list[str], *, input_fn: InputFn | None = None) -> int | None:
    """Show a numbered menu; return a 0-based index, or None to go back."""
    print(display.menu(options))
    answer = ask("Select: ", input_fn=input_fn)
    if not answer:
        return None
    try:
        index = int(answer)
    except ValueError:
        print(f"'{answer}' is not one of the options.")
        return None
    if 1 <= index <= len(options):
        return index - 1
    print(f"Choose a number between 1 and {len(options)}.")
    return None
