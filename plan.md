# ai-automation-lab - plan.md

> On approval, this document is committed verbatim as `plan.md` in the new `~/ai-automation-lab` repo (step 0.0).
> Grounded in: exploration of `devops-homelab-k3s-hybrid-cloud` + both Python labs (2026-08-18), n8n public API OpenAPI spec, MCP Python SDK docs, current Anthropic API reference.

## Context

Third portfolio lab (after `devops-homelab-k3s-hybrid-cloud` and `qa-engineering-lab`) targeting AI Automation Engineer roles. The single thesis to prove: **an LLM can be put into a real operational loop safely** - with a real tool layer, guardrails, cost accounting, and *measured* accuracy - running against the author's production K3s cluster. Every decision optimises for that thesis over feature count.

Flagship: an incident-triage agent triggered by Alertmanager through n8n, investigating via read-only cluster telemetry tools, producing a structured hypothesis, persisted and later scored against the human-confirmed root cause.

## Ground truth about the platform (from exploration - the plan builds on these facts)

- **GitOps pattern:** classic app-of-apps; root Application watches `k8s-infrastructure/argocd-apps/` in the platform repo; child Application files dropped there deploy automatically. No ApplicationSet, no kustomize, no separate values files - **Helm values are inlined in the Application** (monitoring-stack is the precedent). All `project: default`, `prune+selfHeal`.
- **Secrets:** *no* sealed-secrets / SOPS / external-secrets exist. Manual `kubectl create secret` with the create-command as a comment above the consumer, plus rows in `SECURITY.md` and `docs/runbooks/rotate-secrets.md`. Migration to sealed-secrets is ROADMAP item #1 - resolved for this project by decision 1 (introduce sealed-secrets now, new secrets only).
- **Tunnel:** cloudflared (token-based, remote-managed) → one wildcard ingress rule `*.batpepe.online` → `traefik.kube-system.svc:80`. A host is public **iff** it's in `local.tunnel_hosts` (terraform/cloudflare) **and** has an Ingress. **No Cloudflare Access / Zero Trust policies exist**; admin UIs (ArgoCD `argocd.local`, Grafana) are protected by *not* having DNS records. Resolved by decision 2 (first Access policy, for n8n).
- **Monitoring:** kube-prometheus-stack 58.2.2, release `monitoring`, ns `monitoring`. Prometheus: `http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090`. Alertmanager: `http://monitoring-kube-prometheus-alertmanager.monitoring.svc:9093` - **running chart defaults, no custom receivers**: Telegram delivery is provisioned in *Grafana unified alerting* (contact point `tg_bot` from secret `tg-secret`), not Alertmanager. Loki: deprecated `loki-stack` 2.10.3, monolithic, query at `http://loki.monitoring.svc:3100`. Dashboards via sidecar ConfigMaps labelled `grafana_dashboard: "1"`. PrometheusRules/Probes **must** carry `labels: {release: monitoring}`. **No ServiceMonitor exists anywhere yet** - the agent introduces the first one.
  - *Finding to verify in phase 4:* PrometheusRule-based alerts (e.g. `FlaskAppDown`) appear to route nowhere from Alertmanager today (default null-ish config; Telegram flow is Grafana-side). Wiring an Alertmanager webhook receiver for the agent turns a currently dead-end path into a used one without touching the Grafana→Telegram flow.
