"""Client for the n8n public REST API.

Endpoints and schemas were read from the OpenAPI specification in n8n-io/n8n on
2026-08-18 rather than recalled. Three facts from it shape this module:

- `workflowCreate` and `workflow` both set `additionalProperties: false`, so
  echoing back a field the API considers read-only is a 400, not a warning.
- `tags` is read-only on both, so tags are managed through their own endpoints
  and never as part of a workflow payload.
- `GET /workflows` paginates with `cursor` and returns `nextCursor`, so a naive
  single request silently truncates a long list.

Credentials are deliberately absent. The API can create them but never returns
their values, which is the correct boundary for a git-backed workflow: the
workflow references a credential by name, and the value lives only in the
instance, provisioned from a sealed secret.
"""

from __future__ import annotations

from typing import Any, Self

import httpx

from opsagent.resilience import CircuitBreaker, RetryPolicy
from opsagent.resilience import call as resilient_call

JsonDict = dict[str, Any]

API_PREFIX = "/api/v1"
API_KEY_HEADER = "X-N8N-API-KEY"

# Marked readOnly in the OpenAPI schema. Present in every response and rejected
# in any request body.
READ_ONLY_FIELDS = frozenset(
    {
        "id",
        "active",
        "createdAt",
        "updatedAt",
        "isArchived",
        "versionId",
        "triggerCount",
        "tags",
    }
)

# An allowlist rather than "everything except READ_ONLY_FIELDS". A denylist would
# let a field n8n adds later leak into the committed JSON and make every export
# after an upgrade look like a change. The cost is that a genuinely useful new
# field has to be added here on purpose, which is the safer direction to fail.
WRITABLE_FIELDS = frozenset(
    {
        "name",
        "nodes",
        "connections",
        "settings",
        "staticData",
        "pinData",
        "nodeGroups",
        "meta",
        "description",
    }
)

# Required by the create schema. A workflow missing any of these is rejected.
REQUIRED_FIELDS = frozenset({"name", "nodes", "connections", "settings"})


class N8nError(RuntimeError):
    """Base class for every failure raised by this client."""


class N8nApiError(N8nError):
    """The API answered, and the answer was a rejection. Not worth a retry."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"n8n API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class N8nUnavailableError(N8nApiError):
    """The API failed in a way that another attempt might survive."""


class N8nClient:
    """Async client for the workflow endpoints of the n8n public API.

    Every request goes through the resilience wrapper, so a wedged instance
    cannot hold a sync run open indefinitely and a restarting pod is retried
    rather than reported as a failure.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
        policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._policy = policy if policy is not None else RetryPolicy(timeout=timeout)
        self._breaker = breaker
        self._client = httpx.AsyncClient(
            base_url=self._normalise_base_url(base_url),
            headers={API_KEY_HEADER: api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    @staticmethod
    def _normalise_base_url(base_url: str) -> str:
        """Accept the instance root or a URL that already ends in the API prefix."""
        trimmed = base_url.rstrip("/")
        if trimmed.endswith(API_PREFIX):
            return trimmed
        return f"{trimmed}{API_PREFIX}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: JsonDict | None = None,
        json: JsonDict | None = None,
    ) -> JsonDict:
        async def attempt() -> JsonDict:
            response = await self._client.request(method, path, params=params, json=json)
            if response.status_code >= 500:
                raise N8nUnavailableError(response.status_code, response.text[:500])
            if response.status_code >= 400:
                raise N8nApiError(response.status_code, response.text[:500])
            if not response.content:
                return {}
            decoded: JsonDict = response.json()
            return decoded

        return await resilient_call(
            attempt,
            policy=self._policy,
            breaker=self._breaker,
            # A 4xx is excluded on purpose: the payload is wrong, so sending it
            # again just asks the same question and gets the same answer.
            retry_on=(N8nUnavailableError, httpx.TransportError, TimeoutError),
        )

    async def list_workflows(self, *, exclude_pinned_data: bool = True) -> list[JsonDict]:
        """Return every workflow, following `nextCursor` to the end.

        Pinned data is excluded by default. It is editor-local test state, and
        for an alert-driven instance it can hold a real production payload that
        has no business being committed to a public repository.
        """
        workflows: list[JsonDict] = []
        cursor: str | None = None
        while True:
            params: JsonDict = {"limit": 100}
            if exclude_pinned_data:
                params["excludePinnedData"] = "true"
            if cursor is not None:
                params["cursor"] = cursor

            page = await self._request("GET", "/workflows", params=params)
            workflows.extend(page.get("data", []))

            cursor = page.get("nextCursor")
            if not cursor:
                return workflows

    async def get_workflow(self, workflow_id: str) -> JsonDict:
        return await self._request("GET", f"/workflows/{workflow_id}")

    async def create_workflow(self, payload: JsonDict) -> JsonDict:
        return await self._request("POST", "/workflows", json=payload)

    async def update_workflow(self, workflow_id: str, payload: JsonDict) -> JsonDict:
        return await self._request("PUT", f"/workflows/{workflow_id}", json=payload)

    async def activate_workflow(self, workflow_id: str) -> JsonDict:
        return await self._request("POST", f"/workflows/{workflow_id}/activate")

    async def deactivate_workflow(self, workflow_id: str) -> JsonDict:
        return await self._request("POST", f"/workflows/{workflow_id}/deactivate")
