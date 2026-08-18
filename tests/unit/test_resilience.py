"""The retry schedule is asserted directly rather than waited for.

Sleep, clock and jitter are injected, so these tests run in microseconds and a
change to the backoff curve shows up as a failed assertion instead of a slower
suite that nobody notices.
"""

import asyncio
import random

import pytest

from opsagent.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    call,
    full_jitter,
)

pytestmark = pytest.mark.unit

# No jitter, so the recorded delays are the ceilings and the schedule is exact.
NO_JITTER = float


class FakeClock:
    """A clock the test moves by hand, and a sleep that records instead of waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Flaky:
    """Fails for the first `failures` calls, then succeeds."""

    def __init__(self, failures: int, error: BaseException | None = None) -> None:
        self.failures = failures
        self.error = error if error is not None else ConnectionError("boom")
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "ok"


@pytest.mark.asyncio
async def test_success_on_first_attempt_does_not_sleep() -> None:
    clock = FakeClock()
    operation = Flaky(failures=0)

    result = await call(operation, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == "ok"
    assert operation.calls == 1
    assert clock.slept == []


@pytest.mark.asyncio
async def test_retries_then_succeeds() -> None:
    clock = FakeClock()
    operation = Flaky(failures=2)

    result = await call(
        operation,
        policy=RetryPolicy(attempts=3, base_delay=0.2, max_delay=5.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=NO_JITTER,
    )

    assert result == "ok"
    assert operation.calls == 3
    # Exponential from base_delay, one sleep between each pair of attempts.
    assert clock.slept == [0.2, 0.4]


@pytest.mark.asyncio
async def test_backoff_is_capped_by_max_delay() -> None:
    clock = FakeClock()
    operation = Flaky(failures=99)

    with pytest.raises(ConnectionError, match="boom"):
        await call(
            operation,
            policy=RetryPolicy(attempts=6, base_delay=1.0, max_delay=2.0),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            jitter=NO_JITTER,
        )

    # 1, 2, then flat at the cap rather than growing without bound.
    assert clock.slept == [1.0, 2.0, 2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_last_failure_propagates_when_attempts_run_out() -> None:
    clock = FakeClock()
    operation = Flaky(failures=99, error=ConnectionError("still down"))

    with pytest.raises(ConnectionError, match="still down"):
        await call(
            operation,
            policy=RetryPolicy(attempts=2),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            jitter=NO_JITTER,
        )

    assert operation.calls == 2


@pytest.mark.asyncio
async def test_unlisted_exception_is_not_retried() -> None:
    # A rejected request means the payload is wrong. Sending it again wastes
    # the dependency's time and the investigation's budget.
    clock = FakeClock()
    operation = Flaky(failures=99, error=ValueError("bad request"))

    with pytest.raises(ValueError, match="bad request"):
        await call(
            operation,
            policy=RetryPolicy(attempts=5),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert operation.calls == 1
    assert clock.slept == []


@pytest.mark.asyncio
async def test_timeout_is_enforced_per_attempt() -> None:
    async def hangs() -> str:
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(TimeoutError):
        await call(hangs, policy=RetryPolicy(attempts=1, timeout=0.01))


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_and_blocks_further_calls() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after=30.0)
    operation = Flaky(failures=99)

    with pytest.raises(ConnectionError, match="boom"):
        await call(
            operation,
            policy=RetryPolicy(attempts=2),
            breaker=breaker,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            jitter=NO_JITTER,
        )

    assert breaker.is_open
    calls_before = operation.calls

    # The dependency is not contacted again while the circuit is open.
    with pytest.raises(CircuitOpenError):
        await call(
            operation,
            breaker=breaker,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert operation.calls == calls_before


@pytest.mark.asyncio
async def test_breaker_allows_a_trial_after_the_reset_window() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30.0)
    breaker.record_failure(clock.monotonic())
    assert breaker.is_open

    clock.now = 31.0
    operation = Flaky(failures=0)

    result = await call(
        operation,
        breaker=breaker,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result == "ok"
    assert not breaker.is_open


@pytest.mark.asyncio
async def test_rejected_requests_do_not_count_against_the_breaker() -> None:
    # Dependency health and payload validity are different questions. A 400
    # answered five times must not stop the agent reading a healthy service.
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1)
    operation = Flaky(failures=99, error=ValueError("bad request"))

    with pytest.raises(ValueError, match="bad request"):
        await call(
            operation,
            breaker=breaker,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert not breaker.is_open


@pytest.mark.asyncio
async def test_success_resets_the_failure_count() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure(clock.monotonic())

    await call(Flaky(failures=0), breaker=breaker, monotonic=clock.monotonic)

    breaker.record_failure(clock.monotonic())
    assert not breaker.is_open


def test_a_failed_trial_reopens_the_circuit_immediately() -> None:
    # The half-open trial must not hand back a fresh budget of failures, or a
    # dead dependency gets hammered once per reset window times the threshold.
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_after=30.0)
    for _ in range(3):
        breaker.record_failure(clock.monotonic())
    clock.now = 31.0

    breaker.check(clock.monotonic())
    breaker.record_failure(clock.monotonic())

    assert breaker.is_open


@pytest.mark.parametrize("attempts", [0, -1])
def test_policy_rejects_impossible_attempt_counts(attempts: int) -> None:
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=attempts)


def test_full_jitter_stays_within_the_ceiling() -> None:
    rng = random.Random(0)

    values = [full_jitter(2.0, rng=rng) for _ in range(100)]

    assert all(0.0 <= value <= 2.0 for value in values)
    # A constant delay would defeat the point of jitter.
    assert len(set(values)) > 1
