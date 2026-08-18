"""Command line entry point.

Subcommands arrive with the phase that needs them: `resolve` in phase 4, `eval`
in phase 6. Phase 1 adds `n8n`, which is how a workflow gets from the editor
into a reviewable pull request and back into the instance.
"""

from __future__ import annotations

import asyncio

import typer

from opsagent import __version__
from opsagent.config import Settings, get_settings
from opsagent.n8n.client import N8nClient
from opsagent.n8n.sync import (
    ExportResult,
    ImportResult,
    WorkflowDiff,
    diff_workflows,
    export_workflows,
    import_workflows,
    validate_directory,
)
from opsagent.observability.logging import configure_logging

app = typer.Typer(
    name="opsagent",
    help="Incident triage agent for the homelab cluster.",
    no_args_is_help=True,
    add_completion=False,
)

n8n_app = typer.Typer(
    help="Manage n8n workflows as code: export, review, import.",
    no_args_is_help=True,
)
app.add_typer(n8n_app, name="n8n")


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


def _client(settings: Settings) -> N8nClient:
    if settings.n8n_api_key is None:
        raise typer.BadParameter(
            "OPSAGENT_N8N_API_KEY is not set. Create an API key in the n8n UI "
            "under Settings, then provide it through the sealed secret."
        )
    return N8nClient(settings.n8n_url, settings.n8n_api_key.get_secret_value())


async def _export(settings: Settings) -> ExportResult:
    async with _client(settings) as client:
        return await export_workflows(client, settings.workflows_dir)


async def _diff(settings: Settings) -> WorkflowDiff:
    async with _client(settings) as client:
        return await diff_workflows(client, settings.workflows_dir)


async def _import(settings: Settings) -> ImportResult:
    async with _client(settings) as client:
        return await import_workflows(client, settings.workflows_dir)


@n8n_app.command("export")
def n8n_export() -> None:
    """Write the instance's workflows into the repository.

    Run this after editing in the browser. The result is a reviewable diff.
    """
    settings = get_settings()
    result = asyncio.run(_export(settings))

    for file_name in sorted(result.written):
        typer.echo(f"wrote {settings.workflows_dir / file_name}")
    typer.echo(f"{len(result.written)} workflow(s) exported")


@n8n_app.command("validate")
def n8n_validate() -> None:
    """Check the committed workflows without contacting an instance.

    Needs no API key and no cluster, which is what lets it run on a pull
    request. Exits non-zero when something would be rejected at import time.
    """
    settings = get_settings()
    problems = validate_directory(settings.workflows_dir)

    for problem in problems:
        typer.echo(problem)
    if problems:
        raise typer.Exit(code=1)
    typer.echo("workflows are valid")


@n8n_app.command("diff")
def n8n_diff() -> None:
    """Compare the instance against the repository, changing neither.

    Exits non-zero when they differ, which is what makes it usable as a CI gate
    and as the proof that a round trip was clean.
    """
    settings = get_settings()
    difference = asyncio.run(_diff(settings))

    for name in difference.changed:
        typer.echo(f"changed:          {name}")
    for name in difference.only_in_git:
        typer.echo(f"only in git:      {name}")
    for name in difference.only_in_instance:
        typer.echo(f"only in instance: {name}")

    if difference.is_empty:
        typer.echo("in sync")
        return
    raise typer.Exit(code=1)


@n8n_app.command("import")
def n8n_import() -> None:
    """Push the repository to the instance and reconcile activation state.

    Workflows that exist only in the instance are left alone. Deleting them is a
    larger promise than this pipeline makes, and `diff` already reports them.
    """
    settings = get_settings()
    result = asyncio.run(_import(settings))

    for name in result.created:
        typer.echo(f"created:     {name}")
    for name in result.updated:
        typer.echo(f"updated:     {name}")
    for name in result.activated:
        typer.echo(f"activated:   {name}")
    for name in result.deactivated:
        typer.echo(f"deactivated: {name}")
    for name in result.unchanged:
        typer.echo(f"unchanged:   {name}")
