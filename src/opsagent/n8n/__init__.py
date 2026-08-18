"""n8n public API client and the workflow export/import tooling.

Workflows live in git and are deployed by the pipeline. The instance is a
runtime, not a source of truth, which is the whole point of this package:
without it, n8n state exists only inside a running container.
"""
