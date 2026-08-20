"""The read-only claim, enforced rather than documented.

"The agent cannot write to the cluster" is one of this project's headline
claims. A claim that lives only in a README drifts the first time someone adds
a resource and copies the verbs from a nearby example.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.guardrail, pytest.mark.unit]

RBAC_PATH = Path(__file__).resolve().parents[2] / "deploy" / "manifests" / "rbac.yaml"
READ_ONLY_VERBS = {"get", "list", "watch"}


@pytest.fixture(scope="module")
def documents() -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(RBAC_PATH.read_text()) if doc]


@pytest.fixture(scope="module")
def cluster_role(documents: list[dict[str, Any]]) -> dict[str, Any]:
    roles = [doc for doc in documents if doc["kind"] == "ClusterRole"]
    assert len(roles) == 1, "one role, so there is one place to audit"
    return roles[0]


def test_the_role_grants_no_write_verb(cluster_role: dict[str, Any]) -> None:
    granted = {verb for rule in cluster_role["rules"] for verb in rule["verbs"]}

    assert granted <= READ_ONLY_VERBS, f"write verbs granted: {sorted(granted - READ_ONLY_VERBS)}"


def test_the_role_cannot_read_secrets(cluster_role: dict[str, Any]) -> None:
    # The agent reads pod specs, which name the secrets a workload mounts. That
    # is enough to diagnose a missing one. Reading their contents would put
    # every credential in the cluster one prompt injection away from a model API.
    resources = {resource for rule in cluster_role["rules"] for resource in rule["resources"]}

    assert "secrets" not in resources


@pytest.mark.parametrize("forbidden", ["secrets", "serviceaccounts/token", "*"])
def test_no_rule_grants_a_dangerous_resource(cluster_role: dict[str, Any], forbidden: str) -> None:
    resources = {resource for rule in cluster_role["rules"] for resource in rule["resources"]}

    assert forbidden not in resources


def test_no_rule_uses_a_wildcard_api_group(cluster_role: dict[str, Any]) -> None:
    groups = {group for rule in cluster_role["rules"] for group in rule["apiGroups"]}

    assert "*" not in groups


def test_the_binding_points_at_the_agent_account(documents: list[dict[str, Any]]) -> None:
    binding = next(doc for doc in documents if doc["kind"] == "ClusterRoleBinding")

    assert binding["roleRef"]["name"] == "opsagent-read"
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "opsagent", "namespace": "ai-lab"}
    ]


def test_the_service_account_is_not_granted_anything_else(
    documents: list[dict[str, Any]],
) -> None:
    # A second binding is how least privilege quietly stops being true.
    bindings = [doc for doc in documents if doc["kind"].endswith("RoleBinding")]

    assert len(bindings) == 1