- **Postgres:** single-replica Deployment `postgres:15-alpine` in ns `apps`, 1Gi local-path PVC, Service `postgres-service.apps.svc:5432`, one shared `devops` superuser for every app, DBs created manually via port-forward (a ROADMAP item wants this to become a Job). No per-app roles exist.
- **Cluster:** single amd64 K3s node (all CI builds are linux/amd64-only), reached over Tailscale; Traefik bundled ingress (default class, annotation style); storage = default `local-path` RWO; workloads with PVCs set `strategy: Recreate`; requests+limits on everything; probes on DB-free paths; **no NetworkPolicies, no securityContexts** (both are open roadmap items). AWS side currently destroyed - everything runs on the local node.
- **CI:** GHCR (public images, no pull secrets), per-app path-filtered workflows: build → Trivy (CRITICAL/HIGH gate, `ignore-unfixed`) → smoke test → `sed` image SHA into manifest → `gitops:` commit with pull-rebase-push retry loop (ADR-0003: Image Updater rejected deliberately). Actions SHA-pinned. gitleaks, commitlint, hadolint, yamllint, kube-linter, dependency-review in place.
- **Conventions:** one YAML per app with every resource `---`-separated; third-party apps get their own namespace declared inline; first-party go to `apps`; minimal `app: <name>` labels; immutable SHA tags only (`:latest` never referenced); dense "why"-comments; Conventional Commits (types incl. `gitops`), direct pushes to main; new workloads owe entries to README, SECURITY.md, rotate-secrets runbook, ROADMAP, LEARNING_LOG, and an ADR when structure changes.
- **Python labs conventions (both repos):** uv + committed lock, `requires-python = ">=3.12,<3.13"`, ruff `line-length=100` `select=["E","F","I","B","UP","SIM"(,"PT")]`, pytest `--strict-markers -ra` with documented markers, hatchling for importable code, `setup-uv@v5` with lockfile-keyed cache, MIT (2026 Kostiantyn Osmakov), English READMEs (badges → narrative → tables → real output → design-decisions section → author footer), lowercase scoped subject-only commits that state reasoning.
- **Telegram:** a working bot already exists (`tg-secret` in `monitoring`: `bottoken`, `chatid`) - reused for the agent's notifications; no new bot.

## Repo & deployment topology (proposed)

New public repo **`ai-automation-lab`** (same account as the other labs). Runtime manifests live in it, self-contained. The platform repo gets **one** new file in `k8s-infrastructure/argocd-apps/`: `ai-automation-lab.yaml` - a single Application pointing at `ai-automation-lab/deploy/argocd/`, which holds the lab's own child Applications (n8n, opsagent, sync hook). One block in the platform, everything else reviewable in the lab repo; exactly the established root-app mechanism, one level deeper.

Everything runs in a dedicated namespace **`ai-lab`** (n8n is third-party → own-namespace rule; the agent joins it so RBAC, secrets and the future NetworkPolicy stay in one blast-radius). Deviation from the "first-party → `apps`" precedent is deliberate and documented (ADR-001): the lab's manifests come from a different repo and form one system.

**Platform-repo touchpoint checklist (kept minimal, one PR per phase at most):** the Application file; sealed-secrets controller Application (decision 1); `tunnel_hosts += "n8n"` + Access application/policy in `terraform/cloudflare/` (decision 2); Alertmanager webhook receiver in monitoring-stack values (phase 4); rows in SECURITY.md + rotate-secrets; README/ROADMAP/LEARNING_LOG entries.

## Phase breakdown & definitions of done

### Phase 0 - Skeleton
Repo `~/ai-automation-lab`, `uv`-managed, Python 3.12, package `src/opsagent/`.

