# Assumptions

Everything here was assumed rather than proven, or was measured once and could
change under a dependency upgrade. Each entry says what breaks if it is wrong.

## Verified by measurement

### pydantic-settings `extra="forbid"` does not catch mistyped settings
Measured on pydantic-settings 2.15.0, 2026-08-18. `extra="forbid"` never sees a
variable like `OPSAGENT_LOG_LEVL`, because the environment source only offers
names that already match a field, so the typo silently leaves the default in
place. It does reject unrelated keys read from a `.env` file, which would make a
shared `.env` holding n8n or database credentials unusable.

Both behaviours are pinned by tests in `tests/unit/test_config.py`. The setting
is therefore `extra="ignore"` plus an explicit validator that scans the
environment for unknown `OPSAGENT_` names. If a future release changes either
behaviour, those two tests fail and this entry needs revisiting.

## Platform facts this project builds on

Read from `devops-homelab-k3s-hybrid-cloud` at main on 2026-08-18, not from a
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
