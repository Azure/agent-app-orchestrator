# Agent Landing Zone Orchestrator architecture

## Purpose and runtime flow

Agent Landing Zone Orchestrator is the Python 3.12 FastAPI runtime component of the
Agent Landing Zone solution. At a high level it:

1. validates the HTTP request and identity;
2. creates an `Orchestrator` for the conversation;
3. reads `AGENT_STRATEGY` from Azure App Configuration;
4. builds the registered `BaseAgentStrategy` implementation through
   `AgentStrategyFactory`;
5. streams the strategy response over SSE;
6. persists conversation state in Cosmos DB; and
7. records bounded telemetry and optional audit events.

GitHub Copilot engineering agents in `.github/agents/` assist maintainers.
They are repository tooling and never participate in this runtime flow.

## Boundaries

- `src/main.py` composes the application and legacy top-level routes.
- `src/api/` owns focused HTTP routers.
- `src/orchestration/` owns conversation flow and strategy wiring.
- `src/strategies/` owns runtime agent behavior and strategy-specific state.
- `src/connectors/` owns Azure and external service protocols.
- `src/plugins/` owns reusable runtime tools.
- `src/prompts/` owns prompt templates and variants.
- `src/telemetry/` owns tracing, logging, correlation, and audit contracts.
- `src/schemas.py` and `contracts/` own typed compatibility boundaries.
- `frontend/` owns the optional administration dashboard.
- deployment assets own packaging and lifecycle behavior, not application
  policy.

## Design questions

Before changing a boundary, ask:

1. Which layer and repository own the behavior?
2. Is this HTTP transport, orchestration, runtime strategy, provider access,
   reusable tool behavior, configuration, persistence, or a shared contract?
3. Which identities, tokens, data, and trust boundaries are crossed?
4. Does it preserve SSE, conversation, authorization, telemetry, and dashboard
   contracts?
5. Is the client lifecycle singleton, process-scoped, or request-scoped, and
   how is async cleanup guaranteed?
6. What is the rollout, rollback, or roll-forward path?

Prefer a focused strategy, connector, router, plugin, or helper over
conditionals in an unrelated module. Prefer explicit typed contracts over
implicit dictionaries or duplicated configuration knowledge.

## Sources of truth

- Read runtime strategy names from `AgentStrategies` and active builders from
  `AgentStrategyFactory`.
- Read configuration precedence and defaults from the implementation and
  dashboard metadata.
- Read API behavior from FastAPI routes and Pydantic schemas.
- Read audit semantics from `contracts/` and their tests.
- Read release state from `VERSION` and `CHANGELOG.md`.
- Read user-facing product documentation from the `Azure/agent-landing-zone` docs branch.
