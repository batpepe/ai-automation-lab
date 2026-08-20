"""Shared fakes.

The Kubernetes fake lives here rather than in a test module so that every suite
gets the same one. A second, subtly different fake is how a tool ends up passing
its own tests and failing against the cluster.
"""

from collections.abc import Callable
from typing import Any

import pytest

JsonDict = dict[str, Any]


class FakeKubernetesReader:
    """Implements the read-only protocol the cluster tools depend on."""

    def __init__(
        self,
        pods: list[JsonDict] | None = None,
        events: list[JsonDict] | None = None,
        applications: list[JsonDict] | None = None,
    ) -> None:
        self.pods = pods or []
        self.events = events or []
        self.applications = applications or []
        self.calls: list[str] = []

    async def list_pods(self, namespace: str, selector: str | None) -> list[JsonDict]:
        self.calls.append(f"list_pods:{namespace}:{selector}")
        return self.pods

    async def read_pod(self, namespace: str, name: str) -> JsonDict:
        self.calls.append(f"read_pod:{namespace}:{name}")
        return self.pods[0]

    async def list_events(self, namespace: str) -> list[JsonDict]:
        self.calls.append(f"list_events:{namespace}")
        return self.events

    async def list_argocd_applications(self) -> list[JsonDict]:
        self.calls.append("list_applications")
        return self.applications


@pytest.fixture
def make_reader() -> Callable[..., FakeKubernetesReader]:
    """Build a fake cluster reader with whatever the test needs in it."""

    def _make(
        pods: list[JsonDict] | None = None,
        events: list[JsonDict] | None = None,
        applications: list[JsonDict] | None = None,
    ) -> FakeKubernetesReader:
        return FakeKubernetesReader(pods, events, applications)

    return _make
