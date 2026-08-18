# 4. Cloudflare Access in front of the n8n editor

Date: 2026-08-18

## Status

Accepted.

## Context

Every admin surface in this cluster is protected by not having a public DNS
record. ArgoCD answers on `argocd.local`, Grafana has an Ingress but no entry in
the tunnel's host allowlist. There is no Cloudflare Access policy in the account
and no authenticating proxy anywhere.

That approach does not extend to n8n. The editor is a browser application worth
reaching from outside the LAN, and the instance has to receive an Alertmanager
webhook. It also holds credentials for everything it automates, which makes it
the most valuable single target in the cluster.

## Decision

Publish `n8n.batpepe.online` through the existing tunnel and put a Cloudflare
Access application in front of it, managed in `terraform/cloudflare/` beside the
tunnel configuration. n8n's own owner account stays enabled as a second factor
in the ordinary sense: two independent things must be true to reach a workflow.

Webhook traffic does not use this path. Alertmanager reaches n8n at
`http://n8n.ai-lab.svc:5678` inside the cluster, as does workflow sync, so no
authentication exception is needed for machine traffic.

## Consequences

Access is free at this scale and terminates unauthenticated requests at
Cloudflare's edge, so an unauthenticated visitor never reaches the cluster at
all. This is strictly stronger than what protects Grafana today.

The account gains its first Zero Trust configuration, which the report page in
phase 5 can reuse rather than inventing something new.

Access and the DNS record must land together. A merged tunnel host with no
policy is an anonymous n8n editor on the public internet, which is why
`deploy/README.md` puts them in the same step and says so.

## Alternatives rejected

**LAN-only, like ArgoCD.** The safest option and the one the platform already
uses. Rejected because a webhook from an in-cluster Alertmanager works either
way, but the editor becomes unusable away from home, and the phase 5 report page
would inherit the same limitation.

**Tunnel with n8n's built-in login only.** One password between the internet and
a system holding every credential the automation uses, with no rate limiting
that this project controls and no second factor.
