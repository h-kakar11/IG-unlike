"""Phase 9: the confirmations that stand between the user and destruction.

Tested without a terminal by injecting the input function.
"""

from __future__ import annotations

import pytest

from cli import display, prompts


def answers(*values: str):
    """An input function that replays ``values`` then returns empty strings."""
    queue = list(values)

    def _input(_prompt: str = "") -> str:
        return queue.pop(0) if queue else ""

    return _input


# ---------------------------------------------------------------------------
# The destructive confirmation
# ---------------------------------------------------------------------------
def test_only_the_full_word_yes_confirms(capsys):
    assert prompts.confirm_destructive(input_fn=answers("yes")) is True
    capsys.readouterr()


@pytest.mark.parametrize("answer", ["y", "Y", "", "sure", "YES please", "ok", "1", "no"])
def test_everything_else_refuses(answer, capsys):
    assert prompts.confirm_destructive(input_fn=answers(answer)) is False
    assert "Type 'yes' in full" in capsys.readouterr().out


def test_case_is_forgiven_for_an_exact_yes(capsys):
    assert prompts.confirm_destructive(input_fn=answers("YES")) is True
    capsys.readouterr()


def test_the_warning_says_what_will_happen(capsys):
    prompts.confirm_destructive(input_fn=answers("no"))
    out = capsys.readouterr().out

    assert "WARNING" in out
    assert "remove likes from your Instagram account" in out
    assert "difficult or impossible to reverse" in out


def test_the_queue_size_is_shown_when_known(capsys):
    prompts.confirm_destructive(input_fn=answers("no"), pending=12_345)
    assert "12,345" in capsys.readouterr().out


def test_an_interrupted_prompt_refuses(capsys):
    def interrupt(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    assert prompts.confirm_destructive(input_fn=interrupt) is False


def test_a_closed_stdin_refuses(capsys):
    def eof(_prompt: str = "") -> str:
        raise EOFError

    assert prompts.confirm_destructive(input_fn=eof) is False


# ---------------------------------------------------------------------------
# Yes/no and resume prompts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "answer,default,expected",
    [
        ("y", False, True),
        ("yes", False, True),
        ("n", True, False),
        ("no", True, False),
        ("", True, True),
        ("", False, False),
    ],
)
def test_yes_no_answers(answer, default, expected):
    assert prompts.confirm_yes_no("Go?", input_fn=answers(answer), default=default) is expected


def test_unrecognised_answers_reask(capsys):
    assert prompts.confirm_yes_no("Go?", input_fn=answers("wat", "maybe", "y")) is True
    assert capsys.readouterr().out.count("Please answer y or n.") == 2


def test_resume_prompt_shows_the_saved_position(capsys):
    result = prompts.confirm_resume(
        completed=25_384, failed=23, pending=224_593, input_fn=answers("")
    )
    out = capsys.readouterr().out

    assert result is True, "resuming is the safe default"
    assert "Previous session detected." in out
    assert "Completed: 25,384" in out
    assert "Failed: 23" in out


def test_resume_can_be_declined():
    assert prompts.confirm_resume(completed=1, failed=0, pending=2, input_fn=answers("n")) is False


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
def test_menu_selection_is_zero_based():
    assert prompts.choose(["a", "b", "c"], input_fn=answers("2")) == 1


@pytest.mark.parametrize("answer", ["0", "9", "abc", ""])
def test_invalid_menu_input_returns_none(answer):
    assert prompts.choose(["a", "b"], input_fn=answers(answer)) is None


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
def test_dry_run_is_announced_clearly():
    assert "Dry run: ON" in display.mode_line(True)
    assert "LIVE MODE" in display.mode_line(False)


def test_dry_run_result_states_that_nothing_was_removed():
    text = display.dry_run_result(25)
    assert "Currently detected: 25" in text
    assert "No likes were removed." in text


def test_statistics_label_the_estimate_as_approximate():
    from core.progress import ProgressSnapshot

    text = display.statistics_block(
        ProgressSnapshot(remaining=1000, items_per_minute=42, eta_seconds=3600),
        discovered=1000,
        completed=0,
        failed=0,
        skipped=0,
    )
    assert "approximate" in text
    assert "~" in text


def test_status_block_shows_previews_in_dry_run():
    from core.progress import ProgressSnapshot

    snapshot = ProgressSnapshot(processed=5, previewed=5)
    dry = display.status_block(snapshot, dry_run=True)
    live = display.status_block(snapshot, dry_run=False)

    assert "Would unlike" in dry
    assert "Successful" not in dry
    assert "Successful" in live
