"""The CLI, end to end, against the mock site.

These are the tests that matter for Phase 9: they assert that the program
cannot remove a like unless live mode was requested *and* confirmed.
"""

from __future__ import annotations

import json

import pytest

import main as cli_main
from cli.app import Application
from tests.mock_instagram import MockInstagram

pytestmark = pytest.mark.integration


@pytest.fixture
def cli(tmp_path, make_config, monkeypatch):
    """Run ``main.main(argv)`` with config pinned at a mock site and temp paths."""

    def _run(mock: MockInstagram, argv: list[str], *, answers: list[str] | None = None, **overrides):
        config = make_config(mock.base_url, **overrides)
        config_file = tmp_path / "cli-config.json"
        values = config.to_dict()
        values.pop("dry_run", None)  # --live must be the only way to disable it
        config_file.write_text(json.dumps(values))

        replies = list(answers or [])

        def fake_input(prompt: str = "") -> str:
            return replies.pop(0) if replies else ""

        monkeypatch.setattr("builtins.input", fake_input)
        return cli_main.main(["--config", str(config_file), *argv])

    return _run


def test_scan_command_reports_a_count_and_removes_nothing(cli, capsys):
    with MockInstagram(item_count=25, page_size=25) as mock:
        code = cli(mock, ["scan"])
        out = capsys.readouterr().out

        assert code == 0
        assert "Currently detected: 25" in out or "Currently detected: 25" in out
        assert "Dry run complete." in out
        assert "No likes were removed." in out
        assert mock.liked == [f"p/MOCK{i:04d}" for i in range(25)]
        assert mock.state.unlike_calls == 0


def test_run_without_live_changes_nothing(cli, capsys):
    with MockInstagram(item_count=10, page_size=10) as mock:
        code = cli(mock, ["run"])
        out = capsys.readouterr().out

        assert code == 0
        assert "Dry run: ON" in out
        assert "nothing was changed" in out
        assert len(mock.liked) == 10
        assert mock.state.unlike_calls == 0


def test_live_run_requires_a_typed_yes(cli, capsys):
    """Answering anything but "yes" must abort before any action."""
    with MockInstagram(item_count=10, page_size=10) as mock:
        code = cli(mock, ["run", "--live"], answers=["y"])  # 'y' is not enough
        out = capsys.readouterr().out

        assert code == 0
        assert "WARNING" in out
        assert "Type 'yes' in full" in out
        assert len(mock.liked) == 10
        assert mock.state.unlike_calls == 0


def test_live_run_proceeds_after_confirmation(cli, capsys):
    with MockInstagram(item_count=6, page_size=6) as mock:
        code = cli(
            mock,
            ["run", "--live", "--strategy", "item"],
            answers=["yes"],
            batch_size=3,
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "WARNING" in out
        assert mock.liked == []
        assert "Live run" in out


def test_explicit_consent_flag_skips_only_the_prompt(cli, capsys):
    with MockInstagram(item_count=4, page_size=4) as mock:
        code = cli(
            mock,
            ["run", "--live", "--yes-i-understand", "--strategy", "item"],
            answers=[],
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "WARNING" in out, "the warning is still shown"
        assert "confirmation was given on the command line" in out
        assert mock.liked == []


def test_consent_flag_alone_never_enables_live_mode(cli, capsys):
    """--yes-i-understand without --live must not remove anything."""
    with MockInstagram(item_count=4, page_size=4) as mock:
        code = cli(mock, ["run", "--yes-i-understand"])
        captured = capsys.readouterr()

        assert code == 0
        assert "only applies to a live run" in captured.err
        assert len(mock.liked) == 4
        assert mock.state.unlike_calls == 0


def test_limit_is_honoured_by_the_cli(cli):
    with MockInstagram(item_count=10, page_size=10) as mock:
        cli(
            mock,
            ["run", "--live", "--yes-i-understand", "--limit", "3", "--strategy", "item"],
        )
        assert len(mock.liked) == 7


def test_resume_continues_a_partial_run(cli, capsys):
    with MockInstagram(item_count=8, page_size=8) as mock:
        cli(
            mock,
            ["run", "--live", "--yes-i-understand", "--limit", "3", "--strategy", "item"],
            batch_size=3,
        )
        assert len(mock.liked) == 5
        capsys.readouterr()

        code = cli(
            mock,
            ["resume", "--live", "--yes-i-understand", "--strategy", "item"],
            batch_size=3,
        )

        assert code == 0
        assert mock.liked == []
        # 8 unlikes in total: the resumed run repeated nothing.
        assert mock.state.unlike_calls == 8


def test_status_reports_persisted_progress(cli, capsys):
    with MockInstagram(item_count=6, page_size=6) as mock:
        cli(
            mock,
            ["run", "--live", "--yes-i-understand", "--limit", "2", "--strategy", "item"],
        )
        capsys.readouterr()

        cli(mock, ["status"])
        out = capsys.readouterr().out

        assert "Completed:            2" in out
        assert "approximate" in out


def test_config_command_shows_no_credentials(cli, capsys):
    with MockInstagram(item_count=1) as mock:
        cli(mock, ["config"])
        out = capsys.readouterr().out.lower()

        assert "batch_size" in out
        for forbidden in ("password", "sessionid", "cookie", "token"):
            assert forbidden not in out


def test_application_scan_is_read_only(make_config):
    """Belt and braces: the Application's scan path touches nothing."""
    with MockInstagram(item_count=9, page_size=9) as mock:
        config = make_config(mock.base_url, dry_run=True)
        with Application(config, input_fn=lambda _: "") as app:
            report = app.scan()

        assert report.discovered == 9
        assert mock.state.unlike_calls == 0
        assert len(mock.liked) == 9
