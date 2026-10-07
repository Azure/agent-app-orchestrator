---
name: documentation-consistency
description: Keeps GPT-RAG Orchestrator user and operator documentation aligned with shipped behavior. Use for runtime strategies, APIs, configuration keys, deployment, defaults, operations, or breaking changes.
---

# GPT-RAG Orchestrator documentation consistency

User-facing documentation lives on the `docs` branch of `Azure/GPT-RAG` and
is published at https://azure.github.io/AI-Landing-Zones/agent-landing-zone/.

1. Identify the user or operator behavior that changed.
2. Search the documentation source for the feature, API, strategy,
   configuration key, parameter, component, and previous terminology.
3. Update every affected page in the same coordinated change.
4. Register new pages under `nav:` in `mkdocs.yml`.
5. Keep this service README focused; link to the published site instead of
   creating a second long-lived source of product truth.
6. Ensure examples match current defaults, supported runtime strategies,
   configuration labels, authentication, deployment modes, and rollback.
7. Update this repository's README when service-local setup or contracts are
   intentionally documented here.
8. Report the documentation branch or pull request in the implementation
   handoff.

A user-visible change is incomplete until documentation is updated or a search
demonstrates that no published page is affected.
