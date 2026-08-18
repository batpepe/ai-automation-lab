# 1. One namespace for the lab, one file in the platform repository

Date: 2026-08-18

## Status

Accepted.

## Context

The platform repository deploys first-party workloads into a shared `apps`
namespace and gives third-party workloads a namespace of their own. This lab is
both: n8n is third-party, the agent is first-party, and they are one system.

The platform's ArgoCD root Application deploys any Application file dropped into
`k8s-infrastructure/argocd-apps/`. The lab could add one file per component
there, or one file that points back here.

## Decision

Everything the lab runs goes into a dedicated `ai-lab` namespace, and the
platform repository gains a single Application pointing at `deploy/argocd/` in
this repository, which holds the child Applications.

## Consequences

RBAC, secrets and the NetworkPolicy planned for a later phase all describe one
blast radius, which is the property that matters for a workload that holds an
API key and reads cluster telemetry.

Review happens in the repository that owns the code. A change to the n8n chart
version is a pull request here, not a commit in the platform repository.

The cost is one deviation from the platform's `apps` habit, and one level of
indirection when tracing what is deployed: the platform lists `ai-automation-lab`
rather than `n8n`. `deploy/README.md` documents the mapping.
