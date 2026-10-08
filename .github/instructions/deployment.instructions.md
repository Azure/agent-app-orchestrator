---
applyTo: "scripts/**/*.ps1,scripts/**/*.sh,.azure/**,azure.yaml,Dockerfile,infra/**,.devcontainer/**"
---

# Deployment and lifecycle assets

- Keep PowerShell and shell deployment paths behaviorally equivalent.
- Treat lifecycle ordering, `azd` environment reuse, and environment-variable
  propagation as public deployment behavior.
- Preserve compatibility with the infrastructure provisioned by
  `Azure/agent-landing-zone`.
- Do not edit generated infrastructure content when an owning source or
  template exists; change the authoritative input instead.
- Quote paths and external input safely. Never echo credentials, tokens,
  connection strings, or private Azure validation environment names.
- Surface failed prerequisites, authentication, provisioning, and deployment
  steps. Do not continue with a success-shaped fallback outside an explicitly
  documented non-production policy.
- Keep the Python 3.12 service and Node dashboard build stages reproducible.
- Validate syntax and behavior for every changed platform variant.
- Load `documentation-consistency` when deployment steps, requirements,
  defaults, or operator recovery change.
