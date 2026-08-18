"""The console script is real in phase 0 rather than a promise for later."""

import pytest
from typer.testing import CliRunner

from opsagent import __version__
from opsagent.cli import app
from opsagent.config import get_settings

pytestmark = pytest.mark.unit

runner = CliRunner()


def test_version_reports_the_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_show_config_prints_effective_settings() -> None:
    get_settings.cache_clear()

    result = runner.invoke(app, ["show-config"])

    assert result.exit_code == 0
    assert "environment=local" in result.stdout


def test_bare_invocation_shows_help_rather_than_failing_silently() -> None:
    result = runner.invoke(app, [])

    assert "Incident triage agent" in result.stdout
