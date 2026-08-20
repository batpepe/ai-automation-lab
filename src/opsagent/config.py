"""Runtime configuration, read from the environment.

Every setting has a default that works on a laptop with no API keys, no
database and no cluster. That is a hard requirement rather than a convenience:
the repository has to clone and run end to end for a reviewer who will never
have access to the homelab.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "OPSAGENT_"


class Environment(StrEnum):
    """Where the process is running, which is not the same as how it is configured."""

    LOCAL = "local"
    CLUSTER = "cluster"


class Settings(BaseSettings):
    """Effective configuration for one process.

    Secrets added in later phases must be typed `SecretStr` so that printing an
    instance cannot leak them. See `opsagent.cli.show_config`.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        # Deliberately "ignore" rather than "forbid", which was measured rather
        # than assumed: "forbid" does not reject a mistyped OPSAGENT_ variable
        # (the environment source never offers it to the model), and it does
        # reject unrelated keys in a real .env such as the n8n or database
        # credentials. It costs the protection it appears to give. The typo
        # check below is the version that actually works.
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    # Left unset, log rendering follows the environment. JSON on a terminal is
    # unreadable; console formatting in the cluster makes Loki queries guesswork.
    log_json: bool | None = None

    # The in-cluster service address. The n8n API is never published through the
    # tunnel, so workflow sync runs inside the cluster and reaches it here.
    n8n_url: str = "http://n8n.ai-lab.svc:5678"
    # SecretStr so that `show-config`, logs and tracebacks print a mask rather
    # than a working API key.
    n8n_api_key: SecretStr | None = None
    workflows_dir: Path = Path("workflows")

    # Cluster telemetry, at the in-cluster service addresses read from the
    # platform repository. Override them to point at a port-forward when
    # driving the tools from a laptop.
    loki_url: str = "http://loki.monitoring.svc:3100"
    prometheus_url: str = "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090"
    argocd_namespace: str = "argocd"
    runbook_dir: Path = Path("runbooks")
    # None means in-cluster credentials first, then the default kubeconfig.
    kubeconfig: Path | None = None

    @model_validator(mode="after")
    def _reject_unknown_prefixed_variables(self) -> Settings:
        """Fail on an OPSAGENT_ variable that matches no setting.

        A mistyped OPSAGENT_BUDGET_USD would otherwise leave the default budget
        in place and report nothing, which is the failure mode this project
        exists to argue against.
        """
        known = {f"{ENV_PREFIX}{name}".upper() for name in type(self).model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
        )
        if unknown:
            raise ValueError(f"unknown {ENV_PREFIX}* variables: {', '.join(unknown)}")
        return self

    @property
    def render_json_logs(self) -> bool:
        """Whether logs should be JSON, resolving the tri-state `log_json`."""
        if self.log_json is not None:
            return self.log_json
        return self.environment is Environment.CLUSTER


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings.

    Cached so that configuration is read once and cannot drift between callers
    mid-run. Tests that manipulate the environment call `get_settings.cache_clear()`.
    """
    return Settings()
