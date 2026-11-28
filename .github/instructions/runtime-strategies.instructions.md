---
applyTo: "src/strategies/**/*.py,src/orchestration/**/*.py,src/prompts/**,src/upload_prompts.py,src/startup_warmup.py"
---

# Runtime agent strategies and orchestration

These files implement agents and strategies executed by the product. They are
separate from GitHub Copilot engineering assets under `.github/`.

- Add orchestration behavior through a focused `BaseAgentStrategy` subclass.
- Register an operator-selectable strategy in `AgentStrategies`,
  `AgentStrategyFactory`, and the dashboard configuration metadata.
- Select runtime behavior through `AGENT_STRATEGY`; do not hardcode the active
  strategy.
- Keep strategy construction, request-scoped clients, streaming, and cleanup
  async-correct.
- Keep orchestration responsible for conversation flow and strategy wiring;
  put provider access in connectors and reusable tools in plugins.
- Keep prompts in `src/prompts/`. Preserve strict template rendering and fail
  clearly for missing templates or placeholders.
- Never persist inbound access tokens, credentials, or raw authorization
  material in conversation documents, prompts, telemetry, or logs.
- Preserve conversation partitioning, correlation, audit lifecycle, SSE
  output, and cancellation/error semantics.
- Add focused tests for strategy registration, construction, streaming,
  conversation history, request scope, and cleanup.
- Load `engineering-principles` for changes to strategy boundaries, retrieval,
  identity, or persistence.
