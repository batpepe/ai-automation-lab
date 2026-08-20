"""Cluster state tools: pod status and events.

The tools depend on a small read-only protocol rather than on the Kubernetes
client directly. That keeps the official client, which is synchronous and heavy,
isolated in one adapter, and it means the unit tests inject a fake reader and
never open a socket.

Everything here is read-only by construction. The ServiceAccount these tools run
under has no write verbs and cannot read Secrets, which is documented in
docs/threat-model.md and enforced by deploy/manifests/rbac.yaml.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import anyio.to_thread

from opsagent.resilience import CircuitBreaker, RetryPolicy
from opsagent.resilience import call as resilient_call
from opsagent.tools.models import (
    ClusterEvent,
    ContainerStatus,
    EventList,
    EventsInput,
    PodStatus,
    PodStatusInput,
    PodStatusReport,
    TerminationDetail,
)

JsonDict = dict[str, Any]


class KubernetesReader(Protocol):
    """The only cluster access these tools have."""

    async def list_pods(self, namespace: str, selector: str | None) -> list[JsonDict]: ...

    async def read_pod(self, namespace: str, name: str) -> JsonDict: ...

    async def list_events(self, namespace: str) -> list[JsonDict]: ...

    async def list_argocd_applications(self) -> list[JsonDict]: ...


class OfficialClientReader:
    """Adapter over the official Kubernetes client.

    The client is synchronous, so every call runs in a worker thread. Wrapping
    it here rather than in each tool means the thread hop, the timeout and the
    circuit breaker are applied once.
    """

    def __init__(
        self,
        core_api: Any,
        custom_api: Any,
        *,
        argocd_namespace: str = "argocd",
        policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._core = core_api
        self._custom = custom_api
        self._argocd_namespace = argocd_namespace
        self._policy = policy if policy is not None else RetryPolicy(timeout=15.0)
        self._breaker = breaker

    async def _call(self, func: Any, /, **kwargs: Any) -> Any:
        async def attempt() -> Any:
            return await anyio.to_thread.run_sync(lambda: func(**kwargs))

        return await resilient_call(attempt, policy=self._policy, breaker=self._breaker)

    async def list_pods(self, namespace: str, selector: str | None) -> list[JsonDict]:
        response = await self._call(
            self._core.list_namespaced_pod,
            namespace=namespace,
            label_selector=selector or "",
        )
        return [item.to_dict() for item in response.items]

    async def read_pod(self, namespace: str, name: str) -> JsonDict:
        response = await self._call(self._core.read_namespaced_pod, name=name, namespace=namespace)
        result: JsonDict = response.to_dict()
        return result

    async def list_events(self, namespace: str) -> list[JsonDict]:
        response = await self._call(self._core.list_namespaced_event, namespace=namespace)
        return [item.to_dict() for item in response.items]

    async def list_argocd_applications(self) -> list[JsonDict]:
        response = await self._call(
            self._custom.list_namespaced_custom_object,
            group="argoproj.io",
            version="v1alpha1",
            namespace=self._argocd_namespace,
            plural="applications",
        )
        items: list[JsonDict] = response.get("items", [])
        return items


def _as_text(value: Any) -> str | None:
    """Timestamps arrive as datetimes from the client and strings from a fake."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _container_status(status: JsonDict, spec_by_name: dict[str, JsonDict]) -> ContainerStatus:
    state = status.get("state") or {}
    running, waiting, terminated = (
        state.get("running"),
        state.get("waiting"),
        state.get("terminated"),
    )
    if running:
        state_name = "running"
    elif waiting:
        state_name = "waiting"
    elif terminated:
        state_name = "terminated"
    else:
        state_name = "unknown"

    # last_state is where an OOMKill lives after the container has restarted,
    # which is the single most useful field for a CrashLoopBackOff.
    last = (status.get("last_state") or status.get("lastState") or {}).get("terminated")
    if last is None and terminated:
        last = terminated

    resources = (spec_by_name.get(str(status.get("name")), {}) or {}).get("resources") or {}
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}

    return ContainerStatus(
        name=str(status.get("name", "")),
        ready=bool(status.get("ready", False)),
        restart_count=int(status.get("restart_count", status.get("restartCount", 0)) or 0),
        state=state_name,
        waiting_reason=(waiting or {}).get("reason"),
        last_termination=(
            TerminationDetail(
                reason=last.get("reason"),
                exit_code=last.get("exit_code", last.get("exitCode")),
                finished_at=_as_text(last.get("finished_at", last.get("finishedAt"))),
                message=last.get("message"),
            )
            if last
            else None
        ),
        cpu_request=requests.get("cpu"),
        memory_request=requests.get("memory"),
        cpu_limit=limits.get("cpu"),
        memory_limit=limits.get("memory"),
    )


def _pod_status(pod: JsonDict) -> PodStatus:
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    spec_by_name = {str(c.get("name")): c for c in spec.get("containers") or []}

    return PodStatus(
        name=str(metadata.get("name", "")),
        phase=str(status.get("phase", "Unknown")),
        node=spec.get("node_name") or spec.get("nodeName"),
        start_time=_as_text(status.get("start_time", status.get("startTime"))),
        containers=[
            _container_status(item, spec_by_name)
            for item in status.get("container_statuses") or status.get("containerStatuses") or []
        ],
        conditions={
            str(item.get("type")): str(item.get("status"))
            for item in status.get("conditions") or []
            if item.get("type")
        },
    )


async def get_pod_status(params: PodStatusInput, reader: KubernetesReader) -> PodStatusReport:
    """Pod phase, container states and the last termination reason."""
    if params.pod is not None:
        pods = [await reader.read_pod(params.namespace, params.pod)]
    else:
        pods = await reader.list_pods(params.namespace, params.selector)

    items = [_pod_status(pod) for pod in pods]
    note = None if items else "no pods matched"
    return PodStatusReport(namespace=params.namespace, items=items, note=note)


async def get_events(params: EventsInput, reader: KubernetesReader) -> EventList:
    """Recent namespace events, newest first."""
    raw = await reader.list_events(params.namespace)
    cutoff = datetime.now(UTC) - timedelta(minutes=params.since_minutes)

    selected: list[tuple[datetime | None, ClusterEvent]] = []
    for item in raw:
        involved = (item.get("involved_object") or item.get("involvedObject") or {}).get("name")
        if params.involved_object and involved != params.involved_object:
            continue

        last_raw = item.get("last_timestamp") or item.get("lastTimestamp")
        last_seen = _parse_time(last_raw)
        # An event with no usable timestamp is kept rather than dropped: losing
        # it silently is worse than showing it without an age.
        if last_seen is not None and last_seen < cutoff:
            continue

        selected.append(
            (
                last_seen,
                ClusterEvent(
                    type=str(item.get("type", "Normal")),
                    reason=str(item.get("reason", "")),
                    message=str(item.get("message", "")),
                    count=int(item.get("count") or 1),
                    first_seen=_as_text(item.get("first_timestamp") or item.get("firstTimestamp")),
                    last_seen=_as_text(last_raw),
                    involved_object=involved,
                ),
            )
        )

    selected.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC), reverse=True)
    events = [event for _, event in selected[: params.limit]]
    note = None
    if len(selected) > params.limit:
        note = f"showing {params.limit} of {len(selected)} matching events"
    return EventList(namespace=params.namespace, events=events, note=note)
