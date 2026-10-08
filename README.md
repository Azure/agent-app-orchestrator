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
name: Multi-repo ChatGPT and Enterprise data with Azure OpenAI and AI Search
description: Agent Landing Zone core is a Retrieval-Augmented Generation pattern running in Azure, using Azure AI Search for retrieval and Azure OpenAI large language models to power ChatGPT-style and Q&A experiences.
-->
# Agent Landing Zone Orchestrator

Part of the [Agent Landing Zone](https://github.com/Azure/agent-landing-zone) solution.

The **Agent Landing Zone Orchestrator** service is an agentic orchestration layer built on Azure AI Foundry Agent Service and the Microsoft Agent Framework. It enables agent-based RAG workflows by coordinating multiple specialized agents—each with a defined role—to collaboratively generate accurate, context-aware responses for complex user queries.

### Available Strategies

| Key | Strategy | Description |
|-----|----------|-------------|
| `single_agent_rag` | Single Agent RAG | RAG strategy using Azure AI Foundry Agent Service v2 with dynamic routing and direct LLM bypass. |
| `maf_agent_service` | MAF + Agent Service | Microsoft Agent Framework with Azure AI Foundry Agent Service v2 for server-side threads and tool orchestration, with request-scoped client lifecycle for stable async cleanup. |
| `maf_lite` | MAF Lite | Microsoft Agent Framework with direct Azure OpenAI model access (no Agent Service dependency). |
| `mcp` | MCP | Request-scoped Microsoft Agent Framework strategy supporting legacy SSE and streamable HTTP MCP servers. |
| `nl2sql` | NL2SQL | Natural language to SQL translation strategy for structured data queries. |

### MCP strategy configuration

The `mcp` strategy now runs on Microsoft Agent Framework instead of Semantic
Kernel. Existing SSE deployments remain compatible: keep `AGENT_STRATEGY=mcp`
and the existing MCP configuration keys. The default transport is still `sse`,
and the streamed response contract is unchanged.

Configure these values in Azure App Configuration. Use the
`agent-app-orchestrator` label for an orchestrator-specific override, or the
shared `agent-lz` label when every component should use the same value:

| Key | Default | Purpose |
| --- | --- | --- |
| `MCP_APP_ENDPOINT` | `http://localhost:80` in Azure; local runs use `http://localhost:5000` | Base URL of the trusted MCP server. HTTPS should be used outside local development. |
| `MCP_SERVER_TRANSPORT` | `sse` | MCP transport. Supported values are `sse` and `streamable_http`. |
| `MCP_CLIENT_TIMEOUT` | `600` | Connection and read timeout in seconds. It must be an integer greater than zero. |
| `MCP_APP_APIKEY` | Unset | Optional API key sent to the MCP server as `X-API-KEY`. Store it as a Key Vault reference, not as plain text. |
| `AGENT_ID` | Unset | Optional agent identifier passed to the request-scoped Microsoft Agent Framework agent. |

The orchestrator resolves the transport endpoint from `MCP_APP_ENDPOINT`:

| Transport | Endpoint suffix | Example resolved endpoint |
| --- | --- | --- |
| `sse` | `/sse` | `https://mcp.contoso.com/sse` |
| `streamable_http` | `/mcp` | `https://mcp.contoso.com/mcp` |

You can configure the base URL or include the matching suffix. The orchestrator
adds the suffix once, so both `https://mcp.contoso.com` and
`https://mcp.contoso.com/sse` resolve to the same SSE endpoint. A conflicting
suffix fails during strategy initialization. For example,
`MCP_SERVER_TRANSPORT=streamable_http` with an endpoint ending in `/sse` is
rejected and the error tells the operator to use `/mcp`.

> [!IMPORTANT]
> The orchestrator forwards the caller's `user-context` header and, when
> configured, `X-API-KEY` to this endpoint for every request. Use only a trusted
> MCP server, require HTTPS outside local development, restrict network access,
> and keep `MCP_APP_APIKEY` in Key Vault.

For a controlled rollout, leave existing deployments on `sse`, deploy the new
orchestrator revision, and verify that a representative request discovers and
calls the expected tools. To adopt streamable HTTP, confirm that the server
exposes `/mcp`, then change the transport and endpoint together. Monitor startup
and request logs for invalid transport, conflicting endpoint suffix, timeout,
or connection errors. Because the configuration names and SSE default are
unchanged, you can roll back to the previous orchestrator image without
rewriting the existing SSE configuration.

### Foundry hosted-agent Toolbox identity passthrough

When the orchestrator runs as a Microsoft Foundry hosted agent
(`api.hosted_entrypoint`, canonical `POST /responses`) with
`AGENT_STRATEGY=mcp`, document-security identity is established solely through
the Foundry hosted-agent protocol 2.0 call context — per
[Azure/agent-landing-zone ADR-0001](https://github.com/Azure/agent-landing-zone/blob/main/docs/adr/ADR-0001-hosted-agents.md),
Toolbox OAuth identity passthrough is the required native path and a manual
group-filter fallback is never the default.

`POST /responses` is hosted by Microsoft's
`azure-ai-agentserver-responses` implementation of the Foundry Responses v2
protocol. The orchestrator adapter accepts a plain string `input` or an
ordered array of text-only `role`/`content` message objects, streaming or
synchronous (`stream`) execution, `metadata`, and the platform-injected
`agent_reference`. Every other top-level request field — including the
`conversation`, `model`, and `session_id` routing selectors and any parameter
a future Foundry client adds — is logged and dropped before the SDK parses the
body, so a caller-supplied value can never select routing or state, nor
override the server-side agent definition. Unknown fields are ignored rather
than rejected: a strict allowlist turned every newly injected platform field
into a hosted-agent outage. Optional parameters serialized as explicit `null`
carry no caller intent and are likewise treated as absent.
This preserves the
ADR-0004 stateless contract while remaining compatible with the Foundry
Playground and `azd ai agent invoke`.
A non-null `previous_response_id` is rejected outright with HTTP 422 — silently
dropping it would answer from truncated history instead of failing loudly, so
the authenticated caller must instead send the complete, ordered turn history
as `input` on every request. `store` is
overridden to `False` unconditionally, regardless of what the caller sends or
omits, because the hosted container holds zero managed-Conversations
data-plane RBAC and must never depend on Foundry's managed persistence — see
`CHANGELOG.md`. `background: true` requires `store: true` in the underlying
SDK and is therefore also rejected with HTTP 422. Non-message-object array
items and multimodal (non-text) content are rejected rather than silently
losing role semantics or dropping content. Every rejection is logged with its
reason before the response is returned, so a refused request is diagnosable
from telemetry alone. The protocol host still exposes
response retrieval, input-item listing, cancellation, and deletion, but since
every response is created with `store: False` none of those routes ever
return a previously created response.
`POST /invocations` remains a separate compatibility protocol for the existing
`messages` request schema. Both protocols reuse the same hosted strategy
execution and identity guard after parsing their distinct request contracts.

The runtime image pre-caches its tokenizer during the build so hosted requests
do not require public Blob Storage egress in network-isolated environments.

The hosted Responses server disables generative-AI prompt and completion
capture in OpenTelemetry by default. Set
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` only when the
deployment's data-handling policy explicitly permits sensitive content
telemetry.

The hosted entrypoint exposes `GET /readiness` for Microsoft Foundry readiness
probes. The compatibility `GET /health` route returns the same immutable image
version and hosted-eligible strategy set.

The platform injects two headers on every hosted-agent request:

| Header | Purpose | Forwarded to Toolbox? |
| --- | --- | --- |
| `x-agent-user-id` | Per-user container partition key. | No — container-side only. |
| `x-agent-foundry-call-id` | Opaque per-request call identifier. | Yes — the only supported identity passthrough correlation token. |

Both `POST /responses` and `POST /invocations` capture and strictly validate
`x-agent-foundry-call-id`
(printable ASCII, no spaces or control characters, 256 characters max)
whenever the resolved strategy is Toolbox-backed (currently only `mcp`). A
missing or malformed value is rejected with HTTP 401 before the Responses
storage provider, strategy, or Toolbox client is reached; the same guard covers
response retrieval, input-item listing, cancellation, and deletion. There is no
fallback to service identity or to the legacy `metadata_security_id` manual
filter. The validated call id
then flows unchanged through the runtime-neutral `TurnRequest` into the
strategy and is echoed as the sole outbound identity header on every Toolbox
MCP HTTP call, so Toolbox can resolve the signed-in user and mint per-user
credentials server side.

The container never reads, stores, or forwards the `Authorization` header
(the platform gateway strips it before the request reaches the container),
and it never trusts caller- or model-supplied identity fields or `x-client`
group claims for retrieval security decisions. Neither the call id nor any
authorization material is ever written to application logs. Non-Toolbox
hosted strategies (`maf_lite`, `maf_agent_service`, `single_agent_rag`) are
unaffected and continue to run without a call id.

> [!IMPORTANT]
> Toolbox-side OAuth consent and RBAC grants for the signed-in user are an
> external, platform-managed prerequisite. This orchestrator-side change
> propagates the correlation identity Toolbox needs to resolve per-user
> credentials; it does not itself grant, request, or manage that consent —
> the Toolbox/ingestion service and the Foundry hosted-agent platform must be
> configured and consented for the target user independently.

### Audit event configuration

The orchestrator can emit a versioned, metadata-only-by-default activity trail through its
existing OpenTelemetry and Application Insights connection. Audit events are
disabled by default and use the `gptrag.audit` logger namespace. If regular log
export is disabled with `AZURE_MONITOR_DISABLE_LOGGING=true`, enabling audit
events exports only that namespace.

When `AUDIT_EVENTS_ENABLED=false`, no `agentlz.audit.*` events are emitted, no
audit-only log exporter is enabled, no HMAC key is required, and other
`AUDIT_*` settings are ignored. Existing response bodies, SSE streams,
retrieval results, cache behavior, application logs, traces, and metrics remain
unchanged. The additive server-generated `X-Correlation-ID` response header and
documented Cosmos correlation metadata are independent of audit emission.

Configure these values in Azure App Configuration. Store `AUDIT_HMAC_KEY` as a
Key Vault reference and keep it out of the admin dashboard:

| Key | Default | Purpose |
| --- | --- | --- |
| `AUDIT_EVENTS_ENABLED` | `false` | Enables audit-event-v2 custom events (`agentlz.audit.<event_type>`, `service_name` `agent-app-orchestrator`). Enabling without a valid 256-bit HMAC key fails startup. |
| `AUDIT_HMAC_KEY` | Unset | Base64, Base64URL, or hexadecimal encoding of exactly 32 random bytes. Used only to pseudonymize source, conversation, question, thread, tool, and optional actor identifiers. |
| `AUDIT_HMAC_KEY_ID` | `v1` | Non-secret key version recorded with events to support rotation. Change it together with the key. |
| `AUDIT_SENSITIVE_CONTENT_ENABLED` | `false` | Allows approved sensitive fields to be considered for capture. The allowlist must also be non-empty. |
| `AUDIT_SENSITIVE_CONTENT_FIELDS` | Empty | Comma-separated allowlist from `prompt`, `response`, `source_excerpt`, `tool_arguments`, and `tool_result`. Empty captures none. |
| `AUDIT_ACTOR_PSEUDONYM_ENABLED` | `false` | Adds a keyed pseudonym for the authenticated actor. Raw identity is never placed in audit events, trace context, or baggage. |
| `AUDIT_SOURCE_EVENT_LIMIT` | `25` | Per-request source event limit. Accepted range is 1 through 25. |
| `AUDIT_ADDITIONAL_REDACTED_KEYS` | Empty | Additional comma-separated nested key names that must always be redacted. |

Total and tool budgets are fixed safety limits rather than configuration:

| Budget | Limit |
| --- | --- |
| Audit events per request | 64, including one reserved request terminal and at most one `audit.emission.failed` |
| Tool invocations per request | 16 complete started/terminal pairs |
| Grounding-source events per request | 25 maximum, optionally lowered by `AUDIT_SOURCE_EVENT_LIMIT` |

Tool pairs are reserved atomically, so a started event is not emitted unless
capacity also exists for its terminal event. Detail events are suppressed when
a budget is exhausted, while the request terminal remains reserved. Request
terminal events report `audit_events_omitted`, `source_events_omitted`, and
`tool_invocations_omitted`.

Generate a key locally, write it to Key Vault without displaying it, and clear
the temporary shell variable:

```powershell
$auditKey = python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
az keyvault secret set --vault-name <vault-name> --name audit-hmac-key --value $auditKey --query id --output tsv
$auditKey = $null
```

Once the endpoint handler runs, successful orchestrator and feedback responses
return a new server-generated `X-Correlation-ID`; an inbound value is ignored.
Dependency failures and request-body validation failures that occur before the
handler may not include this header. The identifier is recorded in audit events
only when auditing is enabled. W3C `traceparent` remains the distributed tracing
context and is preserved independently. `X-Correlation-ID` is not an
authentication, authorization, idempotency, proof-of-delivery, or immutability
mechanism.

> [!IMPORTANT]
> Sensitive-content capture can include prompts, responses, retrieved excerpts,
> tool arguments, or tool results. It increases privacy, retention, and access
> control obligations. Tokens, credentials, authorization material, cookies,
> connection strings, SAS values, and private keys are filtered before export
> even when sensitive capture is enabled. This filtering is defense in depth,
> not a data-loss-prevention boundary. Operators must still prevent secrets from
> entering content approved for capture.

To roll back, set `AUDIT_EVENTS_ENABLED=false` and restart the orchestrator.
This stops new audit events and does not delete telemetry already exported.
These best-effort operational events can support customer-owned governance,
security, and incident processes, but they are not an immutable ledger and do
not establish legal or regulatory compliance.

Audit settings are loaded at startup. For HMAC rotation, create a new 256-bit
key, update `AUDIT_HMAC_KEY` and `AUDIT_HMAC_KEY_ID` together, and restart the
orchestrator. Pseudonyms generated before and after rotation cannot be directly
correlated.

Audit serialization is bounded before export:

| Limit | Value |
| --- | --- |
| Serialized logical event | 16 KiB UTF-8 |
| Producer attributes | 60 |
| Metadata strings and nested keys | 512 characters |
| Allowlisted sensitive fields | 2,048 characters |
| Nested depth | 6 |
| Mapping entries inspected | 65, including one truncation lookahead |
| Sequence items inspected | 33, including one truncation lookahead |
| Total nested values inspected | 256 |
| `omitted_fields` / `truncated_fields` entries | 32 |

Optional attributes are removed as needed to satisfy the event-size limit.
`truncated_fields` identifies shortened strings or collections, while
`omitted_fields` identifies unsupported, cyclic, over-depth, unknown, or
size-dropped values. The wire schema permits 64 properties because Azure
Monitor adds transport attributes around the producer's 60-attribute payload.
If a bounded logical event still cannot be serialized safely, the original
event is discarded and one payload-free, constant-safe
`audit.emission.failed` event is attempted for the request.

The reusable v2 JSON Schema for orchestrator and ingestion producers is
[`contracts/audit-event-v2.schema.json`](contracts/audit-event-v2.schema.json).
Version 2 renames the Application Insights event prefix from `gptrag.audit.`
to `agentlz.audit.` and the producer `service_name` to
`agent-app-orchestrator`; field names and semantics are unchanged. The
`audit-event-v1` files remain in [`contracts/`](contracts/) as hash-pinned
history for reading events exported before this change.
The ingestion component reserves exactly these event names, with no aliases:

| Scope | Events |
| --- | --- |
| Run lifecycle | `ingestion.run.started`, `ingestion.run.completed`, `ingestion.run.failed`, `ingestion.run.cancelled` |
| Document lifecycle | `ingestion.document.indexed`, `ingestion.document.rejected`, `ingestion.document.deleted` |

They are part of both the logical and Application Insights wire schemas but are
not emitted by this repository.

Foundry IQ exposes MCP activity only after completion and provides no
pre-invocation callback. For `foundry_iq.mcp_tool`, the producer accepts only
finite, nonnegative elapsed durations of at most 24 hours and reconstructs the
start as the observation time minus that duration. The started event uses the
reconstructed `event_time_utc`; both events set
`timing_source=reconstructed`. These timestamps are approximate and do not
prove when the remote tool began execution. Invalid timing omits the activity
pair and attempts the bounded constant-safe emission-failure event without
failing retrieval.

Application Insights stores custom-event property values as strings. The
corresponding exported shape is documented and tested in
[`contracts/audit-event-v2.application-insights.schema.json`](contracts/audit-event-v2.application-insights.schema.json).
Consumers should parse those properties into the logical v2 types before
validating them against the reusable contract. Logical root events use
`parent_event_id=null`. Because the pinned Azure Monitor exporter drops null
custom properties, the wire adapter encodes logical null as
`evt_00000000000000000000000000000000`; consumers must decode that reserved
sentinel to null and must never join it as an event. Contract artifact SHA-256
digests are recorded in
[`contracts/audit-event-v2.sha256`](contracts/audit-event-v2.sha256).

Query orchestrator audit events in Application Insights:

```kusto
customEvents
| where name startswith "agentlz.audit."
| where tostring(customDimensions.service_name) == "agent-app-orchestrator"
| project timestamp, name, tostring(customDimensions.event_id),
    tostring(customDimensions.correlation_id),
    tostring(customDimensions.parent_event_id),
    tostring(customDimensions.status), tostring(customDimensions.reason_code)
| order by timestamp asc
```

During the transition, include history exported under v1 with
`name startswith "agentlz.audit." or name startswith "gptrag.audit."`.

### NL2SQL datasource security

> [!IMPORTANT]
> When using the `nl2sql` strategy, configure every SQL Server, Azure SQL, or Fabric SQL datasource with a least-privilege read-only principal. Grant only the `SELECT` permissions needed for approved schemas, tables, or views, and do not use admin, owner, contributor, ingestion, or write-capable identities for NL2SQL query execution. The orchestrator validates generated SQL before execution, but database permissions remain the primary security boundary.

### Retrieval backends

The `rag` and `multimodal_rag` strategies use Foundry IQ's Knowledge Base retrieve API. Alongside the existing native `azureBlob` and `searchIndex` (Pattern B) knowledge sources, the orchestrator can optionally query a Microsoft 365 **Work IQ** knowledge source for grounded answers over the caller's Outlook mail, Teams chats, and SharePoint / OneDrive files.

Work IQ is opt-in and off by default:

- Set `WORK_IQ_ENABLED=true` and `WORK_IQ_KNOWLEDGE_SOURCE_NAME=<your Work IQ knowledge source>` to enable it.
- Work IQ requires a per-user on-behalf-of token. When the OBO token is missing the Work IQ source is skipped with a warning; managed-identity fallback is never used for remote knowledge source kinds.
- ACL is enforced natively by Microsoft 365 via the forwarded user token — no `filterAddOn` is emitted for Work IQ.
- Remote kinds can take 40–60 seconds end-to-end; set `FOUNDRY_IQ_MAX_RUNTIME_SECONDS` (default `120`) to control the retrieve runtime ceiling. The value is only emitted when a remote kind is enabled, so Pattern A / Pattern B requests stay byte-identical.

Work IQ is currently a gated preview and requires admin consent plus a Work IQ knowledge source provisioned on the same Azure AI Search service. See the enablement guide in the [Azure/agent-landing-zone](https://github.com/Azure/agent-landing-zone) repo for the end-to-end setup ([issue #543](https://github.com/Azure/agent-landing-zone/issues/543)).

#### Generic MCP Server knowledge sources (Preview)

Foundry IQ can optionally call one or more preprovisioned generic MCP Server
knowledge sources. The feature is disabled by default. When disabled, the
orchestrator keeps the existing minimal-reasoning `intents` request and does not
add MCP runtime, activity, reasoning, or credential headers.

Enable it with these non-secret App Configuration settings:

```dotenv
RETRIEVAL_BACKEND=foundry_iq
FOUNDRY_IQ_MCP_ENABLED=true
FOUNDRY_IQ_MCP_REASONING_EFFORT=low
FOUNDRY_IQ_MCP_TRUSTED_HOSTS=mcp.contoso.com
FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS=false
FOUNDRY_IQ_MCP_SOURCES_JSON=[{"name":"monitor-mcp","description":"Read-only Azure Monitor MCP source.","serverURL":"https://mcp.contoso.com/mcp","failOnError":true,"maxOutputDocuments":5,"tools":[{"name":"query_logs","outputParsing":{"kind":"json","jsonParameters":{"documentsPath":"$.results[*]","includeContext":true}},"inclusionMode":"reranked","maxOutputTokens":2048}],"queryHeaders":[{"name":"Authorization","valueFrom":{"kind":"managedIdentity","scope":"api://monitor-mcp/.default"}}]}]
```

`serverURL`, tools, and output parsing are registration metadata. Retrieve
requests send only each registered knowledge source name. `queryHeaders` is
non-secret runtime credential metadata and is never rendered into the
top-level Search knowledge-source registration. Its values are resolved per
request and forwarded with Azure AI Search's paired
`<knowledge-source>-header-name[N]` and
`<knowledge-source>-header-value[N]` control headers. Supported `valueFrom.kind`
values are:

- `managedIdentity`, with an explicit downstream `scope`
- `obo`, with an explicit downstream `scope` and an authenticated user request
- `keyVaultSecret`, with a Key Vault `secretName`
- `none`, which forwards no credential

Literal header values and secrets are rejected. MCP hosts require an exact
trusted-host match and HTTPS; query strings, localhost, IP literals, userinfo,
fragments, and reserved hosts are rejected. The Search service `Authorization`
header and document-security `x-ms-query-source-authorization` header remain
separate from MCP credentials.

Enabling MCP switches retrieval to one user `messages` entry with low or medium
reasoning, `extractiveData`, activity diagnostics, and the existing bounded
Foundry IQ runtime/document limits. Required-source activity failures and
credential errors fail closed. Optional-source failures can return successful
references as a partial result and are logged without raw results, credentials,
query strings, or tool arguments by default.

This is a preview contract on API `2026-05-01-preview`. MCP-generated tool
arguments are not guaranteed to be semantically safe. Use read-only tools,
least-privilege scopes, bounded time ranges and row counts, and server-side
argument validation and auditing.

Tool arguments are omitted from normal telemetry. When
`FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS=true`, debug telemetry writes only a
bounded, recursively redacted representation. Credential, authorization,
token, cookie, and header values are always redacted, including paired control
header values. Keep this setting disabled in production unless the remaining
argument data is approved for diagnostic logging.

The source JSON schema is:

| Field | Requirement |
| --- | --- |
| `name` | Required, unique registered knowledge source name. |
| `serverURL` | Required HTTPS registration metadata. Its exact host must appear in `FOUNDRY_IQ_MCP_TRUSTED_HOSTS`; it is never sent in retrieve bodies. |
| `tools` | Required non-empty list with unique tool `name` values. Each tool requires `outputParsing`, `inclusionMode`, and `maxOutputTokens`. `outputParsing` is discriminated by `kind`: `auto` and `none` accept no parameter object; `json` requires `jsonParameters` with `documentsPath` and optional `includeContext`; `split` optionally accepts `splitParameters` with only `textSplitMode` (`pages` or `sentences`), positive `maximumPageLength`, non-negative `pageOverlapLength`, positive `maximumPagesToTake`, and non-empty `defaultLanguageCode`. Parameter objects cannot be mixed across kinds. `inclusionMode` only accepts `reranked` or `always`; `maxOutputTokens` must be 1–8192. |
| `failOnError` | Optional, defaults to `true`. A selected source failure fails the request. Set `false` only when partial answers without that source are acceptable. |
| `maxOutputDocuments` | Optional per-source limit from 1–50. The top-level `FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS` still limits final grounding documents. |
| `queryHeaders` | Optional ordered runtime-only metadata. Each entry has a safe `name` and non-secret `valueFrom` metadata; it is not rendered into Search registration. The first resolved header uses the unnumbered pair; later headers use deterministic numeric suffixes. |

For `managedIdentity` and `obo`, `valueFrom.scope` is required.
`keyVaultSecret` requires `valueFrom.secretName`; `none` accepts neither field
and sends no header. MCP retrieval also validates
`FOUNDRY_IQ_MAX_RUNTIME_SECONDS` in the 30–600 range. Invalid enabled
configuration stops retrieval instead of skipping a source.

Track cross-repository provisioning and canonical documentation work in
[Azure/agent-landing-zone#567](https://github.com/Azure/agent-landing-zone/issues/567).

## Documentation

For comprehensive information about the Agent Landing Zone, including architecture details, configuration guides, best practices, troubleshooting resources, deployment guidance, customization options, and advanced usage scenarios, please refer to the [official project documentation](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/).

## Dashboard

The orchestrator ships with an optional admin dashboard mounted at `/dashboard`. It exposes three tabs:

- **Overview**: conversation counts for today, the last 7 days, and the last 30 days; a conversations-over-time chart; average user turns per conversation; and the number of active users.
- **Conversations**: a paginated, newest-first list of conversations across all users, with a detail view that renders the full message history.
- **Configuration**: an allowlisted editor for supported orchestrator settings in Azure App Configuration.

The data comes from the existing conversation/history Cosmos DB container used by the orchestrator (`CONVERSATIONS_DATABASE_CONTAINER` in `DATABASE_NAME`). The dashboard is read-only.

**Enabling the dashboard.** It is disabled by default. Set the App Configuration value `ENABLE_DASHBOARD=true` to mount it. When `ENABLE_DASHBOARD=false` (the default), the `/dashboard` HTML page and every `/api/dashboard/*` route are not registered at all.

**Access control.** When authentication is on (`OAUTH_AZURE_AD_TENANT_ID` is configured), the entire `/api/dashboard/*` surface — except the small `/api/dashboard/version` and `/api/dashboard/auth-config` endpoints used by the SPA at bootstrap — requires the caller's bearer token to include the `Admin` app role. The `/dashboard` HTML page itself is served openly so the SPA can load, call `/api/dashboard/auth-config`, and either sign the user in via MSAL (Authorization Code + PKCE) or render an access-denied state on a 403 response. When authentication is off, the dashboard is open like the rest of the app in development.

**Sign-in configuration.** The SPA reads its runtime auth configuration from `GET /api/dashboard/auth-config`, which is derived from these App Configuration keys under the `agent-app-orchestrator` label:

| Key | Required | Purpose |
| --- | --- | --- |
| `OAUTH_AZURE_AD_TENANT_ID` | Yes, to enable the gate | Entra tenant id. When unset, the SPA renders without MSAL and `require_admin` is a no-op. |
| `OAUTH_AZURE_AD_CLIENT_ID` | Yes when tenant is set | Application (client) id of the orchestrator API app registration. |
| `OAUTH_AZURE_AD_API_SCOPE` | Optional | Override for the scope the SPA requests. Defaults to `api://<OAUTH_AZURE_AD_CLIENT_ID>/access_as_user`. |

Use a single Microsoft Entra app registration for both the dashboard SPA and the orchestrator API. The same application (client) id is the MSAL `clientId`, the backend token audience, and the `<client-id>` segment in the default scope `api://<client-id>/access_as_user`. Using separate SPA and API app registrations requires a different configuration model.

The App Registration also needs a **Single-page application** redirect URI pointing at `https://<host>/dashboard/` (trailing slash), an exposed `access_as_user` scope, and an `Admin` app role assigned to every user who should see the dashboard. See the Agent Landing Zone docs: [Operations](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/operations/).

Define the role under **Microsoft Entra ID > App registrations > <your app> > App roles**. Create exactly one app role with value `Admin` and allowed member type `Users/Groups`. The backend checks the token's `roles` claim for the exact case-sensitive value `Admin`; other role names or casing are rejected. Assign users under **Enterprise applications > <your app> > Users and groups** by selecting the `Admin` role.

Keep the Enterprise Application setting **Assignment required?** set to **No** when validating the app-level 403 path. With **No**, a signed-in user without the `Admin` app role reaches the dashboard API and receives the dashboard's access-denied state. With **Yes**, Entra blocks the sign-in earlier with `AADSTS50105`, which is useful for strict production gating but does not validate the dashboard's own 403 handling.

After adding or removing the `Admin` role assignment, sign out completely and sign in again. Existing access tokens do not gain or lose `roles` claims retroactively.

Setting only `OAUTH_AZURE_AD_TENANT_ID` is rejected as a server misconfiguration. The dashboard never skips audience validation or silently falls back to unauthenticated mode when auth is partially configured.

**Token scope.** The frontend must request an access token with the orchestrator's own API scope (`api://<client_id>/access_as_user` by default), not a Microsoft Graph scope. App roles are issued in the `roles` claim of an access token only when the token is requested for the application that defines those roles, so a Graph-scoped token will not surface the `Admin` role and every dashboard call will return 403.

**Building the dashboard bundle.** Production builds happen automatically as part of the `Dockerfile` (an MCR base image stage using Node.js 20 runs `npm run build` and copies the static assets into `src/static`). For local development you can run the Vite dev server with hot reload:

```bash
cd frontend
npm install
npm run dev
```

The dev server proxies `/api` to `http://localhost:9000`, which is where the orchestrator listens locally.

## Prerequisites

Before deploying the application, you must provision the infrastructure as described in the [Agent Landing Zone](https://github.com/Azure/agent-landing-zone) repo. This includes creating all necessary Azure resources required to support the application runtime.

<details markdown="block">
<summary>Click to view <strong>software</strong> prerequisites</summary>
<br>
The machine used to customize and or deploy the service should have:

* Azure CLI: [Install Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
* Azure Developer CLI (optional, if using azd): [Install azd](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd)
* Git: [Download Git](https://git-scm.com/downloads)
* Python 3.12: [Download Python 3.12](https://www.python.org/downloads/release/python-3120/)
* Docker CLI: [Install Docker](https://docs.docker.com/get-docker/)
* VS Code (recommended): [Download VS Code](https://code.visualstudio.com/download)
</details>

## How to deploy the orchestrator service

Make sure you're logged in to Azure before anything else:

```bash
az login
```

### Deploying the app with azd (recommended)

Initialize the template:
```shell
azd init -t Azure/agent-app-orchestrator 
```
> [!IMPORTANT]
> Use the **same environment name** with `azd init` as in the infrastructure deployment to keep components consistent.

Update env variables then deploy:
```shell
azd env refresh
azd deploy 
```
> [!IMPORTANT]
> Run `azd env refresh` with the **same subscription** and **resource group** used in the infrastructure deployment.

Aqui está uma versão mais clara, direta e consistente da instrução:

### Deploying the app with a shell script

To deploy using a script, first clone the repository, set the App Configuration endpoint, and then run the deployment script.

##### PowerShell (Windows)

```powershell
git clone https://github.com/Azure/agent-app-orchestrator.git
$env:APP_CONFIG_ENDPOINT = "https://<your-app-config-name>.azconfig.io"
cd agent-app-orchestrator
.\scripts\deploy.ps1
```

## Found an Issue?

Encountered an error or bug? Help us improve the quality of this accelerator by reporting issues or suggesting enhancements on our **[GitHub Issues page](https://github.com/Azure/agent-landing-zone/issues)**. Your feedback helps make Agent Landing Zone better for everyone!

## Previous Releases

> [!NOTE]  
> For earlier versions, use the corresponding release in the GitHub repository (e.g., v1.0.0 for the initial version).

## 🤝 Contributing

We appreciate contributions! See [CONTRIBUTING](https://github.com/Azure/agent-landing-zone/blob/main/CONTRIBUTING.md) for guidelines on submitting pull requests.

## Trademarks

This project may contain trademarks or logos. Authorized use of Microsoft trademarks or logos must follow [Microsoft’s Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general). Modified versions must not imply sponsorship or cause confusion. Third-party trademarks are subject to their own policies.
