"""The client is tested against a faked HTTP layer, never a live instance.

The behaviours pinned here are the ones the OpenAPI specification implies and
that a single happy-path request would not catch: cursor pagination, the split
between retryable and rejected responses, and the auth header.
"""

import httpx
import pytest
import respx

from opsagent.n8n.client import (
    API_KEY_HEADER,
    N8nApiError,
    N8nClient,
    N8nUnavailableError,
)
from opsagent.resilience import RetryPolicy

pytestmark = pytest.mark.unit

BASE = "http://n8n.test"
WORKFLOWS = f"{BASE}/api/v1/workflows"

# Zero delays so the retry tests do not spend real time sleeping.
FAST_RETRY = RetryPolicy(attempts=3, base_delay=0.0, max_delay=0.0, timeout=5.0)


def workflow(name: str, workflow_id: str = "w1", **extra: object) -> dict[str, object]:
    return {
        "id": workflow_id,
        "name": name,
        "nodes": [],
        "connections": {},
        "settings": {},
        "active": False,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-02T00:00:00.000Z",
        "versionId": "v1",
        **extra,
    }


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://n8n.test", "http://n8n.test/api/v1"),
        ("http://n8n.test/", "http://n8n.test/api/v1"),
        # Already-prefixed URLs are left alone rather than doubled up.
        ("http://n8n.test/api/v1", "http://n8n.test/api/v1"),
        ("http://n8n.test/api/v1/", "http://n8n.test/api/v1"),
    ],
)
def test_base_url_is_normalised(given: str, expected: str) -> None:
    assert N8nClient._normalise_base_url(given) == expected


@pytest.mark.asyncio
@respx.mock
async def test_api_key_is_sent_on_every_request() -> None:
    route = respx.get(WORKFLOWS).mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None})
    )

    async with N8nClient(BASE, "secret-key") as client:
        await client.list_workflows()

    assert route.calls.last.request.headers[API_KEY_HEADER] == "secret-key"


@pytest.mark.asyncio
@respx.mock
async def test_list_workflows_follows_the_cursor_to_the_end() -> None:
    # A single request would silently return only the first page, which is the
    # kind of bug that stays hidden until the instance has enough workflows.
    respx.get(WORKFLOWS).mock(
        side_effect=[
            httpx.Response(200, json={"data": [workflow("first", "w1")], "nextCursor": "next"}),
            httpx.Response(200, json={"data": [workflow("second", "w2")], "nextCursor": None}),
        ]
    )

    async with N8nClient(BASE, "key") as client:
        workflows = await client.list_workflows()

    assert [item["name"] for item in workflows] == ["first", "second"]


@pytest.mark.asyncio
@respx.mock
async def test_list_workflows_excludes_pinned_data_by_default() -> None:
    route = respx.get(WORKFLOWS).mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None})
    )

    async with N8nClient(BASE, "key") as client:
        await client.list_workflows()

    assert route.calls.last.request.url.params["excludePinnedData"] == "true"


@pytest.mark.asyncio
@respx.mock
async def test_rejected_request_raises_and_is_not_retried() -> None:
    route = respx.post(WORKFLOWS).mock(return_value=httpx.Response(400, text="bad payload"))

    async with N8nClient(BASE, "key", policy=FAST_RETRY) as client:
        with pytest.raises(N8nApiError) as caught:
            await client.create_workflow({"name": "x"})

    assert caught.value.status_code == 400
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_server_error_is_retried_then_surfaced() -> None:
    route = respx.get(WORKFLOWS).mock(return_value=httpx.Response(503, text="restarting"))

    async with N8nClient(BASE, "key", policy=FAST_RETRY) as client:
        with pytest.raises(N8nUnavailableError):
            await client.list_workflows()

    assert route.call_count == FAST_RETRY.attempts


@pytest.mark.asyncio
@respx.mock
async def test_server_error_that_recovers_is_transparent() -> None:
    # A restarting n8n pod is normal on a single node. It should cost a retry,
    # not a failed sync.
    respx.get(WORKFLOWS).mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"data": [workflow("recovered")], "nextCursor": None}),
        ]
    )

    async with N8nClient(BASE, "key", policy=FAST_RETRY) as client:
        workflows = await client.list_workflows()

    assert [item["name"] for item in workflows] == ["recovered"]


@pytest.mark.asyncio
@respx.mock
async def test_transport_failure_is_retried() -> None:
    route = respx.get(WORKFLOWS).mock(side_effect=httpx.ConnectError("no route to host"))

    async with N8nClient(BASE, "key", policy=FAST_RETRY) as client:
        with pytest.raises(httpx.ConnectError):
            await client.list_workflows()

    assert route.call_count == FAST_RETRY.attempts


@pytest.mark.asyncio
@respx.mock
async def test_activate_and_deactivate_hit_their_own_endpoints() -> None:
    # `active` is read-only in the schema, so activation cannot ride along in
    # the update payload and has to be its own call.
    activate = respx.post(f"{WORKFLOWS}/w1/activate").mock(
        return_value=httpx.Response(200, json=workflow("x", active=True))
    )
    deactivate = respx.post(f"{WORKFLOWS}/w1/deactivate").mock(
        return_value=httpx.Response(200, json=workflow("x"))
    )

    async with N8nClient(BASE, "key") as client:
        await client.activate_workflow("w1")
        await client.deactivate_workflow("w1")

    assert activate.called
    assert deactivate.called


@pytest.mark.asyncio
@respx.mock
async def test_empty_body_is_not_a_parse_error() -> None:
    respx.post(f"{WORKFLOWS}/w1/activate").mock(return_value=httpx.Response(204))

    async with N8nClient(BASE, "key") as client:
        assert await client.activate_workflow("w1") == {}
