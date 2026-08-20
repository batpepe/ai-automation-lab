"""The single point every tool result passes through before a model sees it.

Placement is the design. Redaction lives at the tool boundary rather than in
front of the prompt, so a future caller of the tool layer cannot forget it and
there is exactly one place to audit.

Redaction preserves identity rather than erasing it. The same address always
becomes the same `<ip-1>`, so the model can still reason that the pod at
`<ip-1>` cannot reach `<ip-2>` and correlate that across a log excerpt and an
event. Masking every match to a single `<redacted>` would destroy exactly the
structure a root cause is made of.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from opsagent.redaction.patterns import (
    DIGEST_PREFIX,
    IP_ALLOWLIST,
    PATTERNS,
    SECRET_KEY_EXCEPTIONS,
    SECRET_KEY_NAMES,
)

# Domains whose names describe this cluster's internal topology. Overridable so
# the layer is not hard-wired to one homelab.
DEFAULT_INTERNAL_DOMAINS: tuple[str, ...] = (
    "batpepe.online",
    "svc.cluster.local",
    "svc",
    "local",
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Redacted content plus how much was replaced."""

    value: Any
    count: int


@dataclass
class _Span:
    start: int
    end: int
    kind: str
    text: str
    # Index in PATTERNS. Decides ties, so `url-password` beats `email` on
    # postgres://user:pass@host, where both start at the same character.
    priority: int


@dataclass
class Redactor:
    """Replaces sensitive text with stable, kind-scoped placeholders.

    One instance per investigation. The alias table is instance state, which is
    what makes `<ip-1>` mean the same host in every tool result of that run and
    nothing at all outside it.
    """

    internal_domains: Sequence[str] = DEFAULT_INTERNAL_DOMAINS
    _aliases: dict[tuple[str, str], str] = field(default_factory=dict, init=False)
    _counters: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._host_pattern = self._build_host_pattern(self.internal_domains)

    @staticmethod
    def _build_host_pattern(domains: Sequence[str]) -> re.Pattern[str] | None:
        if not domains:
            return None
        # Longest first so that svc.cluster.local wins over a bare svc suffix.
        ordered = sorted(domains, key=len, reverse=True)
        alternatives = "|".join(re.escape(domain) for domain in ordered)
        return re.compile(rf"\b[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:{alternatives})\b")

    @property
    def aliases(self) -> dict[str, str]:
        """Placeholder to original value. Never leaves the process."""
        return {alias: original for (_, original), alias in self._aliases.items()}

    def _alias(self, kind: str, original: str) -> str:
        key = (kind, original)
        existing = self._aliases.get(key)
        if existing is not None:
            return existing
        self._counters[kind] = self._counters.get(kind, 0) + 1
        alias = f"<{kind}-{self._counters[kind]}>"
        self._aliases[key] = alias
        return alias

    def redact_text(self, text: str) -> RedactionResult:
        """Replace every recognised secret in one string."""
        spans: list[_Span] = []

        for priority, pattern in enumerate(PATTERNS):
            for match in pattern.regex.finditer(text):
                secret = match.groupdict().get("secret")
                if secret is not None:
                    start, end = match.span("secret")
                else:
                    start, end = match.span()
                    secret = match.group()

                if pattern.kind == "ip" and secret in IP_ALLOWLIST:
                    continue
                # An image digest is not a credential, and it is the single most
                # useful field when a pod will not start.
                if DIGEST_PREFIX.search(text[max(0, start - 12) : start]):
                    continue
                spans.append(_Span(start, end, pattern.kind, secret, priority))

        if self._host_pattern is not None:
            for match in self._host_pattern.finditer(text):
                spans.append(
                    _Span(match.start(), match.end(), "host", match.group(), len(PATTERNS))
                )

        return RedactionResult(*self._apply(text, spans))

    def _apply(self, text: str, spans: list[_Span]) -> tuple[str, int]:
        if not spans:
            return text, 0

        # Earliest match wins; ties go to the more specific pattern, then to the
        # longer match. A span inside one already claimed is dropped rather than
        # replaced twice.
        spans.sort(key=lambda span: (span.start, span.priority, -(span.end - span.start)))
        claimed: list[_Span] = []
        cursor = -1
        for span in spans:
            if span.start >= cursor:
                claimed.append(span)
                cursor = span.end

        rebuilt = []
        position = 0
        for span in claimed:
            rebuilt.append(text[position : span.start])
            rebuilt.append(self._alias(span.kind, span.text))
            position = span.end
        rebuilt.append(text[position:])
        return "".join(rebuilt), len(claimed)

    def redact(self, value: Any) -> RedactionResult:
        """Walk a structure, redacting every string it contains.

        Tool results are structured, so redacting only strings passed in
        directly would leave everything nested untouched.
        """
        total = 0

        def walk(node: Any, *, key_is_secret: bool = False) -> Any:
            nonlocal total
            if isinstance(node, str):
                if key_is_secret:
                    total += 1
                    return self._alias("secret", node)
                result = self.redact_text(node)
                total += result.count
                return result.value
            if isinstance(node, dict):
                return {
                    child_key: walk(
                        child_value,
                        key_is_secret=_is_secret_key(str(child_key)),
                    )
                    for child_key, child_value in node.items()
                }
            if isinstance(node, list):
                return [walk(item, key_is_secret=key_is_secret) for item in node]
            if isinstance(node, tuple):
                return tuple(walk(item, key_is_secret=key_is_secret) for item in node)
            # Numbers, booleans and None carry nothing to redact. A secret
            # stored as an int is not a shape worth guessing at.
            return node

        redacted = walk(value)
        return RedactionResult(redacted, total)


# Kubernetes and Helm mix conventions freely: DB_PASSWORD, db-password and
# clientSecret all appear in the same pod spec. Normalising camelCase to
# snake_case first means one pattern covers all three.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _is_secret_key(key: str) -> bool:
    compact = key.replace("_", "").replace("-", "").lower()
    if compact in SECRET_KEY_EXCEPTIONS:
        return False
    return bool(SECRET_KEY_NAMES.match(_CAMEL_BOUNDARY.sub("_", key).lower()))
