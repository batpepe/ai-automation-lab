# 2. Workflow sync runs inside the cluster, not from CI

Date: 2026-08-20

## Status

Accepted.

## Context

Workflows live in git and have to reach the n8n instance. n8n's public API is
the only supported way to write them, and it needs an API key and network
access to the instance.

The instance's editor is published through the Cloudflare tunnel so it can be
used from a browser. Reaching the API from GitHub Actions would mean publishing
the API alongside it and giving CI a credential that can rewrite every workflow
in the cluster.

## Decision

The n8n API is never exposed through the tunnel. Import runs in-cluster as an
ArgoCD PostSync hook Job against `http://n8n.ai-lab.svc:5678/api/v1`, using an
API key sealed into the namespace.

CI validates workflow JSON and can run `opsagent n8n diff` against nothing at
all; the deployment path is git to ArgoCD to the cluster, like every other
workload here.

## Consequences

The credential with write access to workflows exists only inside the cluster.
There is no CI secret to leak or rotate, and no public API surface to defend.

Sync happens on sync, not on merge. A workflow change lands when ArgoCD next
reconciles rather than at the end of the CI run, which is the same latency every
other change in this platform already has.

Debugging a failed import means reading Job logs rather than a CI log, which is
slightly less convenient and exactly where every other cluster-side failure is
already investigated.

## Alternatives rejected

**GitHub Actions with a Cloudflare Access service token.** Workable, and it is
how a team without cluster access would do it. It trades a small convenience for
a public API endpoint, a long-lived CI credential, and a second authentication
system to keep correct.
