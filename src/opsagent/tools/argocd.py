"""Recent deploys, read from ArgoCD Application resources.

Deliberately not the ArgoCD API. ArgoCD in this cluster is installed from an
upstream manifest and is not itself GitOps-managed, so provisioning an API
account would mean hand-editing argocd-cm and holding another credential. The
Application custom resources carry the same sync history and are readable with
the ClusterRole the agent already needs.

"What changed just before this broke" is the question this answers, and it is
the one that resolves most incidents in a cluster that deploys from git.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from opsagent.tools.k8s import KubernetesReader, _parse_time
from opsagent.tools.models import DeployEvent, DeployHistory, DeploysInput

JsonDict = dict[str, Any]


async def get_recent_deploys(params: DeploysInput, reader: KubernetesReader) -> DeployHistory:
    """Sync history for one ArgoCD application or all of them."""
    applications = await reader.list_argocd_applications()
    cutoff = datetime.now(UTC) - timedelta(hours=params.since_hours)

    dated: list[tuple[datetime | None, DeployEvent]] = []
    for application in applications:
        name = str((application.get("metadata") or {}).get("name", ""))
        if params.app and name != params.app:
            continue

        status = application.get("status") or {}
        sync_status = (status.get("sync") or {}).get("status")
        health_status = (status.get("health") or {}).get("status")
        operation = status.get("operationState") or {}

        for record in status.get("history") or []:
            deployed_raw = record.get("deployedAt") or record.get("deployStartedAt")
            deployed_at = _parse_time(deployed_raw)
            if deployed_at is not None and deployed_at < cutoff:
                continue
            dated.append(
                (
                    deployed_at,
                    DeployEvent(
                        app=name,
                        revision=_short_revision(record.get("revision")),
                        deployed_at=str(deployed_raw) if deployed_raw else None,
                        phase=operation.get("phase"),
                        sync_status=sync_status,
                        health_status=health_status,
                        message=operation.get("message"),
                    ),
                )
            )

        # An application that has never synced has no history but can still be
        # the reason something is broken, so report its current state.
        if not (status.get("history") or []) and (params.app or sync_status):
            dated.append(
                (
                    None,
                    DeployEvent(
                        app=name,
                        sync_status=sync_status,
                        health_status=health_status,
                        phase=operation.get("phase"),
                        message=operation.get("message") or "no sync history recorded",
                    ),
                )
            )

    dated.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC), reverse=True)
    entries = [event for _, event in dated[: params.limit]]

    note = None
    if not entries:
        note = f"no deploys recorded in the last {params.since_hours}h"
    elif len(dated) > params.limit:
        note = f"showing {params.limit} of {len(dated)} deploys"
    return DeployHistory(entries=entries, note=note)


def _short_revision(revision: Any) -> str | None:
    """A commit SHA is only ever read by eye here; 12 characters is plenty."""
    if not revision:
        return None
    text = str(revision)
    return text[:12] if len(text) > 12 else text
