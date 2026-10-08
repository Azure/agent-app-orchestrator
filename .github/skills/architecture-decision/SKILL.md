---
name: architecture-decision
description: Conducts and records a verifiable Agent Landing Zone Orchestrator architectural decision. Use when a choice alters runtime strategy boundaries, contracts, identity, data, deployment, or operation with meaningful reversal cost.
---

# Agent Landing Zone Orchestrator architectural decision

1. Load the relevant `engineering-principles` references.
2. Define the user or operator outcome, constraints, affected repositories,
   and up to five prioritized characteristics with measurable thresholds.
3. Compare at least two viable alternatives and the option of not changing.
4. Evaluate FastAPI and runtime strategy boundaries, identity, authorization,
   data and conversation compatibility, retrieval and MCP trust, deployment,
   cost, operation, migration, and reversibility.
5. Record the decision using
   [the ADR template](references/adr-template.md).
6. Define fitness functions, adoption order, rollback or roll-forward, and a
   review trigger.

Do not turn a framework, model, or Azure service preference into an
architectural requirement. GitHub Copilot engineering agents are not runtime
agent dependencies. When evidence is missing, record a time-bounded
investigation and its decision criterion instead of guessing.
