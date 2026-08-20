"""The catalogue of things that must not reach a model provider.

Every pattern here is targeted. There is deliberately no generic "high entropy
string" rule, because the strings a triage agent needs most look exactly like
secrets: image digests (`sha256:...`), resource UIDs, ReplicaSet hashes. A rule
broad enough to catch an unknown secret would eat all three and leave the model
reasoning about `<redacted>` crashed at `<redacted>`.

The trade is stated rather than hidden: a credential in a shape nobody has seen
before passes through. `docs/threat-model.md` carries that residual risk, and
adding a pattern here is cheap when a new shape shows up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Pattern:
    """One redaction rule.

    Attributes:
        kind: Groups matches for aliasing. Two matches of the same kind and the
            same text always get the same placeholder.
        regex: Must expose the sensitive text as group "secret" when only part
            of the match should be replaced. Otherwise the whole match goes.
    """

    kind: str
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Order matters: the first pattern to claim a span wins, so the specific and
# unambiguous shapes are listed before the general ones.
PATTERNS: tuple[Pattern, ...] = (
    # A JWT is three base64url segments. Unmistakable, and always a credential.
    Pattern("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")),
    # Vendor-prefixed keys. Each prefix is registered and unambiguous.
    Pattern(
        "vendor-key",
        re.compile(
            r"\b("
            r"sk-[A-Za-z0-9_-]{16,}"
            r"|ghp_[A-Za-z0-9]{20,}"
            r"|gho_[A-Za-z0-9]{20,}"
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|AIza[0-9A-Za-z_-]{35}"
            r"|glpat-[A-Za-z0-9_-]{20,}"
            r")"
        ),
    ),
    # Authorization headers, however the log happens to format them.
    Pattern(
        "auth-header",
        _compile(
            r"(?:authorization|proxy-authorization)\s*[:=]\s*[\"']?"
            # An optional scheme word plus the credential itself. Bounded at
            # whitespace on purpose: an unbounded value swallows the rest of the
            # log line and takes every other field down with it.
            r"(?P<secret>(?:[A-Za-z]+\s+)?[A-Za-z0-9._~+/=-]{8,})"
        ),
    ),
    Pattern("bearer", _compile(r"\bbearer\s+(?P<secret>[A-Za-z0-9._~+/=-]{8,})")),
    # The password inside a connection string, leaving scheme and host readable
    # because "cannot reach the database host" is the diagnosis.
    Pattern(
        "url-password",
        re.compile(r"(?<=://)(?P<user>[^:/\s@]+):(?P<secret>[^@/\s]+)(?=@)"),
    ),
    # A named secret field. Covers the overwhelming majority of real leaks in
    # logs and Kubernetes events.
    Pattern(
        "secret-assignment",
        _compile(
            r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|apikey"
            r"|access[_-]?key|private[_-]?key|encryption[_-]?key|client[_-]?secret)"
            r"\s*[:=]\s*[\"']?(?P<secret>[^\s,;\"'}]{4,})"
        ),
    ),
    Pattern("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # IPv4, excluding the addresses that carry no information about this
    # cluster and are useful to keep readable.
    Pattern("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

# Left alone by the IP rule: redacting these tells an attacker nothing and
# costs the model the ability to recognise a loopback or unbound listener.
IP_ALLOWLIST = frozenset({"0.0.0.0", "127.0.0.1", "255.255.255.255", "1.1.1.1", "8.8.8.8"})

# Guards against replacing something that only looks like a secret. Checked
# against the text immediately before a match.
DIGEST_PREFIX = re.compile(r"(?:sha256|sha512|md5)\s*[:=]\s*$", re.IGNORECASE)

# Structured data does not look like `password=hunter2`; it looks like
# {"data": {"password": "aHVudGVyMg=="}}. The value carries no recognisable
# shape, so the *key* is what gives it away. Any value under a key matching this
# is replaced whole, whatever it contains.
# A bare `key` is deliberately absent. In Kubernetes it is almost always
# structural: a taint key on a toleration, a filename in a ConfigMap projection,
# a map entry name. Redacting those hides why a pod will not schedule, which is
# exactly the diagnosis this agent exists to make. Measured against a real pod,
# not guessed: it fired three times on one object, all three false positives.
# Compound forms (api_key, encryption_key) still match, and a bare `key` holding
# a real credential is still caught by the value patterns above.
SECRET_KEY_NAMES = re.compile(
    r"^(?:"
    r"(?:.*[_-])?(?:password|passwd|pwd|secret|token|apikey|credential|credentials"
    r"|auth|authorization|cert|certificate|privatekey)s?"
    r"|.+[_-]keys?"
    r")$",
    re.IGNORECASE,
)

# Exceptions to the rule above. These end in a matching word but name a lookup,
# not a credential, and losing them costs real diagnostic signal.
SECRET_KEY_EXCEPTIONS = frozenset(
    {
        "secretname",
        "secretkeyref",
        "secretref",
        "keyref",
        "configmapkeyref",
        "publickey",
        "hostkey",
        "sortkey",
        "partitionkey",
        "idempotencykey",
    }
)
