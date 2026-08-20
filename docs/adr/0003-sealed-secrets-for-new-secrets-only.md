# 3. Sealed secrets, for this lab's secrets only

Date: 2026-08-20

## Status

Accepted.

## Context

The platform has no secrets controller. Every secret is created by hand with
`kubectl create secret`, with the command written as a comment above the
workload that consumes it and a row in the rotation runbook. Migrating to
sealed secrets has been the top item on the platform roadmap for a while and has
not happened, because migrating seven working secrets carries risk and no
immediate reward.

This lab needs four more: an n8n encryption key, a database password sealed into
two namespaces, and an API key for workflow sync. Adding them by hand would make
the eventual migration bigger, and would mean this repository cannot be deployed
from a clean checkout.

## Decision

Deploy the sealed-secrets controller now, and use it for this lab's secrets
only. The seven existing platform secrets stay exactly as they are.

## Consequences

The controller arrives with nothing depending on it, which is the cheapest
possible moment to introduce it. If it misbehaves, only this lab is affected.

The platform's migration is unblocked: the controller is running, the runbook
entry exists, and each existing secret can be converted one at a time whenever
its owner chooses.

The cluster now has two secret conventions at once, which is worse than one.
That is accepted as a transitional state and recorded in the platform roadmap
rather than pretended away.

Losing the controller's private key means every sealed secret has to be
regenerated from its plaintext. For the n8n encryption key specifically, losing
the plaintext is worse: n8n uses it to encrypt stored credentials, so it must be
backed up outside the cluster before first use.
