# ADR-0001: Native service identity without delegated fallback

**Status:** Accepted for bounded implementation; not deployed<br>
**Date:** 2026-11-02<br>
**Owners:** Agent Landing Zone maintainers

## Context

The approved product plan adds service-authenticated retrieval without changing
authenticated application admission. Each source retains its own authorization
contract, with user delegation remaining the default.
Authorization safety, native state ownership, explicit configuration and
bounded compatibility take priority over tool breadth.

## Alternatives considered

- **Keep user delegation mandatory:** preserves current behavior but excludes
  the approved use case.
- **Use the legacy REST connector or fabricate a user context:** could bypass
  native connection/state boundaries or expose protected content. Rejected.
- **Use official managed Toolbox with a validated structural service profile:** selected;
  coordinated deployment is an accepted cost.

## Decision

Read `HOSTED_AGENT_AUTH_MODE` from the existing `agent-lz` publication;
`user_delegated` remains the default. `service_identity` requires Hosted,
mandatory admission, and each source's supported service-authentication contract.
Do not impose a Blob-only source or capability allowlist or require empty
permission metadata. Preserve protected source defaults. Tools requiring user
delegation fail explicitly at invocation when OBO is unavailable.
Reject invocation-local delegated Search credentials in service mode.

Consume structural `TOOLBOX_ENDPOINT` from the existing capability publication.
Use official `agent-framework-foundry-hosting==1.0.0b261030` `FoundryToolbox`,
without upgrading the existing core `1.24.0` or Foundry `1.16.0` dependencies.
Agent initialization owns discovery and cleanup; native middleware merges only
the discovered `knowledge_base___knowledge_base_retrieve` function with
independent standalone tools.

The SDK authenticates to Toolbox as the workload with scope
`https://ai.azure.com/.default` and forwards the **current** trusted platform
call ID per operation, including after caller changes. Foundry performs managed
user authorization and KB-specific Search source-token forwarding. Platform
provisions the `knowledge_base` MCP registration and its actual project
`RemoteTool` connection, using `ProjectManagedIdentity`/Search audience.
No manual Search assertion, local user-token acquisition or raw model MCP
fallback remains on the managed KB path. Service mode sends no user assertion
and never falls back to user delegation.

Function middleware translates the documented gateway consent envelope into
official `Content.from_oauth_consent_request`/`UserInputRequiredException`.
Other managed tool failures raise `MiddlewareFailure`, not model-visible
error results or ordinary answers. Official host discovery consent and invocation
consent both emit root `oauth_consent_request`/`incomplete`; service mode rejects
consent via the host's supported empty consent-origin allowlist and hard
failure middleware. The UI suppresses dynamic links and surfaces actionable
denial/manual retry; interactive consent resumption is not implemented.

Default Hosted invocation with retrieval off remains functional. Outbound
UI-to-Foundry OBO, managed Toolbox delegation, and standalone tool authorization
are distinct boundaries; none authorizes substituting service privileges on a
failed protected request.
Keep `RETRIEVAL_AUTH_MODE=native_search`; no second route selector is introduced.

Mode and connection-binding changes are structural and fail on live refresh.
Authorization settings and connection bindings are not restored
from workflow snapshots. Tokens remain invocation-local and nonserializable.

Retain the official `ResponsesHostServer`, session providers and checkpoints.
Its pinned hosting SDK requires genuine platform caller-partition (`user_id`)
and call-ID context even for app-only callers. The partition is opaque storage
scope, not an end-user identity assertion or source ACL. Require gateway context
and fail clearly when absent; never derive it from request metadata, fabricate
end users, or ask the BFF to inject trusted platform headers.

## Consequences

Source/tool compatibility is determined by the source, not a type allowlist. Service
callers share the platform application's storage scope; the UI must enforce
admitted-user ownership of continuation IDs, records, attachments and downloads.
This change does not certify gateway app-only header delivery or live retrieval.

## Adoption and migration

Coordinate with platform ADR-0038 and the native UI: validate the actual common
source and project MCP connection, block deployment on configuration failure,
then update the compatible publications/components together. Do not activate by
changing only a live mode value. Restore the coordinated delegated configuration,
connection and components for rollback; no source policy is automatically
converted in either direction. No API/checkpoint format or release pin changes
are introduced by this local implementation.

## Compliance verification

Focused native configuration/tool/hosting tests exercise the real pinned SDK,
default OBO configuration, structural changes, source-type independence, invocation-time
delegation denial, nonpersistent credentials, real caller-context requirements and
native Responses history. Real Toolbox MCP and Foundry model HTTP mocks prove
two-user current call-ID propagation, agent scope/auth, managed KB calls,
outward consent/failed outcomes, no model continuation after tool denial,
`store=false` model calls, and no credential in model inputs or native state.
Ruff and selected mypy checks cover changed modules.
No Azure provisioning, live inference or image build is acceptance evidence.
Gateway service-caller context, downstream Search-token exchange, actual consent
policies and provider readiness remain external validation requirements.

## Documentation impact

The coordinator updates central authentication, Foundry IQ, deployment and
rollback guidance in `Azure/AI-Landing-Zones`; product guides remain central.

## Review trigger

Review before live adoption, when pinned hosting/Foundry APIs change, or before
supporting another source/tool family or hybrid authorization.
