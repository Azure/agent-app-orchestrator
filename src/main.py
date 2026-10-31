"""Retired classic FastAPI service. Native publications have explicit entrypoints."""

raise RuntimeError(
    "Classic /orchestrator, /conversations and /api/dashboard APIs are retired. "
    "Run python -m native.hosted or python -m native.durable using its matching image."
)
