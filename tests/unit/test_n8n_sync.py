"""Export, diff and import, driven through the real client against faked HTTP.

The round trip is the claim this repository makes about n8n, so it is tested end
to end rather than per function: export what the instance holds, and prove the
diff is empty afterwards.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from opsagent.n8n.client import N8nClient
from opsagent.n8n.sync import (
    MANIFEST_NAME,
    ManifestEntry,
    SyncError,
    diff_workflows,
    export_workflows,
    import_workflows,
    load_manifest,
    read_workflow_file,
    render_workflow_file,
    slugify,
    to_committed_payload,
    write_manifest,
)
from opsagent.n8n.sync import Manifest as WorkflowManifest

pytestmark = pytest.mark.unit

BASE = "http://n8n.test"
WORKFLOWS = f"{BASE}/api/v1/workflows"


def api_workflow(
    name: str, workflow_id: str = "w1", *, active: bool = False, **extra: object
) -> dict[str, object]:
    """A workflow as the API returns it, read-only fields included."""
    return {
        "id": workflow_id,
        "name": name,
        "nodes": [{"name": "Start", "type": "n8n-nodes-base.start"}],
        "connections": {},
        "settings": {"executionOrder": "v1"},
        "active": active,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-02T00:00:00.000Z",
        "versionId": "abc",
        "triggerCount": 0,
        "isArchived": False,
        "tags": [{"id": "t1", "name": "ops"}],
        **extra,
    }


def page(*workflows: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(workflows), "nextCursor": None})


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Alert triage", "alert-triage"),
        ("  Spaced  Out  ", "spaced-out"),
        ("CVE triage (nightly)", "cve-triage-nightly"),
        ("already-slugged", "already-slugged"),
    ],
)
def test_slugify_produces_stable_file_stems(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_slugify_rejects_a_name_with_nothing_to_slug() -> None:
    with pytest.raises(SyncError, match="empty file name"):
        slugify("!!!")


def test_committed_payload_drops_read_only_fields() -> None:
    # These are exactly the fields the create schema marks readOnly. Sending
    # one back is a 400 because additionalProperties is false.
    payload = to_committed_payload(api_workflow("Alert triage"))

    assert set(payload) == {"name", "nodes", "connections", "settings"}
    for dropped in ("id", "active", "createdAt", "updatedAt", "versionId", "tags"):
        assert dropped not in payload


def test_committed_payload_rejects_a_workflow_missing_required_fields() -> None:
    # Failing here names the workflow. Failing at import time gives a 400 with
    # far less context.
    incomplete = {"name": "Broken", "nodes": []}

    with pytest.raises(SyncError, match="missing required fields"):
        to_committed_payload(incomplete)


def test_rendering_is_deterministic_regardless_of_key_order() -> None:
    first = render_workflow_file({"name": "a", "nodes": [], "connections": {}, "settings": {}})
    second = render_workflow_file({"settings": {}, "connections": {}, "nodes": [], "name": "a"})

    assert first == second
    assert first.endswith("\n")


def test_manifest_round_trips_through_yaml(tmp_path: Path) -> None:
    manifest = WorkflowManifest(
        entries=(
            ManifestEntry(file="b.json", name="B", active=True, id="w2"),
            ManifestEntry(file="a.json", name="A", active=False),
        )
    )

    write_manifest(tmp_path, manifest)
    loaded = load_manifest(tmp_path)

    # Sorted by file, so the manifest does not churn between exports.
    assert [entry.file for entry in loaded.entries] == ["a.json", "b.json"]
    assert loaded.by_file()["a.json"].id is None
    assert loaded.by_file()["b.json"].id == "w2"


def test_missing_manifest_reads_as_an_empty_repository(tmp_path: Path) -> None:
    assert load_manifest(tmp_path).entries == ()


def test_a_file_that_is_not_a_manifest_is_rejected(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text("just: a mapping\n")

    with pytest.raises(SyncError, match="not a workflow manifest"):
        load_manifest(tmp_path)


@pytest.mark.asyncio
@respx.mock
async def test_export_writes_files_and_manifest(tmp_path: Path) -> None:
    respx.get(WORKFLOWS).mock(
        return_value=page(
            api_workflow("Alert triage", "w1", active=True),
            api_workflow("Daily digest", "w2"),
        )
    )

    async with N8nClient(BASE, "key") as client:
        result = await export_workflows(client, tmp_path)

    assert sorted(result.written) == ["alert-triage.json", "daily-digest.json"]

    written = json.loads((tmp_path / "alert-triage.json").read_text())
    assert "updatedAt" not in written
    assert written["name"] == "Alert triage"

    manifest = load_manifest(tmp_path)
    assert manifest.by_file()["alert-triage.json"].active is True
    assert manifest.by_file()["alert-triage.json"].id == "w1"


@pytest.mark.asyncio
@respx.mock
async def test_export_refuses_to_silently_overwrite_a_colliding_slug(tmp_path: Path) -> None:
    # "Alert triage" and "Alert-Triage" both slug to alert-triage. Writing one
    # over the other would lose a workflow with no error at all.
    respx.get(WORKFLOWS).mock(
        return_value=page(api_workflow("Alert triage", "w1"), api_workflow("Alert-Triage", "w2"))
    )

    async with N8nClient(BASE, "key") as client:
        with pytest.raises(SyncError, match="both map to"):
            await export_workflows(client, tmp_path)


@pytest.mark.asyncio
@respx.mock
async def test_export_then_diff_reports_no_drift(tmp_path: Path) -> None:
    # The round trip in one test: what was exported must compare equal to what
    # the instance holds, or the committed form is lossy.
    respx.get(WORKFLOWS).mock(return_value=page(api_workflow("Alert triage", "w1", active=True)))

    async with N8nClient(BASE, "key") as client:
        await export_workflows(client, tmp_path)
        difference = await diff_workflows(client, tmp_path)

    assert difference.is_empty


@pytest.mark.asyncio
@respx.mock
async def test_diff_reports_content_drift(tmp_path: Path) -> None:
    respx.get(WORKFLOWS).mock(return_value=page(api_workflow("Alert triage", "w1")))
    async with N8nClient(BASE, "key") as client:
        await export_workflows(client, tmp_path)

    edited = json.loads((tmp_path / "alert-triage.json").read_text())
    edited["settings"] = {"executionOrder": "v0"}
    (tmp_path / "alert-triage.json").write_text(render_workflow_file(edited))

    async with N8nClient(BASE, "key") as client:
        difference = await diff_workflows(client, tmp_path)

    assert difference.changed == ("Alert triage",)


@pytest.mark.asyncio
@respx.mock
async def test_diff_reports_activation_drift(tmp_path: Path) -> None:
    respx.get(WORKFLOWS).mock(return_value=page(api_workflow("Alert triage", "w1", active=False)))
    async with N8nClient(BASE, "key") as client:
        await export_workflows(client, tmp_path)

    manifest = load_manifest(tmp_path)
    write_manifest(
        tmp_path,
        WorkflowManifest(
            entries=(
                ManifestEntry(file="alert-triage.json", name="Alert triage", active=True, id="w1"),
            )
        ),
    )
    assert manifest.entries  # the export produced something to diverge from

    async with N8nClient(BASE, "key") as client:
        difference = await diff_workflows(client, tmp_path)

    assert difference.changed == ("Alert triage",)


@pytest.mark.asyncio
@respx.mock
async def test_diff_separates_git_only_from_instance_only(tmp_path: Path) -> None:
    (tmp_path / "in-git.json").write_text(
        render_workflow_file({"name": "In git", "nodes": [], "connections": {}, "settings": {}})
    )
    write_manifest(
        tmp_path,
        WorkflowManifest(entries=(ManifestEntry(file="in-git.json", name="In git", active=False),)),
    )
    respx.get(WORKFLOWS).mock(return_value=page(api_workflow("Only live", "w9")))

    async with N8nClient(BASE, "key") as client:
        difference = await diff_workflows(client, tmp_path)

    assert difference.only_in_git == ("In git",)
    assert difference.only_in_instance == ("Only live",)
    assert difference.changed == ()


@pytest.mark.asyncio
@respx.mock
async def test_import_creates_a_workflow_that_has_no_id_yet(tmp_path: Path) -> None:
    (tmp_path / "new.json").write_text(
        render_workflow_file({"name": "New", "nodes": [], "connections": {}, "settings": {}})
    )
    write_manifest(
        tmp_path,
        WorkflowManifest(entries=(ManifestEntry(file="new.json", name="New", active=False),)),
    )
    respx.get(WORKFLOWS).mock(return_value=page())
    create = respx.post(WORKFLOWS).mock(
        return_value=httpx.Response(200, json=api_workflow("New", "w-new"))
    )

    async with N8nClient(BASE, "key") as client:
        result = await import_workflows(client, tmp_path)

    assert create.called
    assert result.created == ("New",)
    # The assigned id is written back, so the next run updates instead of
    # creating a duplicate.
    assert load_manifest(tmp_path).by_file()["new.json"].id == "w-new"


@pytest.mark.asyncio
@respx.mock
async def test_import_updates_and_activates_when_git_says_active(tmp_path: Path) -> None:
    (tmp_path / "triage.json").write_text(
        render_workflow_file(
            {"name": "Triage", "nodes": [], "connections": {}, "settings": {"executionOrder": "v1"}}
        )
    )
    write_manifest(
        tmp_path,
        WorkflowManifest(
            entries=(ManifestEntry(file="triage.json", name="Triage", active=True, id="w1"),)
        ),
    )
    respx.get(WORKFLOWS).mock(
        return_value=page(api_workflow("Triage", "w1", active=False, settings={"old": True}))
    )
    update = respx.put(f"{WORKFLOWS}/w1").mock(
        return_value=httpx.Response(200, json=api_workflow("Triage", "w1"))
    )
    activate = respx.post(f"{WORKFLOWS}/w1/activate").mock(
        return_value=httpx.Response(200, json=api_workflow("Triage", "w1", active=True))
    )

    async with N8nClient(BASE, "key") as client:
        result = await import_workflows(client, tmp_path)

    assert update.called
    assert activate.called
    assert result.updated == ("Triage",)
    assert result.activated == ("Triage",)


@pytest.mark.asyncio
@respx.mock
async def test_import_is_idempotent_when_nothing_changed(tmp_path: Path) -> None:
    # Running the pipeline twice must not rewrite the instance the second time,
    # or every ArgoCD sync would churn workflow versions.
    respx.get(WORKFLOWS).mock(return_value=page(api_workflow("Steady", "w1")))
    async with N8nClient(BASE, "key") as client:
        await export_workflows(client, tmp_path)

    update = respx.put(f"{WORKFLOWS}/w1").mock(return_value=httpx.Response(200, json={}))
    create = respx.post(WORKFLOWS).mock(return_value=httpx.Response(200, json={}))

    async with N8nClient(BASE, "key") as client:
        result = await import_workflows(client, tmp_path)

    assert result.unchanged == ("Steady",)
    assert not update.called
    assert not create.called


@pytest.mark.asyncio
@respx.mock
async def test_import_leaves_instance_only_workflows_alone(tmp_path: Path) -> None:
    # Deleting what is not in git is a much bigger promise than this pipeline
    # makes. diff already reports the extras.
    write_manifest(tmp_path, WorkflowManifest())
    respx.get(WORKFLOWS).mock(return_value=page(api_workflow("Handmade", "w9")))
    delete = respx.delete(f"{WORKFLOWS}/w9").mock(return_value=httpx.Response(200, json={}))

    async with N8nClient(BASE, "key") as client:
        result = await import_workflows(client, tmp_path)

    assert not delete.called
    assert result == type(result)()


def test_manifest_pointing_at_a_missing_file_fails_clearly(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        WorkflowManifest(entries=(ManifestEntry(file="gone.json", name="Gone", active=False),)),
    )

    with pytest.raises(SyncError, match="does not exist"):
        read_workflow_file(tmp_path, "gone.json")
