# Infrastructure ownership

This component's `main.bicep` declares a resource-group scope but creates no
resources. [Agent Landing Zone](https://github.com/Azure/agent-landing-zone)
owns the deployment topology, resources, identities, configuration, and runtime
version pins.

The component's `azure.yaml` and deployment scripts require an existing
compatible environment. They are not a standalone infrastructure template.
See [full-stack deployment](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/deploy-full-stack/)
and [local development](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/local-development/).
