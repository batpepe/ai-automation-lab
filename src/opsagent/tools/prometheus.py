"""PromQL range queries against the cluster's existing Prometheus.

Bounded on purpose. An unbounded query over a long window can return more
series than an investigation's whole token budget, so the number of series and
the samples per series are capped here and the model is told when that happened.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx

from opsagent.resilience import CircuitBreaker, RetryPolicy
from opsagent.resilience import call as resilient_call
from opsagent.tools.models import MetricResult, MetricSample, MetricSeries, MetricsInput

JsonDict = dict[str, Any]

MAX_SERIES = 20
MAX_SAMPLES_PER_SERIES = 60


class PrometheusError(RuntimeError):
    """Prometheus rejected the query, usually a PromQL syntax error."""


class PrometheusUnavailableError(PrometheusError):
    """Prometheus failed in a way another attempt might survive."""


class PrometheusClient:
    """Minimal read-only client for the Prometheus HTTP API."""

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
        self, query: str, *, start: datetime, end: datetime, step: str
    ) -> JsonDict:
        async def attempt() -> JsonDict:
            response = await self._client.get(
                "/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": step,
                },
            )
            if response.status_code >= 500:
                raise PrometheusUnavailableError(f"{response.status_code}: {response.text[:300]}")
            if response.status_code >= 400:
                # A 400 here is almost always malformed PromQL. The message is
                # worth surfacing: the model can correct its own query.
                raise PrometheusError(f"{response.status_code}: {response.text[:300]}")
            decoded: JsonDict = response.json()
            return decoded

        return await resilient_call(
            attempt,
            policy=self._policy,
            breaker=self._breaker,
            retry_on=(PrometheusUnavailableError, httpx.TransportError, TimeoutError),
        )


async def query_metrics(params: MetricsInput, client: PrometheusClient) -> MetricResult:
    """Evaluate a PromQL expression over a bounded window."""
    end = datetime.now(UTC)
    start = end - timedelta(minutes=params.since_minutes)
    payload = await client.query_range(params.promql, start=start, end=end, step=params.step)

    if payload.get("status") != "success":
        raise PrometheusError(str(payload.get("error", "prometheus reported a failure")))

    raw_series = (payload.get("data") or {}).get("result") or []
    series: list[MetricSeries] = []
    for item in raw_series[:MAX_SERIES]:
        samples = [
            MetricSample(timestamp=float(point[0]), value=float(point[1]))
            for point in (item.get("values") or [])[-MAX_SAMPLES_PER_SERIES:]
            if len(point) >= 2 and _is_number(point[1])
        ]
        series.append(MetricSeries(labels=dict(item.get("metric") or {}), samples=samples))

    notes = []
    if len(raw_series) > MAX_SERIES:
        notes.append(f"showing {MAX_SERIES} of {len(raw_series)} series")
    if not raw_series:
        notes.append("the query returned no series")
    return MetricResult(query=params.promql, series=series, note="; ".join(notes) or None)


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
