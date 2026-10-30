---
name: documentation-consistency
description: Keeps Agent Landing Zone Orchestrator user and operator documentation aligned with shipped behavior. Use for runtime strategies, APIs, configuration keys, deployment, defaults, operations, or breaking changes.
---

# Agent Landing Zone Orchestrator documentation consistency

Product, operator, and developer guides live only in
[`Azure/AI-Landing-Zones` on `main`](https://github.com/Azure/AI-Landing-Zones/tree/main/docs/agent-landing-zone)
and are published at https://azure.github.io/AI-Landing-Zones/agent-landing-zone/.
Keep engineering instructions, governance policies, ADRs, and executable
contracts local.

1. Identify the user or operator behavior that changed.
2. Search the documentation source for the feature, API, strategy,
   configuration key, parameter, component, and previous terminology.
3. Update every affected page in the same coordinated change.
4. Register new pages under `nav:` in the central repository's `mkdocs.yml`.
5. Keep component READMEs concise and link to published topics. Do not
   reintroduce local product, operator, or developer guides.
6. Ensure examples match current defaults, supported runtime strategies,
   configuration labels, authentication, deployment modes, and rollback.
7. Update this repository's README links when central topics change. Local
   startup and testing guidance belongs in
   [local development](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/local-development/#orchestrator)
   and [application testing](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/application-testing/#orchestrator).
8. Report the central documentation commit or pull request in the
   implementation handoff.

A user-visible change is incomplete until documentation is updated or a search
demonstrates that no published page is affected.
