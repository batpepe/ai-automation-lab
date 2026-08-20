# Assumptions

Everything here was assumed rather than proven, or was measured once and could
change under a dependency upgrade. Each entry says what breaks if it is wrong.

## Verified by measurement

### pydantic-settings `extra="forbid"` does not catch mistyped settings
Measured on pydantic-settings 2.15.0, 2026-08-20. `extra="forbid"` never sees a
variable like `OPSAGENT_LOG_LEVL`, because the environment source only offers
names that already match a field, so the typo silently leaves the default in
place. It does reject unrelated keys read from a `.env` file, which would make a
shared `.env` holding n8n or database credentials unusable.

Both behaviours are pinned by tests in `tests/unit/test_config.py`. The setting
is therefore `extra="ignore"` plus an explicit validator that scans the
environment for unknown `OPSAGENT_` names. If a future release changes either
behaviour, those two tests fail and this entry needs revisiting.

### The n8n chart's `config:` block mangles camelCase env var names
Measured with `helm template` against chart 2.0.1 on 2026-08-20. The chart turns
nested `config:` keys into environment variables by joining them with
underscores and uppercasing each key, without splitting camelCase. So
`executions.data.maxAge` becomes `EXECUTIONS_DATA_MAXAGE`, while n8n reads
`EXECUTIONS_DATA_MAX_AGE`. The value would have been accepted, ignored, and the
default 336 hours of execution history kept on a 1Gi volume shared with every
other database in the cluster.

The same block cannot express `EXECUTIONS_DATA_PRUNE_MAX_COUNT` at all, because
`prune` would have to be both a value and a parent key. Both limits therefore
live in the `n8n-runtime` ConfigMap and reach the container through `extraEnv`.

### The chart's `extraEnv` only renders `valueFrom` entries
Also measured, same render. An `extraEnv` entry written as a plain `value:` is
silently dropped from the Deployment rather than rejected. Everything passed
through `extraEnv` in `deploy/argocd/n8n.yaml` therefore references a Secret or
a ConfigMap, which is where the values belong anyway.

### gitleaks fails on the very first push to a new repository
Observed on the initial push, 2026-08-20. The action derives a commit range from
the push event, which for the first commit resolves to `<first-sha>^..<head>`.
That parent does not exist, git errors, and the action exits non-zero even
though its own output says "no leaks found in partial scan". A manually
triggered run scans the full history instead and passed cleanly across all 12
commits. Every push after the first works normally, so the red run in the
Actions history is that one-off and not a finding.

### A bare `key` field is structural in Kubernetes, not a secret
Measured against a live pod on 2026-08-20. Treating any field named `key` as a
credential fired three times on one ordinary pod, and all three were false
positives: two taint keys on tolerations and a filename in a ConfigMap
projection. Redacting a taint key hides why a pod will not schedule, which is
one of the fault scenarios phase 6 exists to test.

`SECRET_KEY_NAMES` now matches compound forms (`api_key`, `encryption_key`) but
not a bare `key`. A bare `key` holding a real credential is still caught by the
value patterns. The three shapes are pinned as regression tests.

This is the argument for running the tools against a real cluster before
trusting them: the fixture list had no tolerations in it, and never would have.

### A synchronous client in a thread needs its own deadline
Measured on 2026-08-20, when the Tailscale link to the cluster dropped during a
live tool run. `asyncio.timeout` cancels the awaiting task but cannot cancel the
worker thread underneath it, so the official Kubernetes client stayed parked on
a socket and the run hung well past its 15 second budget.

`OfficialClientReader` now passes `_request_timeout` into every client call, so
the blocking layer has the same deadline as the async one, and
`tests/unit/test_tools_k8s.py` pins it. The same applies to any future
synchronous dependency wrapped in `anyio.to_thread`.

## Platform facts this project builds on

Read from `devops-homelab-k3s-hybrid-cloud` at main on 2026-08-20, not from a
live cluster (the API server was unreachable from the laptop at the time).
Re-verify before phase 1 lands.

- Single-node amd64 K3s cluster. All CI images build `linux/amd64` only, so an
  arm64 node would fail to pull them.
- ArgoCD app-of-apps: any Application file added to
  `k8s-infrastructure/argocd-apps/` deploys itself. No ApplicationSet, no
  kustomize, Helm values inlined in the Application.
- Prometheus at `monitoring-kube-prometheus-prometheus.monitoring.svc:9090`,
  Loki at `loki.monitoring.svc:3100`, Postgres at `postgres-service.apps.svc:5432`.
- Loki is `loki-stack` 2.10.3, monolithic, deprecated upstream. The tool layer
  targets the plain query API so a future migration to the current chart does
  not rewrite the tools.
- Alertmanager runs kube-prometheus-stack defaults with no custom receivers.
  Telegram delivery is configured in Grafana unified alerting, not Alertmanager.
  Assumption to verify in phase 4: PrometheusRule alerts currently reach nobody.
- `PrometheusRule`, `Probe` and `ServiceMonitor` need `labels: {release: monitoring}`
  or the operator's selector ignores them. No ServiceMonitor exists yet; this
  project adds the first one.
- Storage is the K3s default `local-path` provisioner: ReadWriteOnce and pinned
  to the node, so any workload with a volume sets `strategy: Recreate`.
- The shared Postgres PVC is 1Gi. n8n execution history has to be pruned from
  the day it is deployed, not after it fills the volume.

## Decisions taken without external confirmation

- Namespace `ai-lab` for both n8n and the agent, so RBAC, secrets and a future
  NetworkPolicy share one blast radius. Deviates from the platform's
  "first-party workloads live in `apps`" habit on purpose.
- The platform repository gets exactly one new Application pointing at
  `deploy/argocd/` here, rather than one Application per component.
- Telegram reuses the existing bot in the `tg-secret` secret. No second bot.
- The report page stays cluster-internal until it can sit behind the same
  Cloudflare Access application as the n8n editor.
- Budget defaults: 200k tokens and a 0.25 USD equivalent per investigation.
  On a free tier the token cap is the one that binds.
