"""ArgoCD deploy history and runbook lookup."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsagent.tools.argocd import get_recent_deploys
from opsagent.tools.models import DeploysInput, RunbookInput
from opsagent.tools.runbooks import get_runbook, slugify_alert
from tests.conftest import FakeKubernetesReader

pytestmark = pytest.mark.unit

JsonDict = dict[str, Any]


def application(
    name: str, *, hours_ago: float = 1, revisions: int = 1, phase: str = "Succeeded"
) -> JsonDict:
    history = [
        {
            "revision": f"{'a' * 39}{index}",
            "deployedAt": (datetime.now(UTC) - timedelta(hours=hours_ago + index)).isoformat(),
        }
        for index in range(revisions)
    ]
    return {
        "metadata": {"name": name},
        "status": {
            "sync": {"status": "Synced"},
            "health": {"status": "Healthy"},
            "operationState": {"phase": phase, "message": "successfully synced"},
            "history": history,
        },
    }


@pytest.mark.asyncio
async def test_recent_deploys_are_returned_newest_first(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(applications=[application("n8n", hours_ago=1, revisions=3)])

    history = await get_recent_deploys(DeploysInput(since_hours=24), reader)

    assert len(history.entries) == 3
    assert history.entries[0].deployed_at is not None
    assert history.entries[0].deployed_at > history.entries[1].deployed_at  # type: ignore[operator]


@pytest.mark.asyncio
async def test_deploys_outside_the_window_are_excluded(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(applications=[application("n8n", hours_ago=100)])

    history = await get_recent_deploys(DeploysInput(since_hours=24), reader)

    assert history.entries == []
    assert history.note is not None
    assert "no deploys" in history.note


@pytest.mark.asyncio
async def test_a_single_application_can_be_named(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(applications=[application("n8n"), application("opsagent")])

    history = await get_recent_deploys(DeploysInput(app="n8n"), reader)

    assert {entry.app for entry in history.entries} == {"n8n"}


@pytest.mark.asyncio
async def test_the_revision_is_shortened_for_reading(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(applications=[application("n8n")])

    history = await get_recent_deploys(DeploysInput(), reader)

    assert history.entries[0].revision is not None
    assert len(history.entries[0].revision) == 12


@pytest.mark.asyncio
async def test_an_application_that_never_synced_still_reports_its_state(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    # No history does not mean nothing is wrong. A stuck first sync is exactly
    # the kind of thing worth reporting.
    never_synced = {
        "metadata": {"name": "broken"},
        "status": {
            "sync": {"status": "OutOfSync"},
            "health": {"status": "Degraded"},
            "operationState": {"phase": "Failed", "message": "manifest generation error"},
        },
    }
    reader = make_reader(applications=[never_synced])

    history = await get_recent_deploys(DeploysInput(app="broken"), reader)

    assert history.entries[0].sync_status == "OutOfSync"
    assert history.entries[0].phase == "Failed"


@pytest.mark.asyncio
async def test_deploy_history_is_capped_and_says_so(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(applications=[application("n8n", hours_ago=0, revisions=10)])

    history = await get_recent_deploys(DeploysInput(limit=4), reader)

    assert len(history.entries) == 4
    assert history.note is not None
    assert "4 of 10" in history.note


# --- Runbooks ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("alert", "slug"),
    [
        ("KubePodCrashLooping", "kubepodcrashlooping"),
        ("Blackbox Probe Down", "blackbox-probe-down"),
        ("FlaskAppDown", "flaskappdown"),
    ],
)
def test_alert_names_slug_predictably(alert: str, slug: str) -> None:
    assert slugify_alert(alert) == slug


@pytest.mark.asyncio
async def test_a_matching_runbook_is_returned(tmp_path: Path) -> None:
    (tmp_path / "flaskappdown.md").write_text("# FlaskAppDown\n\nCheck the deployment.\n")

    entry = await get_runbook(RunbookInput(alert_name="FlaskAppDown"), tmp_path)

    assert entry.found is True
    assert entry.content is not None
    assert "Check the deployment" in entry.content


@pytest.mark.asyncio
async def test_a_missing_runbook_is_reported_not_raised(tmp_path: Path) -> None:
    # A missing runbook is an ordinary answer, not a tool failure. Raising
    # would turn "nobody wrote this down" into an investigation error.
    entry = await get_runbook(RunbookInput(alert_name="NeverWritten"), tmp_path)

    assert entry.found is False
    assert entry.content is None


@pytest.mark.asyncio
async def test_a_crafted_alert_name_cannot_escape_the_runbook_directory(tmp_path: Path) -> None:
    # The alert name arrives in a webhook payload, so it is attacker-influenced
    # if anyone can fire an alert.
    secret = tmp_path.parent / "outside.md"
    secret.write_text("should never be read")
    runbooks = tmp_path / "runbooks"
    runbooks.mkdir()

    entry = await get_runbook(RunbookInput(alert_name="../outside"), runbooks)

    assert entry.found is False


@pytest.mark.asyncio
async def test_a_very_long_runbook_is_capped(tmp_path: Path) -> None:
    (tmp_path / "huge.md").write_text("x" * 50_000)

    entry = await get_runbook(RunbookInput(alert_name="huge"), tmp_path)

    assert entry.content is not None
    assert len(entry.content) <= 8_000
