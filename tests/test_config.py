"""Configuration loading, precedence and validation."""

from __future__ import annotations

import json

import pytest

from config import Config, ConfigError


def test_defaults_are_conservative_and_safe():
    config = Config.load(env={})

    assert config.dry_run is True, "opening the program must never arm destruction"
    assert config.headless is False, "a human must be able to log in"
    assert config.min_delay >= 1.0
    assert config.batch_size <= 50
    assert config.max_retries >= 1


def test_environment_overrides_defaults():
    config = Config.load(env={"IGU_BATCH_SIZE": "40", "IGU_MIN_DELAY": "5"})
    assert config.batch_size == 40
    assert config.min_delay == 5.0


def test_json_file_is_read(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"batch_size": 12, "log_level": "DEBUG"}))

    config = Config.load(config_file=path, env={})

    assert config.batch_size == 12
    assert config.log_level == "DEBUG"
    assert str(path) in config.sources


def test_environment_beats_the_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"batch_size": 12}))

    config = Config.load(config_file=path, env={"IGU_BATCH_SIZE": "99"})

    assert config.batch_size == 99


def test_cli_overrides_beat_everything(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"batch_size": 12}))

    config = Config.load(
        config_file=path, env={"IGU_BATCH_SIZE": "99"}, overrides={"batch_size": 7}
    )

    assert config.batch_size == 7


def test_dotenv_is_read(tmp_path):
    (tmp_path / ".env").write_text(
        "# a comment\nIGU_BATCH_SIZE=15\nIGU_LOG_LEVEL='WARNING'\n\n"
    )

    config = Config.load(env={}, project_root=tmp_path)

    assert config.batch_size == 15
    assert config.log_level == "WARNING"


def test_relative_paths_resolve_against_the_project(tmp_path):
    config = Config.load(
        env={}, overrides={"db_path": "data/custom.db"}, project_root=tmp_path
    )
    assert config.db_path == (tmp_path / "data" / "custom.db").resolve()


def test_booleans_accept_the_usual_spellings():
    for value in ("true", "1", "yes", "on", "TRUE"):
        assert Config.load(env={"IGU_HEADLESS": value}).headless is True
    for value in ("false", "0", "no", "off"):
        assert Config.load(env={"IGU_HEADLESS": value}).headless is False


def test_a_bad_boolean_is_rejected():
    with pytest.raises(ConfigError, match="not a boolean"):
        Config.load(env={"IGU_HEADLESS": "maybe"})


def test_unknown_settings_are_rejected():
    with pytest.raises(ConfigError, match="Unknown environment setting"):
        Config.load(env={"IGU_TURBO_MODE": "1"})


def test_unknown_file_settings_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"nonsense": 1}))
    with pytest.raises(ConfigError, match="unknown settings"):
        Config.load(config_file=path, env={})


def test_malformed_json_is_reported_clearly(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        Config.load(config_file=path, env={})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"min_delay": 9, "max_delay": 2}, "max_delay"),
        ({"batch_size": 0}, "batch_size"),
        ({"max_retries": -1}, "max_retries"),
        ({"backoff_factor": 0.5}, "backoff_factor"),
        ({"backoff_initial": 60, "backoff_max": 10}, "backoff_max"),
        ({"unlike_strategy": "turbo"}, "unlike_strategy"),
        ({"log_level": "SHOUT"}, "log_level"),
        ({"max_consecutive_failures": 0}, "max_consecutive_failures"),
        ({"base_url": "instagram.com"}, "base_url"),
        ({"likes_path": "likes"}, "likes_path"),
        ({"max_scroll_stalls": 0}, "max_scroll_stalls"),
        ({"nav_timeout_ms": 10}, "nav_timeout_ms"),
    ],
)
def test_invalid_values_are_rejected(overrides, message):
    with pytest.raises(ConfigError, match=message):
        Config.load(env={}, overrides=overrides)


def test_a_reckless_delay_is_refused():
    """The tool deliberately has no high-rate mode."""
    with pytest.raises(ConfigError, match="deliberately does not support"):
        Config.load(env={}, overrides={"min_delay": 0.1, "max_delay": 0.2})


def test_all_problems_are_reported_at_once():
    with pytest.raises(ConfigError) as excinfo:
        Config.load(env={}, overrides={"batch_size": 0, "max_retries": -1})

    message = str(excinfo.value)
    assert "batch_size" in message and "max_retries" in message


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["password", "sessionid", "cookie", "access_token"])
def test_credential_like_settings_are_refused(key):
    with pytest.raises(ConfigError, match="never accepts an Instagram"):
        Config.load(env={}, overrides={key: "anything"})


def test_credential_like_env_vars_are_refused():
    with pytest.raises(ConfigError, match="never accepts an Instagram"):
        Config.load(env={"IGU_PASSWORD": "hunter2"})


def test_no_config_field_looks_like_a_credential():
    for name in Config.load(env={}).to_dict():
        assert not any(
            marker in name for marker in ("password", "token", "cookie", "secret")
        )


def test_serialisation_round_trips(tmp_path):
    config = Config.load(env={}, overrides={"batch_size": 33})
    data = config.to_dict()
    assert json.loads(json.dumps(data))["batch_size"] == 33

    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    assert Config.load(config_file=path, env={}).batch_size == 33


def test_likes_url_is_built_from_base_and_path():
    config = Config.load(env={}, overrides={"base_url": "https://example.test/"})
    assert config.likes_url == "https://example.test/your_activity/interactions/likes/"


def test_ensure_directories_creates_what_is_needed(tmp_path):
    config = Config.load(
        env={},
        overrides={
            "browser_profile_dir": tmp_path / "p",
            "db_path": tmp_path / "db" / "x.db",
            "log_path": tmp_path / "logs" / "x.log",
        },
    )
    config.ensure_directories()

    assert (tmp_path / "p").is_dir()
    assert (tmp_path / "db").is_dir()
    assert (tmp_path / "logs").is_dir()
