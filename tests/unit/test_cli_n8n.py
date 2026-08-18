"""CLI behaviour that the pipeline depends on: exit codes and secret masking."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from opsagent.cli import app
from opsagent.config import get_settings
from opsagent.n8n.sync import Manifest as WorkflowManifest
from opsagent.n8n.sync import ManifestEntry, render_workflow_file, write_manifest

pytestmark = pytest.mark.unit

runner = CliRunner()
BASE = "http://n8n.test"
WORKFLOWS = f"{BASE}/api/v1/workflows"


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a fake instance and a scratch workflow directory."""
    monkeypatch.setenv("OPSAGENT_N8N_URL", BASE)
    monkeypatch.setenv("OPSAGENT_N8N_API_KEY", "test-key")
    monkeypatch.setenv("OPSAGENT_WORKFLOWS_DIR", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path


def api_workflow(name: str, workflow_id: str = "w1") -> dict[str, object]:
    return {
        "id": workflow_id,
        "name": name,
        "nodes": [],
        "connections": {},
        "settings": {},
        "active": False,
        "updatedAt": "2026-01-02T00:00:00.000Z",
        "versionId": "abc",
    }


def test_show_config_masks_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The one command whose whole job is printing configuration must not be the
    # thing that leaks the credential into a terminal or a CI log.
    monkeypatch.setenv("OPSAGENT_N8N_API_KEY", "super-secret-value")
    get_settings.cache_clear()

    result = runner.invoke(app, ["show-config"])

    assert result.exit_code == 0
    assert "super-secret-value" not in result.stdout
    assert "n8n_api_key=**********" in result.stdout


def test_n8n_commands_fail_clearly_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSAGENT_N8N_API_KEY", raising=False)
    get_settings.cache_clear()

    result = runner.invoke(app, ["n8n", "diff"])

    assert result.exit_code != 0
    assert "OPSAGENT_N8N_API_KEY" in result.output


@respx.mock
def test_export_writes_files_and_reports_them(configured: Path) -> None:
    respx.get(WORKFLOWS).mock(
        return_value=httpx.Response(
            200, json={"data": [api_workflow("Alert triage")], "nextCursor": None}
        )
    )

    result = runner.invoke(app, ["n8n", "export"])

    assert result.exit_code == 0
    assert "1 workflow(s) exported" in result.stdout
    assert json.loads((configured / "alert-triage.json").read_text())["name"] == "Alert triage"


@respx.mock
def test_diff_exits_zero_when_in_sync(configured: Path) -> None:
    respx.get(WORKFLOWS).mock(
        return_value=httpx.Response(
            200, json={"data": [api_workflow("Alert triage")], "nextCursor": None}
        )
    )
    assert runner.invoke(app, ["n8n", "export"]).exit_code == 0

    result = runner.invoke(app, ["n8n", "diff"])

    assert result.exit_code == 0
    assert "in sync" in result.stdout


@respx.mock
def test_diff_exits_non_zero_on_drift(configured: Path) -> None:
    # This exit code is the CI gate. If it ever returns 0 on drift, the pipeline
    # silently stops enforcing that git is the source of truth.
    (configured / "only-in-git.json").write_text(
        render_workflow_file(
            {"name": "Only in git", "nodes": [], "connections": {}, "settings": {}}
        )
    )
    write_manifest(
        configured,
        WorkflowManifest(
            entries=(ManifestEntry(file="only-in-git.json", name="Only in git", active=False),)
        ),
    )
    respx.get(WORKFLOWS).mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None})
    )

    result = runner.invoke(app, ["n8n", "diff"])

    assert result.exit_code == 1
    assert "only in git:      Only in git" in result.stdout


@respx.mock
def test_import_reports_what_it_changed(configured: Path) -> None:
    (configured / "new.json").write_text(
        render_workflow_file({"name": "New", "nodes": [], "connections": {}, "settings": {}})
    )
    write_manifest(
        configured,
        WorkflowManifest(entries=(ManifestEntry(file="new.json", name="New", active=False),)),
    )
    respx.get(WORKFLOWS).mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None})
    )
    respx.post(WORKFLOWS).mock(return_value=httpx.Response(200, json=api_workflow("New", "w-new")))

    result = runner.invoke(app, ["n8n", "import"])

    assert result.exit_code == 0
    assert "created:     New" in result.stdout
