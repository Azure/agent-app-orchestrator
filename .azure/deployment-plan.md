# Azure Deployment Preparation Plan

## Status

Ready for Validation

## Scope

Prepare real Microsoft Entra tenant validation for PR #260, which adds MSAL sign-in and `Admin` app-role authorization to the orchestrator admin dashboard.

This plan does not authorize deployment, merge, release, deletion, or Azure/Entra mutation. The next step is explicit validation approval, then `azure-validate`, then `azure-deploy`.

## Azure context

- Subscription: `9788a92c-2f71-4629-8173-7ad449cb50e1` (`mcaps-paulolacerda`)
- Tenant: `16b3c013-d300-468d-ac64-7eda0820b6d3`
- Preferred validation location: `australiaeast`, matching the existing Entra redirect URI and prior validation endpoint.

## Safety constraints

- No resource-group deletion is authorized by this plan.
- Before any future deletion consideration, run:
  - `az group show --subscription 9788a92c-2f71-4629-8173-7ad449cb50e1 --name <rg> --query tags`
- Never delete a resource group tagged `keep=true`.
- Never delete resource groups by substring matching such as `gpt-rag` or `gptrag`.
- Only delete a validation environment by exact resource group name after explicit approval.

## Workspace analysis

- Repository: `Azure/agent-app-orchestrator`
- Branch: `feature/dashboard-msal-signin`
- PR: `Azure/agent-app-orchestrator#260`
- Base branch: `develop`
- Application: Python 3.12 FastAPI orchestrator with a Vite/React admin dashboard served from `/dashboard/`.
- Deployment artifacts already present:
  - `Dockerfile` builds the dashboard bundle in a Node 20 stage and copies it into the Python runtime image.
  - `azure.yaml` exists and points to azd hooks/scripts, but this repository does not carry standalone `infra/` files. Full Agent Landing Zone environment creation is owned by the main Agent Landing Zone infrastructure repository.
- Preparation mode: MODIFY existing application and validate a PR image against a real tenant.
- Recipe: use existing Agent Landing Zone deployment flow for environment provisioning, then deploy the orchestrator container image/revision from this branch. If no reusable environment exists, recreate a controlled validation environment from the main Agent Landing Zone infrastructure instead of modifying unrelated resource groups.

## Current environment inventory

The previously planned resource group `rg-gptrag-546val` is not present in subscription `mcaps-paulolacerda` as of this preparation pass:

- `az group show --name rg-gptrag-546val` returned `ResourceGroupNotFound`.
- No matching Container Apps or App Configuration stores were found by read-only resource listing in this subscription.

Candidate Agent Landing Zone resource groups found in the subscription:

| Resource group | Location | Tags | Read-only finding |
| --- | --- | --- | --- |
| `rg-gptrag-fiqzt2-sdc06291745` | `swedencentral` | `azd-env-name=gptrag-fiqzt2-sdc06291745` | Candidate only, not selected. |
| `rg-gptrag-az700-zta-063014` | `swedencentral` | `azd-env-name=gptrag-az700-zta-063014` | Candidate only, not selected. |
| `rg-gptrag-fiqzt2-06290951` | `switzerlandnorth` | `azd-env-name=gptrag-fiqzt2-06290951` | Candidate only, not selected. |
| `rg-gptrag-fiqzt-aue-06291815` | `australiaeast` | `azd-env-name=gptrag-fiqzt-aue-06291815` | Contains only `srch-2sud2hrjnye46`; not a complete orchestrator validation environment. |
| `rg-gptragv342val` | `australiaeast` | `azd-env-name=gptragv342val` | Contains network/search resources; no Container App/App Configuration inventory was found in this pass. |

Conclusion: do not assume the old validation environment is available. Validation should either:

1. Recreate a new exact validation environment, for example `rg-gptrag-546val2`, from the main Agent Landing Zone infrastructure, or
2. Use a user-approved existing environment only after confirming it has the orchestrator Container App, ACR, App Configuration, managed identity, Cosmos, and required Agent Landing Zone dependencies.

## Entra application inventory

Use the existing single-tenant app registration:

- Display name: `gpt-rag-546val-dashboard`
- Application (client) id: `b621d3a9-03e7-4cf1-80f6-47a263b751c1`
- Application object id: `1c50cdd8-d331-4916-9279-80336bef98d2`
- Enterprise application object id: `737526d9-dbae-4e28-934c-c582005cce94`
- Tenant: `16b3c013-d300-468d-ac64-7eda0820b6d3`
- Supported accounts: single tenant
- Application ID URI: `api://b621d3a9-03e7-4cf1-80f6-47a263b751c1`
- Scope: `access_as_user`
- App role: value `Admin`, case-sensitive, users/groups, enabled
- Enterprise Application `Assignment required?`: `No`
- Known redirect URI:
  - `https://ca-5hhmdzbxo4x4q-orchestrator.thankfulwave-c9aba52a.australiaeast.azurecontainerapps.io/dashboard/`

Before E2E, update the SPA redirect URI if the recreated Container App gets a different FQDN. The redirect URI must exactly match the browser URL and include the trailing slash.

