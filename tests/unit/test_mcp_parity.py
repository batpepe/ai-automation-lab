"""MCP and the in-process binding must be the same tools, not similar ones.

Both read one registry, so drift can only come from the MCP adapter. These
tests pin the two places it could: the schema the client is shown, and the
result it gets back.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from opsagent.mcp import build_mcp_server
from opsagent.redaction import Redactor
from opsagent.tools import build_registry
from opsagent.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


@pytest.fixture
def registry(tmp_path: Path, make_reader: Any) -> ToolRegistry:
    pod = {
        "metadata": {"name": "n8n-1"},
        "spec": {"nodeName": "kali", "containers": []},
        "status": {"phase": "Running", "containerStatuses": []},
    }
    return build_registry(
        kubernetes=make_reader(pods=[pod]),
        loki=None,
        prometheus=None,
        runbook_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_both_bindings_expose_the_same_tool_names(registry: ToolRegistry) -> None:
    server = build_mcp_server(registry)

    exposed = sorted(tool.name for tool in await server.list_tools())

    assert exposed == list(registry.names)


@pytest.mark.asyncio
async def test_the_mcp_schema_matches_the_registry_input_model(registry: ToolRegistry) -> None:
    # If MCP nested the arguments under `params`, an MCP client and the agent
    # would need different call sites for the same tool.
    server = build_mcp_server(registry)
    tools = {tool.name: tool for tool in await server.list_tools()}

    for spec in registry.specs():
        model_schema = spec.json_schema()
        mcp_schema = tools[spec.name].input_schema

        assert set(mcp_schema["properties"]) == set(model_schema["properties"]), spec.name
        assert set(mcp_schema.get("required", [])) == set(model_schema.get("required", [])), (
            spec.name
        )


@pytest.mark.asyncio
async def test_descriptions_are_carried_across(registry: ToolRegistry) -> None:
    # The description is how the model decides when to call a tool. Losing it
    # in one binding makes that binding quietly worse.
    server = build_mcp_server(registry)

    for tool in await server.list_tools():
        assert tool.description == registry.get(tool.name).description


@pytest.mark.asyncio
async def test_the_same_arguments_produce_the_same_result(registry: ToolRegistry) -> None:
    arguments = {"namespace": "ai-lab"}
    redactor = Redactor()

    direct = await registry.call("get_pod_status", arguments, redactor=redactor)
    server = build_mcp_server(registry, redactor=Redactor())
    through_mcp = await server.call_tool("get_pod_status", arguments)

    payload = _payload(through_mcp)
    assert payload["value"] == direct.value
    assert payload["ok"] is direct.ok


@pytest.mark.asyncio
async def test_the_mcp_envelope_reports_the_guarantees(registry: ToolRegistry) -> None:
    # A client that cannot see truncation or redaction counts will reason as
    # though it saw everything.
    server = build_mcp_server(registry)

    payload = _payload(await server.call_tool("get_pod_status", {"namespace": "ai-lab"}))

    assert set(payload) >= {"ok", "value", "redactions_applied", "truncated", "duration_ms"}


@pytest.mark.asyncio
async def test_a_tool_failure_crosses_the_binding_as_a_result_not_an_exception(
    registry: ToolRegistry,
) -> None:
    # loki is None in this registry, so query_logs raises inside the handler.
    server = build_mcp_server(registry)

    payload = _payload(await server.call_tool("query_logs", {"namespace": "ai-lab"}))

    assert payload["ok"] is False
    assert payload["error"]


def _payload(result: Any) -> dict[str, Any]:
    """Read the tool result whichever way this MCP version returned it."""
    if getattr(result, "structured_content", None):
        content: dict[str, Any] = result.structured_content
        return content
    decoded: dict[str, Any] = json.loads(result.content[0].text)
    return decoded
