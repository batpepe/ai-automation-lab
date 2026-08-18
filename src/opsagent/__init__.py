"""Incident triage agent for a self-hosted K3s cluster.

The package is deliberately layered so the safety properties are structural
rather than conventional: tool output is redacted at the tool boundary, the
agent loop only ever sees redacted data, and the provider layer is swappable so
the whole system runs with no API key and no spend.
"""

__version__ = "0.1.0"
