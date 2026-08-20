"""Runbook lookup by alert name.

The cheapest tool in the set and often the most valuable: if a human already
wrote down what this alert means, the agent should read that before inferring
anything. It also grounds the report, because a hypothesis that cites the
runbook is one a human can check in seconds.
"""

from __future__ import annotations

import re
from pathlib import Path

from opsagent.tools.models import RunbookEntry, RunbookInput

MAX_RUNBOOK_CHARS = 8_000

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify_alert(alert_name: str) -> str:
    return _SLUG.sub("-", alert_name.strip().lower()).strip("-")


async def get_runbook(params: RunbookInput, directory: Path) -> RunbookEntry:
    """Return the runbook for an alert, if one has been written."""
    slug = slugify_alert(params.alert_name)
    if not slug:
        return RunbookEntry(alert_name=params.alert_name, found=False)

    for candidate in (directory / f"{slug}.md", directory / f"{params.alert_name}.md"):
        # Resolve before comparing: the alert name reaches this from a webhook
        # payload, so a crafted name must not be able to read outside the
        # runbook directory.
        resolved = candidate.resolve()
        if not resolved.is_relative_to(directory.resolve()):
            continue
        if resolved.is_file():
            content = resolved.read_text(encoding="utf-8")[:MAX_RUNBOOK_CHARS]
            return RunbookEntry(
                alert_name=params.alert_name,
                found=True,
                path=resolved.name,
                content=content,
            )

    return RunbookEntry(alert_name=params.alert_name, found=False)
