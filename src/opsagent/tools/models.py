"""Input and output shapes for the cluster tools.

Every output model is deliberately narrower than the API object it comes from.
A raw Kubernetes Pod is tens of kilobytes of managed fields and defaults; the
handful of fields that decide a root cause is a few hundred bytes. Narrowing
here is what keeps an investigation inside its budget, and it means the model
never sees fields nobody reasoned about.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- Shared -----------------------------------------------------------------


class ToolInput(BaseModel):
    """Base for every tool input, so bounds are described in one place."""

    model_config = {"extra": "forbid"}


# --- get_pod_status ---------------------------------------------------------


class PodStatusInput(ToolInput):
    namespace: str = Field(description="Kubernetes namespace to inspect.")
    pod: str | None = Field(default=None, description="Exact pod name. Omit to list the namespace.")
    selector: str | None = Field(default=None, description="Label selector, for example app=n8n.")


class TerminationDetail(BaseModel):
    reason: str | None = None
    exit_code: int | None = None
    finished_at: str | None = None
    message: str | None = None


class ContainerStatus(BaseModel):
    name: str
    ready: bool
    restart_count: int
    state: str = Field(description="running, waiting or terminated.")
    waiting_reason: str | None = None
    last_termination: TerminationDetail | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


class PodStatus(BaseModel):
    name: str
    phase: str
    node: str | None = None
    start_time: str | None = None
    containers: list[ContainerStatus] = Field(default_factory=list)
    conditions: dict[str, str] = Field(
        default_factory=dict, description="Condition type to status."
    )


class PodStatusReport(BaseModel):
    namespace: str
    items: list[PodStatus] = Field(default_factory=list)
    note: str | None = None


# --- get_events -------------------------------------------------------------


class EventsInput(ToolInput):
    namespace: str
    involved_object: str | None = Field(
        default=None, description="Restrict to events about this object name."
    )
    since_minutes: int = Field(default=60, ge=1, le=1440)
    limit: int = Field(default=50, ge=1, le=200)


class ClusterEvent(BaseModel):
    type: str
    reason: str
    message: str
    count: int = 1
    first_seen: str | None = None
    last_seen: str | None = None
    involved_object: str | None = None


class EventList(BaseModel):
    namespace: str
    events: list[ClusterEvent] = Field(default_factory=list)
    note: str | None = None


# --- query_logs -------------------------------------------------------------


class LogsInput(ToolInput):
    namespace: str
    pod: str | None = None
    container: str | None = None
    grep: str | None = Field(default=None, description="Substring or regex to filter lines.")
    since_minutes: int = Field(default=30, ge=1, le=1440)
    limit: int = Field(default=200, ge=1, le=1000)


class LogLine(BaseModel):
    timestamp: str
    message: str


class LogExcerpt(BaseModel):
    namespace: str
    pod: str | None = None
    lines: list[LogLine] = Field(default_factory=list)
    note: str | None = None


# --- query_metrics ----------------------------------------------------------


class MetricsInput(ToolInput):
    promql: str = Field(description="PromQL expression.")
    since_minutes: int = Field(default=60, ge=1, le=1440)
    step: str = Field(default="1m", description="Resolution, for example 30s or 5m.")


class MetricSample(BaseModel):
    timestamp: float
    value: float


class MetricSeries(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    samples: list[MetricSample] = Field(default_factory=list)


class MetricResult(BaseModel):
    query: str
    series: list[MetricSeries] = Field(default_factory=list)
    note: str | None = None


# --- get_recent_deploys -----------------------------------------------------


class DeploysInput(ToolInput):
    app: str | None = Field(default=None, description="ArgoCD application name.")
    since_hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=20, ge=1, le=100)


class DeployEvent(BaseModel):
    app: str
    revision: str | None = None
    deployed_at: str | None = None
    phase: str | None = Field(default=None, description="Succeeded, Failed, Running.")
    sync_status: str | None = None
    health_status: str | None = None
    message: str | None = None


class DeployHistory(BaseModel):
    entries: list[DeployEvent] = Field(default_factory=list)
    note: str | None = None


# --- get_runbook ------------------------------------------------------------


class RunbookInput(ToolInput):
    alert_name: str


class RunbookEntry(BaseModel):
    alert_name: str
    found: bool
    path: str | None = None
    content: str | None = None
