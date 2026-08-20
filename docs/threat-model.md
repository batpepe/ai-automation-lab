# Threat model

The agent reads production telemetry, forms conclusions from it, and sends some
of that telemetry to a third party. This document says what it can touch, what
it deliberately cannot, and which risks are accepted rather than solved.

## Trust boundaries

| Boundary | What crosses it | Control |
|---|---|---|
| Cluster to agent | Pod status, events, logs, metrics, ArgoCD history | Read-only ServiceAccount, no Secret access |
| Agent to model provider | Redacted evidence excerpts | Redaction at the tool boundary, size caps, budget caps |
| Alert source to agent | Alertmanager webhook payload | Dedup by fingerprint, per-class rate limit |
| Workload to agent | Log lines, event messages | Treated as untrusted data, never as instructions |
| Human to agent | Resolution verdicts | Authenticated surface, no effect on running work |

The load-bearing property is that the second boundary is downstream of the
first. Everything reaching a provider has already passed through redaction,
because redaction is applied in the tool registry rather than in front of the
prompt. A tool added later inherits it without asking.

## What the agent can touch, and why

Enforced by `deploy/manifests/rbac.yaml` and tested by
`tests/guardrails/test_rbac_is_read_only.py`, which fails if a write verb or
Secret access is ever added.

| Resource | Verbs | Why it is needed |
|---|---|---|
| pods, pods/status | get, list, watch | Phase, restart counts and the last termination reason: the difference between an OOMKill, a failing probe and a bad image |
| events | get, list, watch | The kubelet's and scheduler's account of a failure, which the pod object omits |
| nodes | get, list, watch | Pressure conditions, so "the node is full" is distinguishable from "this pod is broken" |
| persistentvolumeclaims | get, list, watch | A full or unbound volume is a fault scenario and a real cause |
| services, configmaps | get, list, watch | Misrouted traffic and misconfigured values are ordinary causes |
| deployments, replicasets, statefulsets, daemonsets | get, list, watch | A pod that never appeared is a controller problem the pod cannot report |
| applications.argoproj.io | get, list, watch | "What changed just before this broke", without an ArgoCD API account |
| certificates.cert-manager.io | get, list, watch | Expiry is a phase 6 fault scenario |

**Secrets are deliberately excluded.** The agent reads pod specs, which name the
secrets a workload mounts, and that is enough to diagnose a missing or misnamed
one. Reading their contents would add nothing to a diagnosis and would put every
credential in the cluster one prompt injection away from a provider API.

**No write verbs anywhere.** The model proposes; code executes. There is no
remediation path in v1. The fault injection harness in phase 6 does need to
break things, and carries its own separate credential that is never bound to
this account.

## Prompt injection

A log line is written by the workload, not by the operator. Anyone who can write
to a log the agent reads can write instructions to the agent: a compromised
container, a dependency that echoes attacker-controlled input, or a public
endpoint that logs a request body.

What is done about it:

- **Capability, not persuasion, is the real control.** The worst outcome of a
  successful injection is bounded by the RBAC above: read-only, no secrets, no
  shell. An injected instruction cannot delete a Deployment because the
  credential cannot.
- Tool output is delimited and labelled as untrusted data in the system prompt
  (phase 3), never concatenated into the instruction section.
- Tool-call depth, wall-clock and budget caps bound how much an injected
  instruction can spend before the run aborts.
- Redaction runs before the prompt, so an injection that tries to exfiltrate a
  credential through the model's reply has less to work with.

What is **not** claimed: that the model resists injection. A unit test can only
assert harness properties (delimiting, no tool escalation) under a mock
provider. Whether the model actually holds is measured in phase 6, with a
scenario that plants a hostile log line, and the result is reported whatever it
shows.

## Accepted risks

| Risk | Why it is accepted | What would change it |
|---|---|---|
| A credential in an unrecognised shape passes redaction | The alternative is a generic high-entropy rule that eats image digests and resource UIDs, which are exactly what a triage agent needs | A new leak shape appears; add a pattern to `redaction/patterns.py` |
| The provider may train on free-tier inputs | v1 runs on a free tier by choice; redacted evidence only, and the Ollama profile exists for anyone who wants nothing to leave the network | Moving to a paid tier with a no-training term |
| Alias tables map placeholders back to real values in process memory | A human reading a stored report needs to resolve `<ip-1>`; the table is never serialised into a prompt | Persisting alias tables would need encryption at rest |
| n8n holds credentials for everything it automates | It is the most valuable target in the cluster, behind Cloudflare Access and its own auth | See ADR-0004 |
| ArgoCD is not itself GitOps-managed | Pre-existing platform condition, not introduced here | A platform roadmap item |

## Out of scope for v1

Auto-remediation, multi-cluster access, agent-initiated writes of any kind, and
any path from a model response to a shell.
