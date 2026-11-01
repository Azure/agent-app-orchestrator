<!--
page_type: sample
languages:
- azdeveloper
- powershell
- bicep
products:
- azure
- azure-ai-foundry
- azure-openai
- azure-ai-search
urlFragment: agent-app-orchestrator
name: Agent Landing Zone Orchestrator
description: Python orchestration service for Agent Landing Zone conversational agents.
-->
# Agent Landing Zone Orchestrator

The Python 3.12 native Microsoft Agent Framework runtime for
[Agent Landing Zone](https://github.com/Azure/agent-landing-zone). It defines
`agent-lz-conversation` and the human-reviewed `agent-lz-research-review` graph, with
independent optional Search, Foundry IQ, MCP, NL2SQL and multimodal capabilities.
Foundry Responses hosting owns Hosted state; the official Durable Extension
and Durable Task Scheduler own Container Apps execution. There is no classic
service bootstrap, state migration or shared host protocol.

The native feature requires coordinated platform/UI changes and a component
major release. Classic strategy/HTTP/admin routes are retired, not fallbacks.
Previous data is not converted or deleted: rollback uses the previous deployment
and its own state. See the [native executable contract](contracts/native-runtime.json).
Delegated DAX/multimodal adapters remain available to the invocation-local identity
seam, but the native hosts do not yet supply those delegated tokens; they fail closed.
Local SDK tests are not evidence of deployed durability or user/source isolation.

## Documentation

Product, operator, and developer guides are maintained only in
[Azure/AI-Landing-Zones on `main`](https://github.com/Azure/AI-Landing-Zones/tree/main/docs/agent-landing-zone).

- [Application overview](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/application/)
- [Hosted agents](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/hosted-agents/)
- [Agent strategies](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/agent-strategies/)
- [Local orchestrator development](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/local-development/#orchestrator)
- [Application APIs](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/application-api/)
- [Monitoring and audit](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/application-monitoring/)

## Contributing and support

Use the local [engineering contract](AGENTS.md),
[executable contracts](contracts/), and
[contribution guidelines](https://github.com/Azure/agent-landing-zone/blob/main/CONTRIBUTING.md).
For help, see [support](https://github.com/Azure/agent-landing-zone/blob/main/SUPPORT.md).
Report vulnerabilities through the [security policy](SECURITY.md).

## Legal

This project is licensed under the [MIT License](LICENSE.md).
Use of Microsoft trademarks is subject to
[Microsoft's trademark guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/).
