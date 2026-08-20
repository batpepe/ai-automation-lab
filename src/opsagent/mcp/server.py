"""MCP binding for the cluster tools.

The same registry that the agent loop uses, exposed over the Model Context
Protocol so the tools can be driven from an editor session before any agent
exists. That ordering is deliberate: a tool layer worth trusting is one a human
has already used to answer a real question.

Tool schemas are generated from the registry's pydantic input models, so the
arguments an MCP client sends are exactly the arguments the in-process binding
takes. A parity test pins that, because two bindings that drift are two
different tools wearing one name.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp.server import MCPServer

from opsagent.redaction import Redactor
from opsagent.tools.registry import ToolRegistry, ToolSpec

SERVER_NAME = "opsagent"


def _signature_from_model(spec: ToolSpec) -> tuple[list[inspect.Parameter], dict[str, Any]]:
    """Mirror a pydantic model's fields as explicit keyword parameters.

    MCP derives a tool's JSON Schema from the function signature. Passing the
    model as a single parameter would nest every argument under `params`, so the
    MCP surface would take `{"params": {"namespace": "x"}}` while the registry
    takes `{"namespace": "x"}`. Generating the signature keeps them identical.
    """
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, field in spec.input_model.model_fields.items():
        default = (
            inspect.Parameter.empty
            if field.is_required()
            else field.get_default(call_default_factory=True)
        )
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )
        annotations[name] = field.annotation
    return parameters, annotations


def build_mcp_server(
    registry: ToolRegistry,
    *,
    redactor: Redactor | None = None,
    name: str = SERVER_NAME,
) -> MCPServer:
    """Expose every registered tool over MCP.

    One redactor for the life of the server, so `<ip-1>` means the same host for
    the whole session and a human reading the transcript can follow it.
    """
    server = MCPServer(name)
    shared_redactor = redactor if redactor is not None else Redactor()

    for spec in registry.specs():
        server.add_tool(
            _make_callable(spec, registry, shared_redactor),
            name=spec.name,
            description=spec.description,
        )
    return server


def _make_callable(spec: ToolSpec, registry: ToolRegistry, redactor: Redactor) -> Any:
    parameters, annotations = _signature_from_model(spec)

    async def call(**kwargs: Any) -> dict[str, Any]:
        result = await registry.call(spec.name, kwargs, redactor=redactor)
        # The envelope travels with the payload rather than beside it: a client
        # that cannot see that a result was truncated or that redaction ran will
        # reason as though it saw everything.
        return {
            "tool": result.tool,
            "ok": result.ok,
            "value": result.value,
            "error": result.error,
            "redactions_applied": result.redactions_applied,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
        }

    call.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    call.__annotations__ = {**annotations, "return": dict[str, Any]}
    call.__name__ = spec.name
    call.__doc__ = spec.description
    return call
