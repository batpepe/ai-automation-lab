"""The registry's guarantees, which every tool inherits whether it asks or not.

Redaction, size caps, timing and error capture are enforced here precisely so
that a tool added later cannot forget them. These tests are what stop that
being a comment rather than a fact.
"""

import pytest
from pydantic import BaseModel

from opsagent.redaction import Redactor
from opsagent.tools.registry import ToolError, ToolRegistry, ToolSpec

pytestmark = pytest.mark.unit


class EchoInput(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = "hello"
    count: int = 1


class EchoOutput(BaseModel):
    items: list[str] = []
    note: str | None = None


def echo_spec(handler) -> ToolSpec:  # type: ignore[no-untyped-def]
    return ToolSpec(
        name="echo",
        description="Repeat the input, for testing the registry's guarantees.",
        input_model=EchoInput,
        output_model=EchoOutput,
        handler=handler,
    )


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


@pytest.mark.asyncio
async def test_tool_output_is_redacted_before_it_is_returned(redactor: Redactor) -> None:
    # The point of putting redaction here: a tool author cannot opt out.
    async def leaky(params: BaseModel) -> EchoOutput:
        return EchoOutput(items=["connect to 10.42.0.7 as ops@batpepe.online"])

    registry = ToolRegistry()
    registry.register(echo_spec(leaky))

    result = await registry.call("echo", {}, redactor=redactor)

    assert "10.42.0.7" not in str(result.value)
    assert "ops@batpepe.online" not in str(result.value)
    assert result.redactions_applied == 2


@pytest.mark.asyncio
async def test_a_clean_result_reports_zero_redactions(redactor: Redactor) -> None:
    # Distinguishable from "redaction did not run", which is the failure this
    # counter exists to make visible.
    async def clean(params: BaseModel) -> EchoOutput:
        return EchoOutput(items=["pod n8n-1 is Ready"])

    registry = ToolRegistry()
    registry.register(echo_spec(clean))

    result = await registry.call("echo", {}, redactor=redactor)

    assert result.redactions_applied == 0
    assert result.ok


@pytest.mark.asyncio
async def test_an_oversized_result_is_trimmed_and_flagged(redactor: Redactor) -> None:
    # One runaway tool result can consume an investigation's whole budget
    # before the model has seen any other evidence.
    async def flood(params: BaseModel) -> EchoOutput:
        return EchoOutput(items=[f"line {index} " + "x" * 100 for index in range(2000)])

    registry = ToolRegistry(max_result_chars=2_000)
    registry.register(echo_spec(flood))

    result = await registry.call("echo", {}, redactor=redactor)

    assert result.truncated is True
    assert len(result.value["items"]) < 2000
    assert "truncated_note" in result.value


@pytest.mark.asyncio
async def test_a_result_under_the_cap_is_untouched(redactor: Redactor) -> None:
    async def small(params: BaseModel) -> EchoOutput:
        return EchoOutput(items=["a", "b"])

    registry = ToolRegistry(max_result_chars=10_000)
    registry.register(echo_spec(small))

    result = await registry.call("echo", {}, redactor=redactor)

    assert result.truncated is False
    assert result.value["items"] == ["a", "b"]


@pytest.mark.asyncio
async def test_a_failing_tool_returns_an_error_rather_than_raising(redactor: Redactor) -> None:
    # A partial investigation with a stated gap beats no investigation. The
    # agent is expected to reason about what it could not determine.
    async def broken(params: BaseModel) -> EchoOutput:
        raise ConnectionError("loki is unreachable")

    registry = ToolRegistry()
    registry.register(echo_spec(broken))

    result = await registry.call("echo", {}, redactor=redactor)

    assert result.ok is False
    assert result.error is not None
    assert "loki is unreachable" in result.error
    assert result.value is None


@pytest.mark.asyncio
async def test_an_error_message_is_redacted_too(redactor: Redactor) -> None:
    # Exception text quotes cluster content freely. A connection error that
    # prints the DSN would leak the password through the error path.
    async def leaky_error(params: BaseModel) -> EchoOutput:
        raise ConnectionError("failed to reach postgres://n8n:hunter2@db.apps.svc:5432")

    registry = ToolRegistry()
    registry.register(echo_spec(leaky_error))

    result = await registry.call("echo", {}, redactor=redactor)

    assert result.error is not None
    assert "hunter2" not in result.error
    assert result.redactions_applied >= 1


@pytest.mark.asyncio
async def test_invalid_arguments_become_an_error_result(redactor: Redactor) -> None:
    # The model chooses these arguments, so it will get them wrong. That is an
    # evidence gap it can recover from, not a crash.
    async def never_called(params: BaseModel) -> EchoOutput:
        raise AssertionError("handler must not run on invalid input")

    registry = ToolRegistry()
    registry.register(echo_spec(never_called))

    result = await registry.call("echo", {"count": "not-a-number"}, redactor=redactor)

    assert result.ok is False
    assert result.error is not None
    assert "ValidationError" in result.error


@pytest.mark.asyncio
async def test_an_unexpected_argument_is_rejected(redactor: Redactor) -> None:
    async def handler(params: BaseModel) -> EchoOutput:
        return EchoOutput()

    registry = ToolRegistry()
    registry.register(echo_spec(handler))

    result = await registry.call("echo", {"nemespace": "typo"}, redactor=redactor)

    assert result.ok is False


@pytest.mark.asyncio
async def test_duration_is_recorded(redactor: Redactor) -> None:
    async def handler(params: BaseModel) -> EchoOutput:
        return EchoOutput()

    registry = ToolRegistry()
    registry.register(echo_spec(handler))

    result = await registry.call("echo", {}, redactor=redactor)

    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_calling_an_unknown_tool_is_an_error(redactor: Redactor) -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolError, match="unknown tool"):
        await registry.call("nope", {}, redactor=redactor)


def test_registering_the_same_name_twice_is_refused() -> None:
    # Two tools with one name means the binding that wins is whichever
    # registered last, which is not a thing to discover at runtime.
    async def handler(params: BaseModel) -> EchoOutput:
        return EchoOutput()

    registry = ToolRegistry()
    registry.register(echo_spec(handler))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(echo_spec(handler))


def test_specs_are_returned_in_a_stable_order() -> None:
    # The tool list is part of the prompt prefix. An unstable order would
    # invalidate the provider's cache on every call.
    async def handler(params: BaseModel) -> EchoOutput:
        return EchoOutput()

    registry = ToolRegistry()
    for name in ("zebra", "alpha", "middle"):
        registry.register(
            ToolSpec(
                name=name,
                description=name,
                input_model=EchoInput,
                output_model=EchoOutput,
                handler=handler,
            )
        )

    assert registry.names == ("alpha", "middle", "zebra")


def test_the_input_schema_is_exposed_for_both_bindings() -> None:
    async def handler(params: BaseModel) -> EchoOutput:
        return EchoOutput()

    registry = ToolRegistry()
    registry.register(echo_spec(handler))

    schema = registry.get("echo").json_schema()

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"text", "count"}
