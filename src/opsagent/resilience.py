"""Timeout, bounded retry with jitter, and a circuit breaker for outbound calls.

Every call that leaves this process goes through `call`. That is an invariant
rather than a style preference. An investigation that hangs on a wedged Loki
query holds its budget open and never reports; a retry storm aimed at a
single-node cluster is indistinguishable from the outage it is investigating.

The clock, the sleep and the jitter are injected so the tests assert on the
retry schedule directly instead of waiting for it.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

# Transport failures and timeouts are worth another attempt. A rejected request
# is not: retrying a 400 just sends the same broken payload again.
DEFAULT_RETRY_ON: tuple[type[BaseException], ...] = (TimeoutError, OSError)


class CircuitOpenError(RuntimeError):
    """Raised instead of calling a dependency that is currently failing."""


_DEFAULT_RNG = random.Random()


def full_jitter(ceiling: float, *, rng: random.Random | None = None) -> float:
    """Pick a delay uniformly in [0, ceiling].

    Full jitter rather than exponential-plus-noise: when several tool calls fail
    against the same dependency at once, this is what stops them retrying in
    lockstep and rebuilding the spike that knocked it over.
    """
    source = rng if rng is not None else _DEFAULT_RNG
    return source.uniform(0.0, ceiling)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many attempts to make, how long to wait, and how long to allow each."""

    attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 5.0
    timeout: float | None = 10.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must not be negative")

    def backoff_ceiling(self, attempt: int) -> float:
        """Upper bound on the wait before the attempt after this one."""
        # 2.0 rather than 2: typeshed types `int ** int` as Any, because a
        # negative exponent yields a float, and that Any would propagate out.
        return min(self.max_delay, self.base_delay * 2.0**attempt)


@dataclass
class CircuitBreaker:
    """Stops calling a dependency that has failed repeatedly.

    Deliberately per-dependency rather than global: Loki being down should not
    stop the agent reading pod status, because a partial investigation with a
    stated gap is far more useful than none.
    """

    failure_threshold: int = 5
    reset_after: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def check(self, now: float) -> None:
        """Raise if the circuit is open, or let one trial call through."""
        if self._opened_at is None:
            return
        if now - self._opened_at < self.reset_after:
            raise CircuitOpenError(
                f"circuit open after {self._failures} failures, "
                f"retrying in {self.reset_after - (now - self._opened_at):.1f}s"
            )
        # Half-open: allow a single trial. The failure count stays one below the
        # threshold so that a failed trial reopens the circuit immediately
        # rather than granting a fresh full budget of attempts.
        self._opened_at = None
        self._failures = self.failure_threshold - 1

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, now: float) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = now


async def call[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRY_ON,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[float], float] = full_jitter,
) -> T:
    """Run `operation` under a timeout, bounded retries and an optional breaker.

    Args:
        operation: A zero-argument coroutine function. Called once per attempt,
            so it must be safe to invoke more than once.
        policy: Attempt count, backoff and per-attempt timeout.
        breaker: Shared per-dependency breaker. Omit for a one-off call.
        retry_on: Exception types worth another attempt. Everything else
            propagates immediately and is not counted against the breaker,
            because a rejected request says nothing about dependency health.
        sleep: Injected for tests; defaults to `asyncio.sleep`.
        monotonic: Injected for tests; defaults to `time.monotonic`.
        jitter: Injected for tests; defaults to full jitter.

    Returns:
        Whatever `operation` returned.

    Raises:
        CircuitOpenError: The breaker is open, so no call was made.
        BaseException: The last failure, once the attempts are exhausted.
    """
    effective_policy = policy if policy is not None else RetryPolicy()
    effective_sleep = sleep if sleep is not None else asyncio.sleep
    last_error: BaseException | None = None

    for attempt in range(effective_policy.attempts):
        if breaker is not None:
            breaker.check(monotonic())
        try:
            if effective_policy.timeout is None:
                result = await operation()
            else:
                async with asyncio.timeout(effective_policy.timeout):
                    result = await operation()
        except retry_on as error:
            last_error = error
            if breaker is not None:
                breaker.record_failure(monotonic())
            if attempt == effective_policy.attempts - 1:
                break
            await effective_sleep(jitter(effective_policy.backoff_ceiling(attempt)))
        else:
            if breaker is not None:
                breaker.record_success()
            return result

    if last_error is None:  # pragma: no cover - attempts >= 1 makes this unreachable
        raise RuntimeError("retry loop ended without a result or an error")
    raise last_error
