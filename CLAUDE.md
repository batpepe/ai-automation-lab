## Approach
- Read files before writing. Don't re-read unchanged files.
- Thorough in reasoning, concise in output.
- No preamble, closing fluff, or sycophancy.
- No emojis, em-dashes, or smart quotes in files or commits.
- Verify SHAs, versions, flags, endpoints and API shapes by reading code or docs before asserting.
- Targeted edits over rewrites.
- Comments say why, not what. Every non-obvious config line carries its reason.

## What this is
An incident triage agent for the K3s homelab described in the sibling repo
devops-homelab-k3s-hybrid-cloud. Alertmanager fires, n8n dedupes and calls this
service, the agent investigates with read-only cluster tools, and the result is
persisted and later scored against the human-confirmed root cause. The measured
accuracy is the point of the project, not the agent.

## Stack
Python 3.12 + uv | FastAPI | Pydantic v2 | SQLAlchemy 2 + PostgreSQL | typer | structlog
MCP tool layer | n8n as a GitOps workload | ArgoCD app-of-apps | Prometheus + Loki + Grafana

## Key paths
- plan.md                          phases, definitions of done, pushback on the brief
- src/opsagent/config.py           settings, OPSAGENT_ prefix, safe defaults
- src/opsagent/observability/      logging now, metrics and probes in phase 5
- src/opsagent/tools/              cluster tools, one registry, redacted outputs (phase 2)
- src/opsagent/redaction/          the choke point every tool output passes through (phase 2)
- src/opsagent/providers/          LLMProvider protocol, mock default, no paid keys (phase 3)
- src/opsagent/agent/              loop, budget and depth caps, TriageReport (phase 3)
- src/opsagent/n8n/                public API client and workflow export/import (phase 1)
- workflows/                       n8n workflow JSON, the source of truth for the instance
- deploy/argocd/                   child Applications; the platform repo points one app here
- evals/scenarios/                 fault injection scenarios and expected root causes (phase 6)
- docs/adr/                        decisions and the alternatives rejected

## Commands
- uv sync && uv run pytest
- uv run ruff check . && uv run ruff format --check .
- uv run mypy
- uv run opsagent show-config

## Invariants, do not weaken without an ADR
- The default provider is the mock. A clean clone runs end to end with no API key and no spend.
- Redaction happens at the tool boundary, before anything reaches a prompt. Never in the agent.
- The agent's ServiceAccount is read-only: no secret reads, no write verbs. The eval harness
  carries its own separate write credential; the agent never gets one.
- The model proposes, code executes. No remediation path from the model in v1.
- Every external call has a timeout, bounded retry with jitter, and a circuit breaker.
  There is no bare `await client.get(...)` anywhere.
- Budget, tool-call depth and wall-clock caps are enforced in the loop and each has a test.
- Log lines are untrusted input. Tool output is delimited and marked as data in the prompt.
- Tests never touch the network or the cluster. Faked Kubernetes API and faked HTTP only.

## Constraints inherited from the platform
- Single amd64 K3s node. Images build linux/amd64 only.
- ArgoCD runs prune=true, selfHeal=true. Out-of-band kubectl edits get reverted.
- Manifest image tags are immutable commit SHAs. `latest` is never referenced.
- PrometheusRule, Probe and ServiceMonitor objects need `labels: {release: monitoring}`
  or the operator ignores them.
- Prometheus: monitoring-kube-prometheus-prometheus.monitoring.svc:9090
- Loki: loki.monitoring.svc:3100 (loki-stack 2.10.3, monolithic, no gateway)
- Postgres: postgres-service.apps.svc:5432
- Secrets are SealedSecrets in git. Nothing plaintext, no manual kubectl create secret.
- Direct pushes to main are the project convention; do not open PRs unless asked.

## Verification before claiming done
- ruff, mypy and pytest green locally, then the CI run green.
- Manifests: `kubectl diff` clean and the owning ArgoCD app Healthy/Synced after sync.
- Anything asserted about the cluster is read from the cluster or the manifests, not recalled.
- A phase is done when its definition of done in plan.md is ticked, then stop and report.