## Runtime configuration required

Set these Azure App Configuration key-values under label `gpt-rag-orchestrator` in the selected validation environment:

| Key | Value |
| --- | --- |
| `ENABLE_DASHBOARD` | `true` |
| `OAUTH_AZURE_AD_TENANT_ID` | `16b3c013-d300-468d-ac64-7eda0820b6d3` |
| `OAUTH_AZURE_AD_CLIENT_ID` | `b621d3a9-03e7-4cf1-80f6-47a263b751c1` |
| `OAUTH_AZURE_AD_API_SCOPE` | `api://b621d3a9-03e7-4cf1-80f6-47a263b751c1/access_as_user` |

Also confirm the orchestrator Container App has:

- `APP_CONFIG_ENDPOINT=https://<store>.azconfig.io`
- `AZURE_CLIENT_ID=<uami-client-id>` if a user-assigned managed identity is used
- Managed identity permission to read App Configuration
- If testing dashboard Configuration writes, managed identity permission equivalent to App Configuration Data Owner

Important: check whether the legacy label `orchestrator` contains any of the same keys. If it does, remove or align those values only with explicit approval, because legacy label precedence can override `gpt-rag-orchestrator`.

Restart or create a new Container App revision after changing `ENABLE_DASHBOARD`, because the dashboard route registration is evaluated at startup.

## Validation deployment approach

1. Confirm the target environment choice with Paulo:
   - Recreate a new validation environment from Agent Landing Zone infrastructure, or
   - Use an existing complete environment after read-only inventory confirms it is safe.
2. Build an orchestrator image from the rebased PR branch.
3. Push the image to the environment ACR.
4. Deploy a new orchestrator Container App revision using that image.
5. Set or verify the App Configuration key-values above.
6. Update the Entra SPA redirect URI to the final `/dashboard/` URL if needed.
7. Run E2E checks below.

## E2E test matrix

### Baseline auth opt-out

Temporarily omit `OAUTH_AZURE_AD_TENANT_ID` in a non-production validation revision.

Expected:

- `/dashboard/` loads without MSAL sign-in.
- `GET /api/dashboard/auth-config` returns `auth_enabled:false` or equivalent disabled state.

### Admin user

Use Paulo's tenant account with the `Admin` app role assigned.

Expected:

- Sign-in redirects back to `/dashboard/`.
- `/api/dashboard/overview`, `/api/dashboard/conversations`, and `/api/dashboard/config` send `Authorization: Bearer <token>` and return 200.
- Local JWT inspection shows:
  - `aud` is `b621d3a9-03e7-4cf1-80f6-47a263b751c1` or `api://b621d3a9-03e7-4cf1-80f6-47a263b751c1`
  - `scp` contains `access_as_user`
  - `roles` contains `Admin`
- Sign out clears the session and returns to the sign-in state.

Do not paste tokens into external sites. Decode locally only.

### Non-admin user

Use Gonzalo Becerra or another test user without the `Admin` app role. If Gonzalo currently has `Admin`, remove the assignment only for the test and restore it afterward with explicit approval.

Expected with Enterprise Application `Assignment required? = No`:

- Entra sign-in succeeds.
- The first protected dashboard API call returns 403.
- The SPA shows `Access denied`, the signed-in account, and `Sign out and try another account`.
- No administrative dashboard data is rendered.

### Optional 401 path

Expire or invalidate the MSAL session and call a protected endpoint.

Expected:

- The SPA shows session-expired/sign-in-again behavior or redirects through MSAL.
- The app never downgrades to an anonymous dashboard API call.

## Documentation updates included in this PR

README dashboard guidance now explains:

- The required single App Registration model for SPA and API.
- Where to define app roles: App registrations > application > App roles.
- Where to assign app roles: Enterprise applications > application > Users and groups.
- Why only `Admin` exists and why the value is case-sensitive.
- Why `Assignment required? = No` is needed to test the app-level 403 path.
- Why users must fully sign out and sign in after role assignment changes.

## Merge and release sequence after validation

1. Complete Azure E2E validation and record results in PR #260.
2. Mark PR #260 ready for review if validation passes.
3. Merge PR #260 into `develop`.
4. Prepare the next `Azure/agent-app-orchestrator` release branch from `develop`, following this repository's `AGENTS.md` release rules.
5. Tag and publish the orchestrator release with the GitHub Release title exactly equal to the tag, for example `vX.Y.Z`.
6. If the parent `Azure/agent-landing-zone` repository pins or consumes the orchestrator version, update it in a follow-up PR/release after the orchestrator release is available.

## Planning checklist

- [x] Analyze workspace and deployment recipe.
- [x] Inspect Azure context and prior environment read-only.
- [x] Inventory reusable resources, tags, configuration, identity, and endpoints where available.
- [x] Define Entra SPA/API and Admin-role prerequisites.
- [x] Define admin, non-admin, and optional 401 E2E scenarios.
- [x] Incorporate operator documentation improvements.
- [x] Confirm that the prior `rg-gptrag-546val` environment is not currently available.
- [x] Draft merge and release sequence.
- [x] Finalize this plan for explicit approval before validation/deployment.
