"""Container logs, read through Loki rather than the Kubernetes API.

Reading logs from the Kubernetes API only works while the pod still exists, and
the pod being gone is exactly the case worth investigating. Loki keeps them
after the restart that destroyed the evidence.

The deployed Loki is the deprecated loki-stack chart running as a single binary,
so this targets the plain query API and no gateway. That keeps a future move to
the current chart a URL change rather than a rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx

from opsagent.resilience import CircuitBreaker, RetryPolicy
from opsagent.resilience import call as resilient_call
from opsagent.tools.models import LogExcerpt, LogLine, LogsInput

JsonDict = dict[str, Any]


class LokiError(RuntimeError):
    """Loki answered with a rejection."""


class LokiUnavailableError(LokiError):
    """Loki failed in a way another attempt might survive."""


def escape_label_value(value: str) -> str:
    """Escape a value for a LogQL string literal.

    The pod name and the grep term come from the model, so they are untrusted
    input to a query language. Escaping is not politeness here.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_query(params: LogsInput) -> str:
    """Build the LogQL selector for one namespace, pod and optional filter."""
    selectors = [f'namespace="{escape_label_value(params.namespace)}"']
    if params.pod:
        selectors.append(f'pod="{escape_label_value(params.pod)}"')
    if params.container:
        selectors.append(f'container="{escape_label_value(params.container)}"')

    query = "{" + ", ".join(selectors) + "}"
    if params.grep:
        query += f' |~ "{escape_label_value(params.grep)}"'
    return query


class LokiClient:
    """Minimal read-only client for the Loki query API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._policy = policy if policy is not None else RetryPolicy(timeout=timeout)
        self._breaker = breaker
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query_range(
        self, query: str, *, start: datetime, end: datetime, limit: int
    ) -> JsonDict:
        async def attempt() -> JsonDict:
            response = await self._client.get(
                "/loki/api/v1/query_range",
                params={
                    "query": query,
                    # Loki wants nanoseconds since the epoch.
                    "start": int(start.timestamp() * 1_000_000_000),
                    "end": int(end.timestamp() * 1_000_000_000),
                    "limit": limit,
                    "direction": "backward",
                },
            )
            if response.status_code >= 500:
                raise LokiUnavailableError(f"{response.status_code}: {response.text[:300]}")
            if response.status_code >= 400:
                raise LokiError(f"{response.status_code}: {response.text[:300]}")
            decoded: JsonDict = response.json()
            return decoded

        return await resilient_call(
            attempt,
            policy=self._policy,
            breaker=self._breaker,
            retry_on=(LokiUnavailableError, httpx.TransportError, TimeoutError),
        )


async def query_logs(params: LogsInput, client: LokiClient) -> LogExcerpt:
    """Recent log lines for a namespace, pod or container."""
    end = datetime.now(UTC)
    start = end - timedelta(minutes=params.since_minutes)
    payload = await client.query_range(
        build_query(params), start=start, end=end, limit=params.limit
    )

    lines: list[LogLine] = []
    for stream in (payload.get("data") or {}).get("result") or []:
        for entry in stream.get("values") or []:
            if len(entry) < 2:
                continue
            raw_timestamp, message = entry[0], entry[1]
            lines.append(
                LogLine(timestamp=_nanos_to_iso(raw_timestamp), message=str(message).rstrip("\n"))
            )

    # Loki returns newest first per stream; across streams the order is not
    # guaranteed, so sort rather than trust it.
    lines.sort(key=lambda line: line.timestamp, reverse=True)
    trimmed = lines[: params.limit]

    note = None
    if not trimmed:
        note = "no log lines matched; the pod may have been garbage collected or never logged"
    elif len(lines) > params.limit:
        note = f"showing the newest {params.limit} of {len(lines)} lines"

    return LogExcerpt(namespace=params.namespace, pod=params.pod, lines=trimmed, note=note)


def _nanos_to_iso(raw: Any) -> str:
    try:
        return datetime.fromtimestamp(int(raw) / 1_000_000_000, tz=UTC).isoformat()
    except (TypeError, ValueError):
        return str(raw)
