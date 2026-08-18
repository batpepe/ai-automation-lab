"""The offline gate. Every case here is one that would otherwise fail a deploy."""

import json
from pathlib import Path

import pytest

from opsagent.n8n.sync import Manifest as WorkflowManifest
from opsagent.n8n.sync import (
    ManifestEntry,
    render_workflow_file,
    validate_directory,
    write_manifest,
)

pytestmark = pytest.mark.unit

VALID = {"name": "Alert triage", "nodes": [], "connections": {}, "settings": {}}


def seed(directory: Path, payload: dict[str, object], *, name: str = "Alert triage") -> None:
    (directory / "alert-triage.json").write_text(render_workflow_file(payload))
    write_manifest(
        directory,
        WorkflowManifest(
            entries=(ManifestEntry(file="alert-triage.json", name=name, active=False),)
        ),
    )


def test_an_empty_directory_is_valid(tmp_path: Path) -> None:
    # The repository starts with no workflows. That is not an error.
    assert validate_directory(tmp_path) == []


def test_a_canonical_workflow_passes(tmp_path: Path) -> None:
    seed(tmp_path, VALID)

    assert validate_directory(tmp_path) == []


def test_a_read_only_field_is_rejected(tmp_path: Path) -> None:
    # additionalProperties is false in the create schema, so this is a 400 at
    # import time. Catching it on the pull request is the whole point.
    seed(tmp_path, VALID)
    contaminated = dict(VALID, versionId="abc", updatedAt="2026-01-01T00:00:00.000Z")
    (tmp_path / "alert-triage.json").write_text(json.dumps(contaminated, indent=2) + "\n")

    problems = validate_directory(tmp_path)

    assert any("canonical form" in problem for problem in problems)


def test_a_missing_required_field_is_reported_with_the_file_name(tmp_path: Path) -> None:
    seed(tmp_path, VALID)
    (tmp_path / "alert-triage.json").write_text(
        render_workflow_file({"name": "Alert triage", "nodes": []})
    )

    problems = validate_directory(tmp_path)

    assert any("missing required fields" in problem for problem in problems)
    assert all(problem.startswith("alert-triage.json") for problem in problems)


def test_malformed_json_is_reported_rather_than_raised(tmp_path: Path) -> None:
    seed(tmp_path, VALID)
    (tmp_path / "alert-triage.json").write_text("{not json")

    problems = validate_directory(tmp_path)

    assert any("not valid JSON" in problem for problem in problems)


def test_hand_reformatted_file_is_rejected(tmp_path: Path) -> None:
    # Same content, different formatting. Allowing it would mean the next export
    # produces a diff that looks like a change but is not.
    seed(tmp_path, VALID)
    (tmp_path / "alert-triage.json").write_text(json.dumps(VALID))

    problems = validate_directory(tmp_path)

    assert any("canonical form" in problem for problem in problems)


def test_manifest_and_disk_are_reconciled_in_both_directions(tmp_path: Path) -> None:
    seed(tmp_path, VALID)
    (tmp_path / "orphan.json").write_text(render_workflow_file(dict(VALID, name="Orphan")))
    write_manifest(
        tmp_path,
        WorkflowManifest(
            entries=(
                ManifestEntry(file="alert-triage.json", name="Alert triage", active=False),
                ManifestEntry(file="ghost.json", name="Ghost", active=False),
            )
        ),
    )

    problems = validate_directory(tmp_path)

    assert any("ghost.json is in the manifest but not on disk" in problem for problem in problems)
    assert any("orphan.json is on disk but not in the manifest" in problem for problem in problems)


def test_a_renamed_workflow_is_caught(tmp_path: Path) -> None:
    # Renaming in the editor changes the file name on the next export. A file
    # whose name no longer matches the manifest means someone edited one of the
    # two by hand.
    seed(tmp_path, VALID, name="Something else")

    problems = validate_directory(tmp_path)

    assert any("manifest says" in problem for problem in problems)
