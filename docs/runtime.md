# Orchestrator runtime guide

Return to the [service README](../README.md). Product deployment and shared
configuration are owned by the
[central documentation](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/)
and its [main-branch source](https://github.com/Azure/AI-Landing-Zones/tree/main/docs/agent-landing-zone).
This guide describes orchestrator `v5.2.2`. Use the platform release manifest
for the compatible component combination.

Operator setup: [dashboard Entra registration](#dashboard-entra-setup),
[classic MCP](#classic-mcp-strategy),
[Work IQ for Microsoft 365](#work-iq-microsoft-365), and
[generic MCP knowledge sources](#generic-mcp-knowledge-sources-preview).

## Configuration and identity

Set `APP_CONFIG_ENDPOINT` in the process environment. Runtime values are loaded
from Azure App Configuration and Key Vault references. The credential chain
tries managed identity, then Azure CLI. `AZURE_CLIENT_ID` selects a
user-assigned identity when present. Local `az login` still needs data-plane
RBAC and connectivity to the configured services.

The [configuration client](../src/connectors/appconfig.py) supplies these
selectors in order:

1. `orchestrator`
2. `agent-app-orchestrator`
3. `gpt-rag-orchestrator`
4. `agent-lz`
5. No label, represented by the SDK selector `label_filter="\0"`

The provider merges later selections over earlier duplicates. The no-label
selection is not a fallback used only when `agent-lz` is absent.
A service-label
value is therefore not necessarily the effective value if `agent-lz` or an
unlabelled duplicate exists. Dashboard writes use `agent-app-orchestrator`.
Read back the effective value after a write. Key lookup uses the exact key
name, without automatic `GPT_RAG_` or `AGENTLZ_` aliases.

Direct environment reads for runtime settings are disabled by default.
`allow_environment_variables=true` enables them ahead of provider values.
Bootstrap variables such as `APP_CONFIG_ENDPOINT` and `AZURE_CLIENT_ID` are
read directly regardless of that setting. The classic entrypoint loads a
local `.env` through `python-dotenv`, but does not load `.env.sample`.
The hosted entrypoint expects process environment configuration.
Remote authentication failure
causes classic startup to exit. Other remote-load failures disable the
provider, but required missing values can still fail consumers. This is not
an offline mode for the complete service.

Configure at least the endpoints and model required by the shared strategy
base: `AI_FOUNDRY_PROJECT_ENDPOINT`, `AI_FOUNDRY_ACCOUNT_ENDPOINT`, and
`CHAT_DEPLOYMENT_NAME`. Classic conversation storage also needs
`DATABASE_ACCOUNT_NAME` and `DATABASE_NAME`.
`CONVERSATIONS_DATABASE_CONTAINER` defaults to `conversations`.
Retrieval, MCP, SQL, telemetry, and prompt storage need their own settings
and permissions when used. The sample environment file is not a complete
deployment contract.

Prefer managed identity and least-privilege RBAC for Azure service access.
Store secrets as Key Vault references rather than plaintext configuration.
Keyless Azure SDK access does not remove inbound API authentication or
downstream user authorization requirements.

### TLS compatibility

`APP_CONFIG_TLS_COMPATIBILITY` is a process-environment opt-in, default `false`.
Only the strings `true` and `false` are accepted, ignoring case and surrounding
spaces. When enabled, `KEY_VAULT_URI` is also required in the environment.
Both endpoints must be plain HTTPS origins without credentials, query,
fragment, or a non-root path.

The compatibility transport applies to the App Configuration provider and
Key Vault reference resolution for that configured vault. It uses a TLS 1.2
minimum, the `prime256v1` curve, and TCP maximum segment size `1200`.
Certificate and hostname verification remain enabled. It does not force TLS
1.2 as a maximum, disable certificate checks, replace credentials, or change
every outbound client. In particular, this is not a general transport setting
for Search, models, MCP, identity token endpoints, configuration writes, or
unrelated vault clients. Custom CA handling is preserved by the adapter.
Platform and network compatibility still require validation in the target
environment. See the [offline transport tests](../tests/test_appconfig_transport.py).

## Strategies

`AGENT_STRATEGY` defaults to `maf_lite` in the execution paths, although
`.env.sample` illustrates `single_agent_rag`. The
[factory](../src/strategies/agent_strategy_factory.py) registers:

| Key | Execution |
| --- | --- |
| `maf_lite` | Microsoft Agent Framework with direct Azure OpenAI model access. The shared base still reads Foundry project/account settings. |
| `maf_agent_service` | Microsoft Agent Framework with Foundry Agent Service. |
| `single_agent_rag` | Foundry Agent Service v2 with intent routing and direct-model paths. |
| `mcp` | Microsoft Agent Framework with request-scoped MCP tools. |
| `multimodal` | Retrieval and model flow with image-aware grounding on the classic Search path. |
| `nl2sql` | Natural-language queries over configured SQL or Fabric datasources. |

`multiagent` exists in the enum but is not registered. `rag` and
`multimodal_rag` are not strategy keys. Hosted eligibility is narrower than
the classic registry.

## Classic API and conversation state

Run `main:app` with `--app-dir src`. `POST /orchestrator` accepts
[`OrchestratorRequest`](../src/schemas.py). A normal request supplies `ask`
and can supply a `conversation_id`. Answers stream over SSE. The same route
handles `type=feedback` payloads rather than starting a new answer.

The [service-auth dependency](../src/dependencies.py) accepts a validated
Entra token in `X-Service-Authorization`, or the existing Dapr/API-key
credentials. Keyless service auth needs `ORCHESTRATOR_AUTH_AUDIENCE` and
`ORCHESTRATOR_ALLOWED_CALLER_IDS` plus tenant configuration. The user
`Authorization` bearer is a separate boundary. `ALLOW_ANONYMOUS` defaults
to `true` for user context, not for bypassing service authentication.
Use `DISABLE_AUTH=true` only for controlled development.

Conversation list, detail, rename, and soft-delete routes are
`GET /conversations`, `GET /conversations/{conversation_id}`,
`PATCH /conversations/{conversation_id}`, and
`DELETE /conversations/{conversation_id}`. They require authenticated
ownership checks. Classic orchestration stores message state and feedback in
Cosmos DB. Some conversation writes run in background tasks, so an emitted
answer is not a durable-write acknowledgement. Automatic profile access is
suspended by the shared strategy base. Do not equate conversation history
with enabled cross-conversation profile personalization.

`ENABLE_DASHBOARD=false` by default. Enabling it mounts dashboard API routes
and serves `/dashboard` when built static assets are available. The dashboard
reads conversation data and can **write allowlisted configuration**. It is
not wholly read-only. With Entra configured, protected dashboard APIs require
the exact `Admin` app role. Version and auth-bootstrap endpoints are exceptions.
Without a tenant, the development dashboard gate is open. A configured tenant
without a client ID is a server misconfiguration, not a safe anonymous fallback.
See [dashboard configuration](../src/api/dashboard_config.py).

### Dashboard Entra setup

The dashboard SPA and API use **one Entra app registration**. Its client ID
is both the MSAL client ID and the backend token audience. Separate SPA and
API registrations are not supported by this configuration model.

1. In **Microsoft Entra ID > App registrations**, select the orchestrator app.
   Under **Authentication**, add the **Single-page application** platform
   with redirect URI `https://<orchestrator-host>/dashboard/`, including the
   trailing slash. Add the corresponding local-origin URI only for local
   development. The SPA uses its current origin plus `/dashboard/` for
   sign-in and post-logout redirects, with Authorization Code and PKCE.
2. Under **Expose an API**, use application ID URI `api://<client-id>` and
   expose the delegated scope `access_as_user`. Grant the required consent
   for that scope. If using another scope on this same API, configure its
   complete value through `OAUTH_AZURE_AD_API_SCOPE`.
3. Under **App roles**, define an enabled role with the exact value `Admin`
   and allowed member type **Users/Groups**. Under **Enterprise applications >
   the orchestrator app > Users and groups**, assign that role to approved
   dashboard operators. Other roles or different casing do not satisfy the gate.
4. Set the effective App Configuration values below, restart the backend,
   and reload `/dashboard/`. Follow the label precedence described above.
   Sign out and sign in again after role-assignment changes so a new token
   reflects the assignment.

| Setting | Default | Required setup |
| --- | --- | --- |
| `ENABLE_DASHBOARD` | `false` | Set `true` to mount the dashboard APIs and available static bundle. |
| `OAUTH_AZURE_AD_TENANT_ID` | Unset | Set the Entra tenant ID to enable dashboard authentication. Leaving it unset opens the development gate. |
| `OAUTH_AZURE_AD_CLIENT_ID` | Unset | Set the app registration's application client ID. Required when the tenant is set. |
| `OAUTH_AZURE_AD_API_SCOPE` | `api://<client-id>/access_as_user` | Optional complete API scope override. It changes the requested scope, not the backend's expected audience. |

`GET /api/dashboard/auth-config` supplies the browser's tenant, client,
authority, and scope at runtime. Request the orchestrator API's access token,
not a Microsoft Graph token. Missing or invalid bearer tokens return `401`.
A valid token without `Admin` in its `roles` array returns `403`.
Tenant configuration without a client ID returns `500`.

The SPA needs no client secret. `OAUTH_AZURE_AD_CLIENT_SECRET` is used by
backend OBO flows when those features are needed, not by dashboard sign-in.
Keep it server-side as a Key Vault reference.

For a controlled negative-role test, **Enterprise application > Properties >
Assignment required? = No** lets a signed-in non-Admin reach the API's `403`
gate. With **Yes**, Entra can reject an unassigned user earlier with
`AADSTS50105`. Choose the production assignment policy deliberately rather
than weakening it just to test the API.

## Hosted execution

Run `api.hosted_entrypoint:app` with `--app-dir src`, for example:

```powershell
.\.venv\Scripts\python -m uvicorn api.hosted_entrypoint:app --app-dir src --host 127.0.0.1 --port 8001
```

This is an entrypoint selection, not a standalone Foundry deployment recipe.
The platform owns hosted registration, ingress, identity passthrough, consent,
and RBAC. `POST /responses` is the canonical protocol.
`POST /invocations` is a compatibility protocol with a different schema.
`GET /readiness` and `GET /health` report the image version and eligible
strategies, not a live dependency certification.

Only `maf_lite`, `maf_agent_service`, `single_agent_rag`, and `mcp` are
eligible. The hosted path does not use classic Cosmos state or perform managed
Foundry Conversations data-plane operations. It requires file-backed prompts
and disables profile memory. The authenticated caller must provide complete,
ordered history on every turn. Responses `input` supports a plain string or
text-only message objects. Multimodal content is rejected.
`store` is forced to `false`. Non-null `previous_response_id` and
`background: true` are rejected with `422`. Caller routing fields such as
`conversation`, `model`, and `session_id` do not select state or the server's
agent definition.

Hosted `mcp` requires a valid platform `x-agent-foundry-call-id`, including
for the protocol's response-management routes. Missing or malformed context
is rejected with `401`. The validated opaque call ID is passed to Toolbox,
which resolves per-user identity. The orchestrator does not convert it into
a user token or grant consent. There is no manual group-filter or service
identity fallback for this Toolbox guard. This does not prove identical
authorization semantics for every non-Toolbox retrieval path.

Hosted generative-AI message capture is disabled by default. Set
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` only after approving
the telemetry data-handling implications.

## Retrieval and ingestion boundaries

`RETRIEVAL_BACKEND` accepts `ai_search` and `foundry_iq`. The
[resolver](../src/util/retrieval_backend.py) caches the selection and falls
back to `ai_search` for empty or unknown values. Restart after changing it.
The selector is used by the MAF Lite, MAF Agent Service, and multimodal
context providers and the shared knowledge-search connector. It is not a
switch that changes every tool or datasource in every strategy.

Foundry IQ needs `KNOWLEDGE_BASE_NAME` and a knowledge-base or Search endpoint.
Its default retrieve API version is `2026-05-01-preview`.
`azureBlob` native sources and `searchIndex` sources are distinct paths.
The multimodal strategy's Foundry IQ provider supplies text grounding, not
the classic image-grounding path. [Work IQ](#work-iq-microsoft-365) and
[generic MCP knowledge sources](#generic-mcp-knowledge-sources-preview) are
additional opt-ins, not default retrieval. They are separate from
`AGENT_STRATEGY=mcp`.

The optional ingestion Container App is a platform deployment choice,
independent of Container Apps or Foundry hosted orchestration. In the current
platform deployment contract, fresh supported native Foundry IQ deployments
default that choice to `false`. Existing and custom selections remain intact.
Deploying ingestion does not select `RETRIEVAL_BACKEND`, register all knowledge
sources, or switch a configured source automatically.

Native Foundry IQ Blob indexing covers its configured corpus. It does not
replace conversation attachment processing, SharePoint synchronization,
NL2SQL metadata ingestion, or every custom format. The connector has a
separate opt-in conversation-upload `searchIndex` source for native Blob
topologies. Keep the required attachment ingestion path when enabling it.
Choose topology in the central deployment guide, not by extrapolating from a
runtime feature flag.

### Classic MCP strategy

Select `AGENT_STRATEGY=mcp` for the request-scoped MCP agent.
Configure these settings through the configuration provider:

| Setting | Default | Behavior |
| --- | --- | --- |
| `MCP_APP_ENDPOINT` | `http://localhost:80` in detected Azure environments | Base MCP server URL. Non-Azure execution overrides even a configured URL with `http://localhost:5000`. |
| `MCP_SERVER_TRANSPORT` | `sse` | Accepts `sse` and `streamable_http`. |
| `MCP_CLIENT_TIMEOUT` | `600` | Connection/read timeout in positive integer seconds. |
| `MCP_APP_APIKEY` | Unset | Optional value sent as `X-API-KEY`. Store it as a Key Vault reference. |
| `AGENT_ID` | Unset | Optional ID passed to the request-scoped agent. |

Azure detection checks for the process variable `CONTAINER_APP_NAME` or
`WEBSITE_INSTANCE_ID`. Outside that detection, run the local MCP server on
port `5000`. In Azure, set `MCP_APP_ENDPOINT` to the actual trusted server.
Use HTTPS outside controlled local development.

The client appends `/sse` for `sse` or `/mcp` for `streamable_http` exactly
once. A conflicting suffix is rejected. For example, an Azure endpoint
`https://mcp.contoso.com` resolves to `https://mcp.contoso.com/sse` by default.
The client sends `user-context` and any configured API key. Hosted execution
also forwards its validated platform call ID as described above.
This client does not enforce the generic Foundry IQ MCP trusted-host policy.

### Work IQ Microsoft 365

Work IQ adds Microsoft 365 grounding through an already registered `workIQ`
knowledge source on the selected Foundry IQ knowledge base. The shipped
settings catalog describes it as a gated preview requiring admin consent on
the Foundry connector. Confirm service availability, provision the source,
and complete that consent before enabling it. A runtime flag does not perform
registration or grant Microsoft 365 permissions.

| Setting | Default | Setup |
| --- | --- | --- |
| `RETRIEVAL_BACKEND` | `ai_search` | Select `foundry_iq` for the retrieval paths described above. |
| `WORK_IQ_ENABLED` | `false` | Set `true` to opt in to Work IQ. |
| `WORK_IQ_KNOWLEDGE_SOURCE_NAME` | Empty | Set the registered Work IQ source name on that knowledge base. |
| `FOUNDRY_IQ_MAX_RUNTIME_SECONDS` | `120` | Retrieve runtime ceiling emitted when a remote or generic MCP source is enabled. |

Work IQ requires per-user OBO authorization. The backend delegation helper
uses `OAUTH_AZURE_AD_TENANT_ID`, `OAUTH_AZURE_AD_CLIENT_ID`, and
`OAUTH_AZURE_AD_CLIENT_SECRET`, plus delegated permissions and consent for the
requested resource. Keep the secret in Key Vault. The Foundry IQ caller
forwards the Search-audience OBO token through
`x-ms-query-source-authorization`. Microsoft 365 enforces the source's user
ACLs natively, so the Work IQ entry has no `filterAddOn`.

At the connector level, an empty source name or absent OBO token skips the
Work IQ entry with a warning. The MAF context provider additionally requires
a retrieval user token when Work IQ is enabled and can fail before retrieve.
Neither path substitutes managed identity for the user. A Toolbox call ID
alone is not an OBO token. `FOUNDRY_IQ_FORWARD_SOURCE_AUTH` is deprecated and
ignored, not a way to enable service-identity fallback.

### Generic MCP knowledge sources preview

This is a separate, default-off Foundry IQ preview, not the classic MCP
strategy or a source-provisioning API. Register each `mcpServer` knowledge
source on the knowledge base first. Its name, server, and tools must match
the metadata you configure here.

| Setting | Default | Setup or constraint |
| --- | --- | --- |
| `RETRIEVAL_BACKEND` | `ai_search` | Select `foundry_iq` for the applicable retrieval paths. |
| `FOUNDRY_IQ_API_VERSION` | `2026-05-01-preview` | This exact version is required when generic MCP is enabled. |
| `FOUNDRY_IQ_MCP_ENABLED` | `false` | Set `true` to enable conversational MCP planning. |
| `FOUNDRY_IQ_MCP_SOURCES_JSON` | `[]` | A non-empty JSON array of source objects is required when enabled. |
| `FOUNDRY_IQ_MCP_REASONING_EFFORT` | `low` | Only `low` and `medium` are accepted. |
| `FOUNDRY_IQ_MCP_TRUSTED_HOSTS` | Empty | Required exact hostname allowlist. Accepts comma/newline-separated hostnames or a JSON array of hostnames. |
| `FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS` | `false` | Keep off unless debug argument logging and its data-handling risks are approved. |
| `FOUNDRY_IQ_MAX_RUNTIME_SECONDS` | `120` | Must be an integer from `30` to `600` for this preview. |

For example, set the trusted hosts to `mcp.contoso.com` and the source JSON
to the complete array below. Replace the illustrative name, host, tool, and
resource scope with the values of your registered source. Do not paste the
JSON into the process environment unless runtime environment overrides are
explicitly enabled.

```json
[
  {
    "name": "monitor-mcp",
    "description": "Read-only Azure Monitor MCP source.",
    "serverURL": "https://mcp.contoso.com/mcp",
    "failOnError": true,
    "maxOutputDocuments": 5,
    "tools": [
      {
        "name": "query_logs",
        "outputParsing": {
          "kind": "json",
          "jsonParameters": {
            "documentsPath": "$.results[*]",
            "includeContext": true
          }
        },
        "inclusionMode": "reranked",
        "maxOutputTokens": 2048
      }
    ],
    "queryHeaders": [
      {
        "name": "Authorization",
        "valueFrom": {
          "kind": "managedIdentity",
          "scope": "api://monitor-mcp/.default"
        }
      }
    ]
  }
]
```

Each source requires a unique `name`, HTTPS `serverURL`, and non-empty
`tools` list. Tool names must be unique within a source. Each tool requires
`name`, `outputParsing`, `inclusionMode` (`reranked` or `always`), and
`maxOutputTokens` from `1` to `8192`. `outputParsing.kind` accepts `auto`,
`none`, `json`, or `split`. Only `json` requires `jsonParameters`, including
`documentsPath` and optional boolean `includeContext`. `split` optionally
accepts `splitParameters` with `textSplitMode` (`pages` or `sentences`),
positive `maximumPageLength`, non-negative `pageOverlapLength`, positive
`maximumPagesToTake`, and non-empty `defaultLanguageCode`. When both lengths
are set, overlap must be smaller than the page length.
Do not mix parsing parameters across kinds.

`failOnError` defaults to `true`, so a reported failure from that source
fails the request. Set it to `false` only if partial answers are acceptable.
Optional `maxOutputDocuments` accepts `1` to `50` per source. The separate
top-level `FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS` can limit final grounding records.
`description` and `queryHeaders` are optional. Unknown fields are rejected.
Server URLs and tool definitions are registration metadata, not sent in the
retrieve body. The request references registered source names and relays
resolved query headers through paired source-specific control headers.

Trusted-host entries are bare hostnames without scheme, port, or path.
Matching is exact after lowercasing and removing a trailing dot, not wildcard
or subdomain matching. Server URLs reject HTTP, embedded credentials, query
strings, fragments, IP literals, single-label names, and reserved/local host
names such as `localhost` or names ending in `.internal` or `.test`.
The hostname allowlist does not replace network egress or DNS controls.

Each query header needs a valid, unique HTTP field name and `valueFrom`
metadata. Reserved and hop-by-hop headers such as `Host` and `Connection`
are rejected.
Literal tokens, API keys, and other credential-value fields are rejected in
the source JSON. Use one of these credential kinds:

| `valueFrom.kind` | Required metadata | Runtime behavior |
| --- | --- | --- |
| `managedIdentity` | `scope`, such as `api://monitor-mcp/.default` | Requests a token through the configured service credential chain, which can use Azure CLI locally. Grant that identity the required access. |
| `obo` | Explicit target-resource `scope` | Exchanges the original incoming user token for that scope. Requires the backend OBO settings described for Work IQ and resource consent. It does not reuse the Search-audience token. |
| `keyVaultSecret` | `secretName` | Reads the named secret from `KEY_VAULT_URI` using the Key Vault credential chain. Grant secret-read access and store the complete header value there. |
| `none` | Neither `scope` nor `secretName` | Omits that header. An empty `queryHeaders` list also adds no credentials. |

These choices do not disable Foundry IQ service authentication or the
registered MCP source's authorization requirements.

`scope` is forbidden for `keyVaultSecret` and `none`. `secretName` is forbidden
for all kinds except `keyVaultSecret`, where it must be `1` to `127` letters,
digits, or hyphens. Missing credentials or a failed OBO exchange fail the
request rather than downgrading to `none`. Normal telemetry omits tool
arguments. The debug opt-in applies bounded recursive redaction, not a
guarantee that arbitrary arguments contain no sensitive data.

For NL2SQL, a least-privilege read-only database principal is the primary
security boundary. Generated-SQL validation is not a replacement for database
permissions.

## Audit and recovery

Orchestrator audit emission defaults to off with `AUDIT_EVENTS_ENABLED=false`.
Enabling it requires `AUDIT_HMAC_KEY`, encoded as exactly 32 random bytes in
Base64, Base64URL, or hexadecimal form. Keep it in Key Vault and out of the
dashboard. `AUDIT_HMAC_KEY_ID` defaults to `v1`. Change the key and ID together
and restart. Pseudonyms across rotations are not directly correlatable.

Events use the existing telemetry pipeline, the `gptrag.audit` logger, and
`agentlz.audit.*` custom-event names. Sensitive capture defaults to off and
also requires an explicit `AUDIT_SENSITIVE_CONTENT_FIELDS` allowlist.
Audit sanitation is defense in depth, not a guarantee that arbitrary content
is safe to export. Audit events are best-effort operational evidence, not an
immutable ledger or a compliance certification.

The [logical v2 schema](../contracts/audit-event-v2.schema.json),
[Application Insights wire schema](../contracts/audit-event-v2.application-insights.schema.json),
and [digests](../contracts/audit-event-v2.sha256) define event compatibility.
Application Insights custom properties are strings. Decode the root
`parent_event_id` sentinel `evt_00000000000000000000000000000000` to null
before correlating events. The v1 files are retained for historical events.

Set `AUDIT_EVENTS_ENABLED=false` and restart to stop new audit events.
This does not remove previously exported telemetry. Reverting an image does
not undo persisted configuration or Cosmos changes. Use the platform's
compatible release pins and separately authorized data/configuration recovery.

## Component image build selection

The `v5.2.2` [PowerShell](../scripts/deploy.ps1) and
[Bash](../scripts/deploy.sh) deployment hooks accept `BUILD_MODE=local` or
`BUILD_MODE=acr-task`, normalized for case and surrounding whitespace.
An unset value does **not** unconditionally mean remote ACR Tasks:

1. An explicit non-empty `BUILD_MODE` takes precedence.
2. Otherwise recognized legacy `USE_DOCKER` values select `local`
   (`true`, `1`, `yes`) or `acr-task` (`false`, `0`, `no`).
3. If still unset, `NETWORK_ISOLATION=true` or a non-empty
   `ACR_TASK_AGENT_POOL` selects `acr-task`.
4. Otherwise a usable Docker command and successful `docker info` select
   `local`. If Docker is unavailable, select `acr-task`.

PowerShell also rejects the Docker Desktop manually paused diagnostic.
It trims the legacy/boolean selectors, while Bash only lowercases those
selectors. Prefer exact values without whitespace for portable configuration.
Unknown legacy `USE_DOCKER` values fall through to automatic selection.
Unknown explicit `BUILD_MODE` values fail.

Explicit `local` fails when Docker is unavailable rather than switching to
remote. Local builds use `linux/amd64`, then push to ACR. `acr-task` uses
`az acr build`. Network-isolated remote builds require
`ACR_TASK_AGENT_POOL`, otherwise selection fails. This documents component
behavior, not a standalone deployment recipe or an assertion that a parent
hook sets `BUILD_MODE`.

Deployment destinations use `AZURE_APP_RESOURCE_GROUP` from App Configuration
under `agent-lz`. Only a successful missing or empty read falls back to
`AZURE_RESOURCE_GROUP` for older environments. Configuration access failures
or multiple destination values stop deployment rather than select another group.