**DoD**
- [x] `uv sync && uv run pytest` green; `ruff check`, `ruff format --check` and `mypy --strict src/` clean. 16 tests, not a placeholder: config defaults and overrides, the tri-state log renderer, and the CLI.
- [x] `pyproject.toml` per lab conventions above (hatchling, ruff+`PT`, strict markers); mypy `--strict` scoped to `src/` (stricter than the older labs - completes the direction qa-lab started), relaxed for `tests/`.
- [x] CI per house pattern: `setup-uv` + lockfile cache, `uv sync --locked`, concurrency-cancel, least-privilege permissions, timeouts, **SHA-pinned actions** (platform convention wins over the labs' tag-pinning - this repo carries security posture), gitleaks + commitlint jobs copied from the platform. Two deliberate deviations: `setup-uv` is pinned to the current v10.0.1 rather than the qa-lab's v5, and there is no path filter because the repository is a single project, so every file can change the result.
- [x] `CLAUDE.md` for future sessions (project map, commands, guardrail invariants, "no emojis/em-dashes" and comment-style rules inherited from the platform).
- [x] `plan.md` (this doc), `docs/assumptions.md` seeded, README with mermaid architecture, MIT license.
- [ ] `.env.example` - blocked by a local permission rule covering `.env*`. Content is settled; it needs one command to land.
- [ ] CI observed green on `main` - needs the GitHub remote, which is an outward-facing step and is left for explicit approval.

**Measured during the phase, not assumed:** `extra="forbid"` in pydantic-settings does not catch a mistyped `OPSAGENT_` variable and does reject unrelated keys in a shared `.env`, so it costs the protection it appears to give. Replaced with an explicit validator; both behaviours are pinned by tests and written up in `docs/assumptions.md`.

### Phase 1 - n8n as a GitOps workload + workflow sync tooling
n8n via community chart **`8gears/n8n-helm-chart`** (no official chart exists), deployed platform-style: an ArgoCD Application with **inline Helm values** (the monitoring-stack precedent), pinned chart version, ns `ai-lab`. Postgres backend (`DB_TYPE=postgresdb` → shared instance, dedicated `n8n` DB + non-superuser role via the provisioning Job - decision 3), small PVC for `/home/node/.n8n` with `strategy: Recreate` (RWO discipline), execution pruning on (`EXECUTIONS_DATA_PRUNE`, bounded age/count - the shared Postgres PVC is 1Gi), resources sized honestly (n8n will be the heaviest workload on the node; expect ~300-500Mi), probes on `/healthz`, amd64 image pinned by version.

Secrets (`N8N_ENCRYPTION_KEY`, DB creds, n8n API key for the sync job, provider API key): **SealedSecret resources in git** (decision 1) - sealed-secrets controller added to the platform's `argocd-apps/` first, `kubeseal` in the dev toolchain, rotation procedure documented in the platform runbook.

Exposure (decision 2): `n8n.batpepe.online` in `tunnel_hosts` + the account's **first Cloudflare Access application/policy** in `terraform/cloudflare/` (email OTP pin), n8n owner auth as second layer. Webhook and API traffic never leaves the cluster.

Workflow-as-code against the **n8n public REST API** (verified 2026-08: base `/api/v1`, header `X-N8N-API-KEY`; `GET/POST /workflows`, `GET/PUT/DELETE /workflows/{id}`, `POST /workflows/{id}/activate|deactivate`; credentials are **write-only** via API - values can never be exported, which is the correct GitOps boundary: workflows in git reference credentials by name; credential values live only in the instance).

CLI `opsagent n8n export|import|diff` (typer):
- `export` - pull workflows, normalise (strip `updatedAt`/`versionId`/counters, stable key order), write `workflows/<slug>.json` + `workflows/manifest.yaml` (id↔file map, active flag, tags);
- `diff` - normalised instance↔git comparison (CI check and round-trip proof);
- `import` - create-or-update by manifest, reconcile activation. Idempotent.

**Sync path (ADR-002):** the n8n API is *never* exposed through the tunnel. Import runs **in-cluster** as an ArgoCD PostSync hook Job (CI-built image) against `http://n8n.ai-lab.svc:5678/api/v1`. Rejected alternative: GitHub Actions → tunnel + service token (bigger surface, CI secret sprawl).

**DoD**
- [ ] n8n running on Postgres in `ai-lab`, state survives pod restart; UI at `n8n.batpepe.online` behind Cloudflare Access (verified: unauthenticated request blocked, authenticated passes).
- [ ] sealed-secrets controller deployed via GitOps; every lab secret is a `SealedSecret` in git - zero plaintext, zero manual `kubectl create secret` for this lab.
- [ ] DB provisioning Job created `n8n` database + role (non-superuser) idempotently.
- [ ] Round-trip proven: trivial workflow edited in UI → `export` → PR with reviewable JSON diff → merge → pipeline deploys → `diff` empty.
- [x] CI validates workflow JSON on every run, with no API key and no cluster: `opsagent n8n validate` catches read-only fields, missing required fields, non-canonical formatting and manifest drift.
- [ ] Platform PR merged: Application file + sealed-secrets Application + terraform (tunnel host, Access).
- [x] ADR-001 (topology/namespace), ADR-002 (sync path), ADR-003 (sealed-secrets scope), ADR-004 (Access for admin surfaces).

**Written and tested, waiting on a deploy:** the API client, the export/diff/import/validate tooling and 79 tests are done and green. The manifests in `deploy/` are written against verified facts (chart 2.0.1 carrying n8n 1.122.4 from the OCI registry, sealed-secrets 2.19.2, ArgoCD v3.4.2 resolving OCI charts with no repository secret) but have not been applied: every remaining item above changes the production cluster, so `deploy/README.md` carries the ordered procedure and those steps are the author's to run.

**Measured during the phase:** the 8gears chart's old HTTP repository is gone (404) and the chart now ships only via OCI, which is why this is the first Application in the platform to use an OCI `repoURL`. `kubeseal` is not installed on the laptop yet, so the sealed secrets cannot be generated here.

### Phase 2 - Tool layer over MCP
Cluster capabilities as one **ToolRegistry** (single source of truth: name, description, pydantic input/output schemas, handler). Two bindings from day one: **MCP server** (official `mcp` SDK; stdio entrypoint for Claude Code on the laptop via read-only kubeconfig context over Tailscale, and streamable-HTTP mounted in the FastAPI app for in-cluster use), and the **in-process binding** the agent loop uses later (pushback §1).

The **redaction layer lands here**, not in phase 3 - it is a choke point on every tool's output path, so nothing unredacted ever leaves the tool boundary: token/secret patterns, emails, IPs, internal hostnames (configurable list seeded with `*.batpepe.online`, `*.svc`, Tailscale IPs). Every result carries `redactions_applied` and `truncated`. All outputs size-capped. Every external call goes through one `resilience.py` wrapper (timeout, bounded retry + jitter, circuit breaker) - no bare awaits.

Backends (verified endpoints): K8s API (official client, in-cluster SA or kubeconfig), Loki `http://loki.monitoring.svc:3100/loki/api/v1/*`, Prometheus `http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090/api/v1/*`. `get_recent_deploys` reads **ArgoCD Application CRs directly** (`applications.argoproj.io`, `status.history[]`/`operationState`) - read-only k8s RBAC instead of provisioning ArgoCD API tokens (ArgoCD isn't GitOps-managed there; adding accounts would mean hand-editing `argocd-cm` - avoided), plus recent commits from the public platform repo via unauthenticated GitHub API.

**DoD**
- [ ] Six tools implemented and unit-tested against a faked K8s API and faked Loki/Prometheus HTTP (respx) - zero network in CI.
- [ ] Redaction suite is the heaviest in the repo (real-shaped fixtures with planted secrets/IPs/emails/hostnames; property-style cases).
- [ ] Read-only RBAC written and documented: ServiceAccount + ClusterRole (get/list/watch on pods, events, deployments, replicasets, nodes, pvcs; `applications.argoproj.io`; **no secrets, no write verbs**) - the "what it can touch and why" table in `docs/threat-model.md`.
- [ ] MCP server driven from the author's Claude Code session against the real cluster - demonstrated before any agent exists.
- [ ] MCP↔in-process parity test (same registry → same schemas/results).

### Phase 3 - The agent
`LLMProvider` protocol; **`MockProvider` (deterministic scenario playback) is the default** everywhere including CI; `OpenAICompatProvider` behind config for the free-tier backends (decision 4; reference pricing in `providers/pricing.py` config table - never at call sites). Switching provider/model/backend is config only. Repo clones and runs end-to-end with zero API keys and zero spend.

Agent loop: hardened system prompt (tool outputs wrapped in delimiters, declared untrusted data) → tool-use rounds against the in-process registry → structured `TriageReport` (pydantic: ranked hypotheses + supporting evidence refs, blast radius, matched runbook, confidence, explicit unknowns, suggested-never-executed next actions). Provider-side structured output where supported; validate + **one** bounded repair retry on malformed output.

Guardrails, all enforced in the loop, all tested: hard per-investigation budget as **token cap + USD-equivalent cap** (token cap binds on free tiers where cost is $0; USD binds when paid pricing is configured; pre-call estimate + post-call actual; exceed → clean abort, partial findings, status `budget_exceeded`), max tool-call depth, wall-clock timeout, per-call output caps, provider-rate-limit-aware backoff, and **service-side dedup + per-alert-class rate limit** as the authoritative backstop behind n8n's dedup (pushback §6).

Persistence (SQLAlchemy 2 async + Alembic): every investigation, model call, tool call. REST: `POST /investigations` (idempotent on fingerprint within window), `GET /investigations[/{id}]`, `POST /investigations/{id}/resolution`.

**DoD**
- [ ] Mock e2e: alert payload in → persisted TriageReport out, with model_calls + tool_calls rows.
- [ ] Guardrail tests: budget stop, depth cap, timeout, malformed-output repair, redaction-before-prompt, RBAC-denied tool call surfaces as an evidence gap (not a crash).
- [ ] Prompt-injection harness test (delimiting/untrusted marking asserted; behavioural resistance measured in phase 6 - honest split, pushback §4).
- [ ] Contract tests for structured-output parsing incl. malformed/truncated responses.
- [ ] One documented real-provider smoke investigation on the free tier ($0 actual); token usage + "would-cost at paid rates" recorded in `docs/cost-model.md`.

### Phase 4 - The loop closes
Alertmanager → n8n: add a **webhook receiver + route to Alertmanager's config** in the kps inline values (platform-repo touchpoint; today Alertmanager routes PrometheusRule alerts nowhere - the Grafana→Telegram flow is untouched, and the "does FlaskAppDown actually deliver?" finding gets verified and documented while at it). Receiver targets `http://n8n.ai-lab.svc:5678/webhook/...` in-cluster.

n8n workflows (all in `workflows/`, deployed by the phase-1 pipeline, never click-only): dedup by alert fingerprint per window → `POST /investigations` → poll → Telegram summary (existing bot) with link to the report page → GitHub issue when the alert class is new (gated, pushback §2) → resolution capture (`opsagent resolve <id>` CLI + API first; Telegram inline buttons as stretch).

**DoD**
- [ ] Kill a pod in a scratch namespace → Telegram message with ranked hypotheses + report link, no human involvement; timing recorded.
- [ ] Alert storm test: N duplicates in window → exactly one investigation; the rest counted in `triage_investigations_total{outcome="deduplicated"}`.
- [ ] GitHub issue created once per new alert class, linked to the investigation.
- [ ] Resolution round-trip: actual root cause stored → visible on report page → accuracy join works.
- [ ] Alertmanager delivery finding verified & written up (platform docs updated if it was indeed dead-ended).

### Phase 5 - Observability
Prometheus metrics (`triage_investigations_total{outcome}`, `triage_duration_seconds{stage}`, `triage_tool_calls_total{tool}`, `llm_tokens_total{model,direction}`, `llm_cost_usd_total{model}` - would-cost at configured reference prices, `triage_budget_exceeded_total`) + **the platform's first ServiceMonitor** (with `labels: {release: monitoring}` - same selector rule as PrometheusRules). Grafana dashboard JSON via the sidecar ConfigMap convention (`grafana_dashboard: "1"`, datasource uids `prometheus`/`loki`). `/health` (no deps) + `/ready` (DB, K8s API, provider). Plain server-rendered report page, published behind the same Cloudflare Access application as n8n (decision 2). `docs/runbook.md`. PrometheusRule (with `release: monitoring`) alerting on the agent itself: error rate, budget-exceeded, alerts-arriving-but-no-investigations.

**DoD**
- [ ] Dashboard shows live data from ≥5 investigations; screenshot in README.
- [ ] The watcher is watched: meta-alert fires in a controlled test and reaches Telegram.
- [ ] Report page renders an investigation end-to-end from the DB alone.

### Phase 6 - Fault injection & evaluation (the phase that makes the project)
`evals/scenarios/*.yaml`: fault manifest + expected root cause + expected evidence. Harness `opsagent eval run` applies faults to a scratch namespace `chaos-lab` using a **separate write-capable kubeconfig held only by the harness** - the agent's SA stays read-only. Scenarios (≥8): OOMKill, bad image tag, failing readiness probe, full PVC, blackholed dependency, broken ArgoCD sync, expired certificate, prompt-injection via malicious log line.

Scoring per scenario: top-1/top-3 root-cause accuracy, time-to-hypothesis, tool calls, tokens, cost; confidence calibration (stated vs. actual). `docs/eval-report.md` with the numbers, per-miss analysis, and what to fix next - misses reported honestly.

**DoD**
- [ ] ≥8 scenarios, one command, repeatable, self-cleaning.
- [ ] `docs/eval-report.md` with the table, calibration note, token totals and would-cost-at-paid-rates figures (real-provider run throttled to fit the free tier's daily quota; $0 actual spend).
- [ ] Prompt-injection scenario result reported as-is, whatever it shows.

### Phase 7 - Second & third automations (only after 0-6 documented)
Daily digest (scheduled n8n → agent over the same tools), manifest-review bot (PR webhook → k8s manifest diff → PR comment: removed limits, replica changes, `:latest`, secret-about-to-be-committed), CVE triage (nightly Trivy output → "affects a running image?" → digest). Each reuses provider layer, cost accounting, n8n pipeline, metrics. DoD written when the phase starts.

## Proposed file tree

```
ai-automation-lab/
├── README.md CLAUDE.md plan.md LICENSE
├── pyproject.toml uv.lock .python-version .env.example
├── Dockerfile docker-compose.yml            # local: n8n + postgres + opsagent
├── .github/workflows/{ci.yml,n8n-sync.yml,gitleaks.yml,commitlint.yml}
├── src/opsagent/
│   ├── config.py cli.py resilience.py
│   ├── api/            # FastAPI: investigations, resolution, report page, health/ready
│   ├── agent/          # loop.py budget.py prompts.py report.py (TriageReport)
│   ├── providers/      # base.py openai_compat.py mock.py pricing.py
│   ├── tools/          # registry.py k8s.py loki.py prometheus.py argocd.py runbooks.py
│   ├── mcp/            # server.py __main__.py (stdio)
│   ├── redaction/      # engine.py patterns.py
│   ├── persistence/    # models.py repository.py alembic/
│   ├── n8n/            # client.py sync.py
│   └── observability/  # metrics.py logging.py (structlog)
├── tests/{unit,contract,e2e,guardrails}/
├── workflows/          # n8n JSON + manifest.yaml (source of truth)
├── deploy/
│   ├── argocd/         # child Applications (n8n, opsagent, sync-hook) - platform repo points here
│   ├── n8n/            # anything not expressible as inline chart values (secrets templates, netpol later)
│   ├── opsagent/       # deployment svc SA+RBAC ServiceMonitor PrometheusRule secrets
│   └── sync-hook/      # ArgoCD PostSync Job
├── dashboards/         # grafana json (packaged as sidecar ConfigMap in deploy/)
├── runbooks/           # <alertname>.md matched by get_runbook
├── evals/scenarios/    # *.yaml + fault manifests
└── docs/{adr/,runbook.md,threat-model.md,cost-model.md,assumptions.md,eval-report.md}
```

## Data model sketch (PostgreSQL, SQLAlchemy 2)

```
investigations
  id UUID PK · alert_fingerprint TEXT ix · alert_name TEXT ix · alert_labels JSONB
  trigger_source TEXT (webhook|manual|eval) · status TEXT
    (running|completed|failed|budget_exceeded|depth_exceeded|timeout)
  started_at/finished_at · budget_usd NUMERIC · spent_usd NUMERIC
  tool_calls_used INT · report JSONB (TriageReport) · summary TEXT · confidence REAL
  error TEXT NULL
  UNIQUE (alert_fingerprint, window_bucket)     -- authoritative dedup, survives n8n

model_calls
  id UUID PK · investigation_id FK ix · seq INT · provider TEXT · model TEXT
  tokens_in INT · tokens_out INT · cost_usd NUMERIC · latency_ms INT · stop_reason TEXT

tool_calls
  id UUID PK · investigation_id FK ix · model_call_id FK NULL · seq INT
  tool_name TEXT · arguments JSONB · result_bytes INT · truncated BOOL
  redactions_applied INT · duration_ms INT · success BOOL · error TEXT NULL

resolutions
  investigation_id FK UNIQUE · actual_root_cause TEXT
  verdict TEXT (correct|partially_correct|incorrect|undetermined)
  matched_hypothesis_rank INT NULL · notes TEXT · resolved_at · resolved_by

alert_classes
  alert_name TEXT PK · first_seen · last_seen · investigation_count INT
  github_issue_url TEXT NULL                    -- "new class" gate

eval_runs (phase 6)
  id · started_at · scenario_set · provider · totals JSONB   -- groups eval investigations
```

Accuracy metric = investigations joined with resolutions; evals reuse the same tables via `trigger_source='eval'`.

## Tool interface signatures (phase 2)

All async, pydantic results, redacted + size-capped at this boundary, wrapped by `resilience.py`.

```python
async def get_pod_status(namespace: str, pod: str | None = None,
                         selector: str | None = None) -> PodStatusReport
    # phase, restarts, container states + last termination (reason/exit code),
    # requests/limits, node, conditions

async def get_events(namespace: str, involved_object: str | None = None,
                     since_minutes: int = 60, limit: int = 50) -> EventList

async def query_logs(namespace: str, pod: str | None = None, container: str | None = None,
                     grep: str | None = None, since_minutes: int = 30,
                     limit: int = 200) -> LogExcerpt        # Loki loki.monitoring.svc:3100

async def query_metrics(promql: str, since_minutes: int = 60,
                        step: str = "1m") -> MetricResult   # bounded range & series count

async def get_recent_deploys(app: str | None = None, since_hours: int = 24,
                             limit: int = 20) -> DeployHistory
    # ArgoCD Application CRs (status.history, operationState) + recent commits
    # touching the affected manifests (public repo, unauthenticated GitHub API)

async def get_runbook(alert_name: str) -> RunbookEntry | None  # slug match in runbooks/
```

`ToolRegistry` derives the MCP declarations and the provider tool schemas from these signatures - one definition, three consumers (MCP stdio, MCP HTTP, agent loop).

## LLM provider layer (phase 3) - free-tier-first (user decision)

```python
class LLMProvider(Protocol):
    name: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    async def healthcheck(self) -> bool: ...  # feeds /ready


# CompletionRequest: model, system, messages, tool_schemas, max_tokens, json_schema|None
# CompletionResponse: blocks (text | tool_use), tokens_in/out, stop_reason
# cost computed centrally from providers/pricing.py (config), never at call sites
```

- `MockProvider` - deterministic scripted scenarios; **default everywhere incl. CI**; zero keys, zero spend.
- `OpenAICompatProvider` - the one real implementation, speaking the OpenAI-compatible `/chat/completions` dialect with tool calling + JSON-schema response format. This single class covers every free option as pure config (`base_url`, `api_key`, `model`): **Google Gemini free tier via its OpenAI-compat endpoint (proposed default: `gemini-2.5-flash` - free daily quota, solid function calling)**, Groq free tier (`llama-3.3-70b-versatile`), OpenRouter `:free` models, and **local Ollama** for a fully private $0 mode. Switching = config change, never code - the brief's requirement, demonstrated across four backends.
- Paid providers (Anthropic etc.) remain a documented extension point in `providers/` - not implemented in v1 since there's no free tier; the protocol already accommodates them.

**Free-tier consequences, designed in rather than ignored:**
1. **Budgets stay meaningful at $0:** guardrail = USD cap **and** token cap per investigation (token cap is the binding one on free tiers; USD activates whenever paid pricing is configured). `pricing.py` still carries reference paid prices so reports can state "this investigation ≈ $X at provider Y rates" - that's the cost-model story for `docs/cost-model.md`.
2. **Rate limits are the real constraint** (free tiers are RPM/RPD-capped): investigation concurrency = 1, provider calls go through the same `resilience.py` (429-aware backoff), and the dedup window sizes accordingly. Eval runs (phase 6) throttle to fit daily quota.
3. **Threat model gets an honest section:** free tiers may use inputs for training (Google's free tier explicitly does) - one more reason redaction sits *before* the provider boundary and is the most-tested layer in the repo; the Ollama profile exists for anyone who wants nothing to leave the network.

## Things in the brief I'd push back on

1. **"Have the agent consume the tools over MCP."** Literal MCP-loopback (agent → HTTP → own MCP server) adds a serialisation hop and a failure surface *inside* the guardrail perimeter while proving nothing extra - both bindings are thin adapters over one `ToolRegistry`. Proposal: agent binds the registry **in-process**; MCP remains the external contract (Claude Code, n8n, future consumers), with a parity test pinning both paths to identical schemas/results. "Same tools work everywhere" stays literally true. ADR-005. *(If you want dogfooding-via-MCP anyway it's a config flag - but default off.)*
2. **GitHub issue per new alert class** will be noisy during fault injection (phases 4-6 create alert classes by design). Gate: non-eval sources only, confidence/severity floor, once per class with cooldown (`alert_classes` makes it deterministic), issue links the investigation.
3. **Persisting full evidence** (raw logs/events) would bloat the 1Gi-PVC Postgres and re-leak what redaction scrubbed. Persist bounded, already-redacted excerpts only; the report cites excerpts, never full dumps.
4. **A unit test where "a log line instructs the model"** can only assert harness properties (delimiting, untrusted marking, no tool escalation) under MockProvider - it cannot prove the *model* resists. Honest split: deterministic harness tests in phase 3, live injection scenario measured in phase 6, threat model states exactly this. Claiming more would be the overclaim this project exists to avoid.
5. **Alert delivery assumption.** The brief assumes Alertmanager fires webhooks today; in reality Alertmanager runs chart defaults and Telegram lives in Grafana alerting. The fix (phase 4) is small and additive - an Alertmanager receiver+route in the kps values - and likely *repairs* a real gap (PrometheusRule alerts currently appear to dead-end). Verified and documented as part of phase 4.
6. **Dedup only in n8n** puts a core guardrail in the least-testable layer. n8n dedups first (cheap), the service enforces fingerprint-window uniqueness as a DB constraint plus per-class rate limit - survives n8n bugs, restarts, replays.
7. **1Gi shared Postgres + n8n execution history** don't mix long-term: execution pruning on from day one, and the eval phase watches DB growth. If it becomes a problem, that's a documented reason to revisit Q3.

## Decisions (user-confirmed 2026-08-18)

1. **Secrets: introduce sealed-secrets now.** Controller lands as one more Application in the platform's `argocd-apps/`; all NEW secrets of this lab (n8n encryption key, DB creds, n8n API key, provider API key) are `SealedSecret` resources in git. Existing platform secrets are untouched (their migration stays a platform roadmap item, now unblocked). ADR documents scope.
2. **n8n UI: tunnel + Cloudflare Access.** `tunnel_hosts += "n8n"` and the account's first Zero Trust Access application + policy (email OTP / identity pin), managed in `terraform/cloudflare/` alongside the existing tunnel config. n8n's built-in owner auth stays on as the second layer. The report page ships behind the same Access app later (phase 5) - until then it stays cluster/LAN-only.
3. **Postgres: shared instance, separate DBs + non-superuser roles** (`n8n`, `opsagent`), provisioned by an idempotent Job - closing the platform's "seed via Job" roadmap gap and ending the shared-`devops`-superuser pattern for new apps. n8n execution pruning on from day one (shared PVC is 1Gi).
4. **Real provider: free-tier only.** `OpenAICompatProvider` with Gemini free tier as proposed default; Groq/OpenRouter/Ollama as config-swap alternates; no paid keys required anywhere in v1. See provider section for how budgets/rate limits/threat model absorb this.

Defaulted (recorded in `docs/assumptions.md`, overridable at any review):
5. Telegram: reuse the existing bot from `tg-secret`. 6. Budgets: per-investigation token cap 200K + $0.25 USD-equivalent cap (binding when paid pricing configured); eval runs throttled to free-tier daily quota. 7. Repo: public `batpepe/ai-automation-lab`, MIT. 8. Topology: one Application file in the platform repo pointing at `deploy/argocd/` here. 9. Images: linux/amd64. 10. Laptop→cluster access for MCP/evals assumes Tailscale up.

## Verification (per phase)

Each phase ends with: `ruff check` + `mypy --strict src/` + `uv run pytest` green (no network in CI), GH Actions green, plus the phase's live check - P1: UI→git→pipeline round-trip, `opsagent n8n diff` empty; P2: tool calls from Claude Code against the real cluster; P3: mock e2e + one real-provider smoke; P4: pod kill → Telegram end-to-end + storm dedup; P5: dashboard live + meta-alert test-fire; P6: full eval run producing `docs/eval-report.md`. Then stop and wait for review, per working agreement.

## References (fetched 2026-08-18)

- n8n public API: docs.n8n.io/connect/n8n-api/ (`X-N8N-API-KEY`, `/api/v1`) + OpenAPI spec in n8n-io/n8n (endpoints verified).
- n8n on K8s: no official chart; community `8gears/n8n-helm-chart` (maintained); n8n-io/n8n-hosting for raw-manifest examples.
- MCP Python SDK: package `mcp`, decorator tools, stdio + streamable-HTTP, ASGI mounting - py.sdk.modelcontextprotocol.io.
- Anthropic API: current models/pricing/structured outputs (claude-api reference, loaded via skill).
- Platform facts: file-level exploration of `devops-homelab-k3s-hybrid-cloud` @ main, 2026-08-18.
