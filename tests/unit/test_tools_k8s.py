"""Kubernetes tools against a faked reader. No cluster, no socket."""

from collections.abc import Callable
from typing import Any

import pytest

from opsagent.tools.k8s import get_events, get_pod_status
from opsagent.tools.models import EventsInput, PodStatusInput
from tests.conftest import FakeKubernetesReader

pytestmark = pytest.mark.unit

JsonDict = dict[str, Any]


def oomkilled_pod() -> JsonDict:
    return {
        "metadata": {"name": "n8n-6d4b8c9f7-x2k9p"},
        "spec": {
            "node_name": "kali",
            "containers": [
                {"name": "n8n", "resources": {"limits": {"memory": "768Mi", "cpu": "1"}}}
            ],
        },
        "status": {
            "phase": "Running",
            "start_time": "2026-08-20T10:00:00+00:00",
            "conditions": [{"type": "Ready", "status": "False"}],
            "container_statuses": [
                {
                    "name": "n8n",
                    "ready": False,
                    "restart_count": 4,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "last_state": {
                        "terminated": {
                            "reason": "OOMKilled",
                            "exit_code": 137,
                            "finished_at": "2026-08-20T12:59:00+00:00",
                        }
                    },
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_pod_status_surfaces_the_last_termination_reason(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    # The single most useful field for a CrashLoopBackOff, and the one that
    # lives in last_state rather than state.
    reader = make_reader(pods=[oomkilled_pod()])

    report = await get_pod_status(PodStatusInput(namespace="ai-lab"), reader)

    container = report.items[0].containers[0]
    assert container.state == "waiting"
    assert container.waiting_reason == "CrashLoopBackOff"
    assert container.last_termination is not None
    assert container.last_termination.reason == "OOMKilled"
    assert container.last_termination.exit_code == 137


@pytest.mark.asyncio
async def test_pod_status_reports_limits_so_an_oomkill_can_be_judged(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(pods=[oomkilled_pod()])

    report = await get_pod_status(PodStatusInput(namespace="ai-lab"), reader)

    assert report.items[0].containers[0].memory_limit == "768Mi"
    assert report.items[0].node == "kali"


@pytest.mark.asyncio
async def test_naming_a_pod_reads_it_directly_instead_of_listing(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(pods=[oomkilled_pod()])

    await get_pod_status(PodStatusInput(namespace="ai-lab", pod="n8n-1"), reader)

    assert reader.calls == ["read_pod:ai-lab:n8n-1"]


@pytest.mark.asyncio
async def test_a_selector_is_passed_through(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(pods=[])

    report = await get_pod_status(PodStatusInput(namespace="ai-lab", selector="app=n8n"), reader)

    assert reader.calls == ["list_pods:ai-lab:app=n8n"]
    assert report.note == "no pods matched"


@pytest.mark.asyncio
async def test_camel_case_payloads_are_understood(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    # The official client returns snake_case; a raw API response and most
    # fixtures are camelCase. Both have to parse or the tool works in tests and
    # fails in the cluster.
    pod = {
        "metadata": {"name": "p"},
        "spec": {"nodeName": "kali", "containers": []},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "c", "ready": True, "restartCount": 2, "state": {"running": {}}}
            ],
        },
    }
    report = await get_pod_status(PodStatusInput(namespace="ai-lab"), make_reader(pods=[pod]))

    assert report.items[0].node == "kali"
    assert report.items[0].containers[0].restart_count == 2


def event(reason: str, *, minutes_ago: int, obj: str = "n8n", count: int = 1) -> JsonDict:
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "type": "Warning",
        "reason": reason,
        "message": f"{reason} happened",
        "count": count,
        "first_timestamp": stamp,
        "last_timestamp": stamp,
        "involved_object": {"name": obj},
    }


@pytest.mark.asyncio
async def test_events_outside_the_window_are_dropped(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(events=[event("Recent", minutes_ago=5), event("Ancient", minutes_ago=600)])

    result = await get_events(EventsInput(namespace="ai-lab", since_minutes=60), reader)

    assert [item.reason for item in result.events] == ["Recent"]


@pytest.mark.asyncio
async def test_events_are_newest_first(make_reader: Callable[..., FakeKubernetesReader]) -> None:
    reader = make_reader(events=[event("Older", minutes_ago=30), event("Newer", minutes_ago=1)])

    result = await get_events(EventsInput(namespace="ai-lab"), reader)

    assert [item.reason for item in result.events] == ["Newer", "Older"]


@pytest.mark.asyncio
async def test_events_can_be_narrowed_to_one_object(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(
        events=[event("A", minutes_ago=1, obj="n8n"), event("B", minutes_ago=1, obj="other")]
    )

    result = await get_events(EventsInput(namespace="ai-lab", involved_object="n8n"), reader)

    assert [item.reason for item in result.events] == ["A"]


@pytest.mark.asyncio
async def test_the_limit_is_reported_rather_than_applied_silently(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    reader = make_reader(events=[event(f"E{i}", minutes_ago=i + 1) for i in range(10)])

    result = await get_events(EventsInput(namespace="ai-lab", limit=3), reader)

    assert len(result.events) == 3
    assert result.note is not None
    assert "3 of 10" in result.note


@pytest.mark.asyncio
async def test_an_event_without_a_timestamp_is_kept(
    make_reader: Callable[..., FakeKubernetesReader],
) -> None:
    # Dropping it silently is worse than showing it without an age.
    reader = make_reader(events=[{"type": "Warning", "reason": "NoStamp", "message": "m"}])

    result = await get_events(EventsInput(namespace="ai-lab"), reader)

    assert [item.reason for item in result.events] == ["NoStamp"]
