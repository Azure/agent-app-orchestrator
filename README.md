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

The Python 3.12 service for conversational agent execution in
[Agent Landing Zone](https://github.com/Azure/agent-landing-zone). It selects a
configured strategy, streams answers, and integrates models, retrieval, and
tools. Classic execution uses FastAPI and Cosmos DB conversation storage.
The separate Microsoft Foundry hosted entrypoint is stateless.

## Documentation and release scope

- [Product documentation](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/)
  covers architecture, deployment, and cross-component setup.
- [Canonical documentation source](https://github.com/Azure/AI-Landing-Zones/tree/main/docs/agent-landing-zone)
  is on `Azure/AI-Landing-Zones` `main`, not a legacy documentation branch.
- [Service configuration, API, identity, and operations](docs/runtime.md)
  describes this component's implementation and limitations.
- [Audit schemas and integrity pins](contracts/) define the shared event shapes.

Agent Landing Zone `4.3.0` pins orchestrator
[`v5.2.1`](https://github.com/Azure/agent-app-orchestrator/tree/v5.2.1)
at `27f952fcf8d48a2ba6267c237ea6bea8361fedd2`. These runtime notes describe
that revision. Do not treat unreleased `develop` behavior as shipped.

## Run this service locally

First provision or obtain compatible infrastructure through Agent Landing Zone.
This repository does not provision a standalone resource set. Use a development
App Configuration store, appropriate Azure RBAC, and network access to its
dependencies. A successful server start is not an end-to-end readiness test.

From this repository's root, with Python 3.12 and Azure CLI installed, run:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
az login
$env:APP_CONFIG_ENDPOINT = "https://<app-configuration-name>.azconfig.io"
.\.venv\Scripts\python -m uvicorn main:app --app-dir src --host 127.0.0.1 --port 9000
```

Use actual configured resource and model values, not the placeholders in
[`.env.sample`](.env.sample). The service does not automatically load that file.
The [runtime guide](docs/runtime.md#configuration-and-identity) explains required
settings and the explicit environment-variable opt-in. SQL strategies also
need the ODBC dependencies installed by the [Dockerfile](Dockerfile).

Classic requests use `POST /orchestrator`. User bearer authentication and
service authentication are separate requirements. See the
[API and security boundaries](docs/runtime.md#classic-api-and-conversation-state)
before calling it. The image's default command serves classic mode on port
`8080`. Hosted execution requires a different command and the Foundry platform
prerequisites described in the [hosted section](docs/runtime.md#hosted-execution).

## Retrieval and optional ingestion

`RETRIEVAL_BACKEND` defaults to `ai_search` in this service. Platform deployment
defaults can supply a different value. Foundry IQ uses separately configured
knowledge sources. Deploying the optional ingestion Container App does not
change this backend or select sources.

Fresh supported native Foundry IQ deployments default the ingestion Container
App deployment control to `false`, independently of Container Apps or hosted
orchestration. Existing and custom selections are preserved. Native Blob
ingestion is not a replacement for conversation attachments or every source
type. See [retrieval boundaries](docs/runtime.md#retrieval-and-ingestion-boundaries)
before choosing a topology.

## Contributing

Updating `develop` does not deploy to Azure. Dev deployment is opt-in through
the **Manual Dev Deployment** workflow in GitHub Actions; run it explicitly
against the intended branch only when a deployment is authorized.

Use the repository's [engineering contract](AGENTS.md) and
[contribution guidelines](https://github.com/Azure/agent-landing-zone/blob/main/CONTRIBUTING.md).
The [deprecated evaluations folder](evaluations/README.md) links to external
evaluation guidance. No evaluation gate is enabled merely by following that link.
