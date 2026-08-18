"""Log rendering is asserted on the configuration, not on captured output.

The bound logger is cached on first use in production, so a test that captured
stdout would be asserting on whichever test ran first.
"""

import pytest
import structlog

from opsagent.observability.logging import configure_logging

pytestmark = pytest.mark.unit


def test_json_output_installs_the_json_renderer() -> None:
    configure_logging(json_output=True)

    renderer = structlog.get_config()["processors"][-1]

    assert isinstance(renderer, structlog.processors.JSONRenderer)


def test_console_output_installs_the_console_renderer() -> None:
    configure_logging(json_output=False)

    renderer = structlog.get_config()["processors"][-1]

    assert isinstance(renderer, structlog.dev.ConsoleRenderer)


def test_level_name_is_case_insensitive() -> None:
    # Environment variables arrive as whatever a human typed.
    configure_logging("debug")

    assert structlog.get_config()["processors"]


def test_unknown_level_fails_loudly() -> None:
    with pytest.raises(KeyError):
        configure_logging("chatty")
