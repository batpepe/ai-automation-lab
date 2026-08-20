"""One definition per tool, three consumers.

The registry is the single source of truth for what the agent can do. The MCP
server, the agent's in-process loop and the REST surface all read from it, so a
tool cannot exist in one and be missing or differently shaped in another. A
parity test pins that.

It is also where the guarantees live. Redaction, output size caps, timing and
error capture are applied here rather than in each tool, because a guarantee
that every tool has to remember is one a new tool will forget.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from opsagent.redaction import Redactor

# Roughly 8k tokens of JSON. A single oversized tool result can consume an
# investigation's whole budget before the model has seen any other evidence,
# so the cap is a guardrail rather than a formatting preference.
DEFAULT_MAX_RESULT_CHARS = 32_000

ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


class ToolError(RuntimeError):
    """A tool failed in a way the agent should see as an evidence gap."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything both bindings need to expose one tool."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler

    def json_schema(self) -> dict[str, Any]:
        """Input schema, used by MCP and by the provider's tool declarations."""
        return self.input_model.model_json_schema()


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool produced, plus the evidence that the guarantees were applied."""

    tool: str
    value: Any
    redactions_applied: int
    truncated: bool
    duration_ms: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ToolRegistry:
    """Holds tool definitions and enforces what every call must obey."""

    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    _specs: dict[str, ToolSpec] = field(default_factory=dict, init=False)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolError(f"unknown tool {name!r}") from None

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        redactor: Redactor,
    ) -> ToolResult:
        """Run a tool and return a result that is safe to put in a prompt.

        A failure is returned rather than raised. The agent's job is to reason
        about what it could not determine, and a tool that raises turns a
        partial investigation into no investigation.
        """
        spec = self.get(name)
        started = time.monotonic()

        try:
            parsed = spec.input_model.model_validate(arguments)
            output = await spec.handler(parsed)
        except Exception as error:  # noqa: BLE001 - deliberately total
            # The message can quote cluster content, so it is redacted like any
            # other tool output before anyone sees it.
            message = redactor.redact_text(f"{type(error).__name__}: {error}")
            return ToolResult(
                tool=name,
                value=None,
                redactions_applied=message.count,
                truncated=False,
                duration_ms=self._elapsed_ms(started),
                error=message.value,
            )

        redacted = redactor.redact(output.model_dump(mode="json"))
        value, truncated = self._truncate(redacted.value)

        return ToolResult(
            tool=name,
            value=value,
            redactions_applied=redacted.count,
            truncated=truncated,
            duration_ms=self._elapsed_ms(started),
        )

    def _truncate(self, value: Any) -> tuple[Any, bool]:
        """Cap the serialised size, telling the model when something was cut."""
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) <= self.max_result_chars:
            return value, False

        if isinstance(value, dict):
            for key in ("items", "entries", "lines", "results", "samples", "events"):
                if isinstance(value.get(key), list) and value[key]:
                    trimmed = dict(value)
                    trimmed[key] = self._trim_list(value[key], encoded)
                    trimmed["truncated_note"] = (
                        f"{key} was shortened to fit the {self.max_result_chars} character cap"
                    )
                    return trimmed, True

        # Nothing list-shaped to shorten, so say so rather than emit a
        # half-parsed fragment of JSON.
        return {
            "truncated_note": (
                f"result exceeded the {self.max_result_chars} character cap and was dropped; "
                "narrow the query and try again"
            )
        }, True

    def _trim_list(self, items: list[Any], encoded: str) -> list[Any]:
        keep = max(1, int(len(items) * self.max_result_chars / len(encoded)) - 1)
        return items[:keep]

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)
