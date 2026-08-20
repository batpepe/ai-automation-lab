"""Loki and Prometheus tools against faked HTTP."""

import httpx
import pytest
import respx

from opsagent.resilience import RetryPolicy
from opsagent.tools.loki import LokiClient, LokiError, build_query, escape_label_value, query_logs
from opsagent.tools.models import LogsInput, MetricsInput
from opsagent.tools.prometheus import (
    MAX_SERIES,
    PrometheusClient,
    PrometheusError,
    query_metrics,
)

pytestmark = pytest.mark.unit

LOKI = "http://loki.test"
PROM = "http://prom.test"
FAST = RetryPolicy(attempts=2, base_delay=0.0, max_delay=0.0, timeout=5.0)


# --- LogQL construction -----------------------------------------------------


def test_query_selects_namespace_pod_and_container() -> None:
    query = build_query(LogsInput(namespace="ai-lab", pod="n8n-1", container="n8n", grep="ERROR"))

    assert query == '{namespace="ai-lab", pod="n8n-1", container="n8n"} |~ "ERROR"'


def test_a_quote_in_the_filter_cannot_break_out_of_the_query() -> None:
    # The pod name and filter come from the model, so this is untrusted input
    # to a query language.
    assert escape_label_value('a"b\\c') == 'a\\"b\\\\c'
    query = build_query(LogsInput(namespace="ai-lab", grep='" or {job=~".+"}'))

    assert query.count('|~ "') == 1
    assert '\\"' in query


@pytest.mark.asyncio
@respx.mock
async def test_logs_are_parsed_newest_first() -> None:
    respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "stream": {"pod": "n8n-1"},
                            "values": [
                                ["1755691200000000000", "older line"],
                                ["1755691260000000000", "newer line"],
                            ],
                        }
                    ]
                }
            },
        )
    )

    async with LokiClient(LOKI, policy=FAST) as client:
        excerpt = await query_logs(LogsInput(namespace="ai-lab"), client)

    assert [line.message for line in excerpt.lines] == ["newer line", "older line"]
    assert excerpt.lines[0].timestamp.startswith("2025-") or excerpt.lines[0].timestamp.startswith(
        "2026-"
    )


@pytest.mark.asyncio
@respx.mock
async def test_no_matching_logs_says_so_rather_than_returning_nothing() -> None:
    # An empty result and a broken query look identical to a model unless the
    # tool says which one happened.
    respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"data": {"result": []}})
    )

    async with LokiClient(LOKI, policy=FAST) as client:
        excerpt = await query_logs(LogsInput(namespace="ai-lab"), client)

    assert excerpt.lines == []
    assert excerpt.note is not None
    assert "no log lines matched" in excerpt.note


@pytest.mark.asyncio
@respx.mock
async def test_loki_rejection_is_not_retried() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(400, text="parse error")
    )

    async with LokiClient(LOKI, policy=FAST) as client:
        with pytest.raises(LokiError):
            await query_logs(LogsInput(namespace="ai-lab"), client)

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_loki_outage_is_retried() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(503, text="unavailable")
    )

    async with LokiClient(LOKI, policy=FAST) as client:
        with pytest.raises(LokiError):
            await query_logs(LogsInput(namespace="ai-lab"), client)

    assert route.call_count == FAST.attempts


# --- Prometheus -------------------------------------------------------------


def matrix(series_count: int, samples: int = 3) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"pod": f"pod-{index}"},
                    "values": [[1755691200 + step, str(step)] for step in range(samples)],
                }
                for index in range(series_count)
            ],
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_metrics_are_parsed_into_labelled_series() -> None:
    respx.get(f"{PROM}/api/v1/query_range").mock(return_value=httpx.Response(200, json=matrix(1)))

    async with PrometheusClient(PROM, policy=FAST) as client:
        result = await query_metrics(MetricsInput(promql="up"), client)

    assert result.series[0].labels == {"pod": "pod-0"}
    assert [sample.value for sample in result.series[0].samples] == [0.0, 1.0, 2.0]


@pytest.mark.asyncio
@respx.mock
async def test_a_broad_query_is_capped_and_the_cap_is_reported() -> None:
    # An unbounded query can return more series than the whole investigation
    # budget. Silently truncating would let the model reason from a fragment it
    # believes is complete.
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=matrix(MAX_SERIES + 5))
    )

    async with PrometheusClient(PROM, policy=FAST) as client:
        result = await query_metrics(MetricsInput(promql="up"), client)

    assert len(result.series) == MAX_SERIES
    assert result.note is not None
    assert f"{MAX_SERIES} of {MAX_SERIES + 5}" in result.note


@pytest.mark.asyncio
@respx.mock
async def test_malformed_promql_surfaces_the_parse_error() -> None:
    # Worth surfacing rather than swallowing: the model can correct its own
    # query on the next tool call.
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(400, text='parse error at char 4: unexpected ")"')
    )

    async with PrometheusClient(PROM, policy=FAST) as client:
        with pytest.raises(PrometheusError, match="parse error"):
            await query_metrics(MetricsInput(promql="up)"), client)


@pytest.mark.asyncio
@respx.mock
async def test_an_empty_result_is_reported_as_such() -> None:
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"result": []}})
    )

    async with PrometheusClient(PROM, policy=FAST) as client:
        result = await query_metrics(MetricsInput(promql="up"), client)

    assert result.note is not None
    assert "no series" in result.note


@pytest.mark.asyncio
@respx.mock
async def test_a_non_numeric_sample_is_skipped_rather_than_crashing() -> None:
    # Prometheus returns NaN as a string for staleness markers.
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"metric": {}, "values": [[1.0, "NaN?"], [2.0, "5"]]}]},
            },
        )
    )

    async with PrometheusClient(PROM, policy=FAST) as client:
        result = await query_metrics(MetricsInput(promql="up"), client)

    assert [sample.value for sample in result.series[0].samples] == [5.0]
