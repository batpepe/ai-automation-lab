"""Run the tool layer as an MCP server over stdio.

    python -m opsagent.mcp

Registered in an editor's MCP configuration, this is what lets the tools be
driven by hand against the real cluster before any agent exists. Reads come
from whatever kubeconfig context is active, so point it at a read-only context.
"""

from __future__ import annotations

from opsagent.config import get_settings
from opsagent.mcp.server import build_mcp_server
from opsagent.observability.logging import configure_logging
from opsagent.tools import build_registry_from_settings


def main() -> None:
    settings = get_settings()
    # stdio is the protocol channel, so logs must not be written to it. structlog
    # is configured to stdout, which is why the transport gets its own stream and
    # this stays on the default handler.
    configure_logging(settings.log_level, json_output=settings.render_json_logs)
    build_mcp_server(build_registry_from_settings(settings)).run(transport="stdio")


if __name__ == "__main__":
    main()
