"""Command line entry point.

Subcommands arrive with the phase that needs them: `n8n` in phase 1, `resolve`
in phase 4, `eval` in phase 6. Phase 0 ships the root app plus the two commands
that make a fresh checkout inspectable.
"""

from __future__ import annotations

import typer

from opsagent import __version__
from opsagent.config import get_settings
from opsagent.observability.logging import configure_logging

app = typer.Typer(
    name="opsagent",
    help="Incident triage agent for the homelab cluster.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command("show-config")
def show_config() -> None:
    """Print the effective configuration.

    Exists because a misread environment variable is otherwise invisible until
    something behaves oddly in the cluster. Secrets are typed `SecretStr`, whose
    string form is masked, so this stays safe as settings grow. Do not widen it
    to `model_dump(mode="json")`, which unwraps them.
    """
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.render_json_logs)
    for name, value in settings.model_dump().items():
        typer.echo(f"{name}={value}")
