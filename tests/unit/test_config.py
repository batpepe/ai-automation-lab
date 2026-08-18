"""Configuration behaviour, including the failure modes worth guaranteeing."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from opsagent.config import Environment, Settings, get_settings

pytestmark = pytest.mark.unit


def test_defaults_need_no_environment() -> None:
    # The headline claim of the repository is that it runs with nothing
    # configured. If this test ever needs a fixture, that claim has broken.
    settings = Settings()

    assert settings.environment is Environment.LOCAL
    assert settings.log_level == "INFO"
    assert settings.log_json is None


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSAGENT_ENVIRONMENT", "cluster")
    monkeypatch.setenv("OPSAGENT_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.environment is Environment.CLUSTER
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("environment", "log_json", "expected"),
    [
        (Environment.LOCAL, None, False),
        (Environment.CLUSTER, None, True),
        # An explicit setting wins over the environment in both directions.
        (Environment.LOCAL, True, True),
        (Environment.CLUSTER, False, False),
    ],
)
def test_render_json_logs_resolves_the_tri_state(
    environment: Environment, log_json: bool | None, expected: bool
) -> None:
    settings = Settings(environment=environment, log_json=log_json)

    assert settings.render_json_logs is expected


def test_unknown_prefixed_variable_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo in OPSAGENT_LOG_LEVL must not silently leave the default in place.
    # pydantic-settings will not catch this on its own: extra="forbid" never
    # sees the variable, because the environment source only offers names that
    # already match a field. Hence the explicit check in Settings.
    monkeypatch.setenv("OPSAGENT_LOG_LEVL", "DEBUG")

    with pytest.raises(ValidationError, match="OPSAGENT_LOG_LEVL"):
        Settings()


def test_unrelated_dotenv_keys_are_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real .env also holds credentials for n8n, the database and the provider.
    # The boundary is "unknown OPSAGENT_ setting", not "unknown key", so those
    # entries have to pass through untouched.
    (tmp_path / ".env").write_text("N8N_API_KEY=not-a-real-key\nOPSAGENT_LOG_LEVEL=WARNING\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.log_level == "WARNING"


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()
