# Changelog

## [v5.1.0] - 2026-10-05

### Added

- **Keyless service authentication (ADR-0019).** The orchestrator now accepts
  a Microsoft Entra access token in the `X-Service-Authorization: Bearer`
  header, validated against `ORCHESTRATOR_AUTH_AUDIENCE`, the tenant issuer,
  and the `ORCHESTRATOR_ALLOWED_CALLER_IDS` allow list (`oid`/`azp`/`appid`).
  Callers authenticate with their managed identity; no shared secret is
  needed. The `Authorization` header stays reserved for the user OBO token.
  The Dapr token and `X-API-KEY` remain as fallbacks.

## [v5.0.0] - 2026-10-03

### Changed

- **Agent Landing Zone dual-read (Azure/GPT-RAG#695).** For one release, App
  Configuration loads the shared `agent-lz` label ahead of the legacy
  `gpt-rag` label, and `AGENTLZ_*` keys (environment and App Configuration)
  take precedence over their `GPT_RAG_*` equivalents, which remain fallbacks.
  The OpenTelemetry `service.name` is now `agentlz.orchestrator`.
- **Audit events move to the `audit-event-v2` contract (breaking for audit
  queries).** Orchestrator audit records now use `customEvents.name` values
  under `agentlz.audit.*` (was `gptrag.audit.*`), `schema_version` 2, and
  `service_name` `agent-app-orchestrator` (was `gpt-rag-orchestrator`). Field
  names and semantics are unchanged. The vendored v2 schemas are pinned at
  `884dfa2441d3313c8ec46a099f60ce86e7abb6cdf88bb5b5da720463edbf5e97` (logical)
  and `48416073768c0710b9a1f58640d4e822745f28b3a17b3712a2fe4cd9326c9c07`
  (Application Insights wire); the v1 files remain in `contracts/` as history.
  Internal logger names (`gptrag.audit`) are unchanged. Update dashboards and
  alerts that filter on `gptrag.audit.`.

## [v4.1.2] - 2026-10-01

### Added

- **Python quality-policy bootstrap.** Add development-only Ruff, mypy,
  Import Linter and Grimp checks, incremental typing, source-only import and
  broad-handler inventories, mutation fixtures and fail-closed PR aggregation.
  Handler approval and administrator activation remain explicitly pending.

### Fixed

- **User identity and conversation ownership are derived on the server.** The
  orchestrator no longer trusts a client-supplied principal or a development
  token; the caller's identity comes from the validated access token, and a
  conversation id is bound to its owner, including ids first created by an
  upload, so it cannot be read or reused by another user.
- **Deprecated source-authorization setting warning uses a fixed message.**
  The Foundry IQ deprecation warning no longer interpolates configuration
  names or values into logs.
- **Shared OBO exchange no longer creates an import cycle.** Search and
  Foundry IQ use one connector-owned exchange/cache while preserving Search's
  public wrappers, delegated scopes and strict/anonymous behavior.
- **Legacy retrieval plugin resolves the current model connector.** Replace
  the missing `AzureOpenAIClient` import with the existing `get_genai_client`
  and await its asynchronous embeddings operation.
- **Typed API metadata retains its existing wire shape.** Express legacy
  OpenAPI examples through Pydantic's typed metadata API and restore missing
  dashboard annotation imports without changing request or response fields.

## [v4.1.1] - 2026-09-03

### Fixed

- **The dashboard SPA build is repaired, which unblocks the orchestrator
  container image.** `Dockerfile` builds `frontend/` in its first stage, so a
  frontend build failure fails the whole image and therefore `azd deploy`.
  Three unrelated dependency bumps had been merged without any CI job that
  builds the frontend, and together they broke it:
  - `react` was raised to `19.x` while `react-dom` and `@types/react-dom`
    stayed on `18.x`, so `npm install` aborted with a fatal `ERESOLVE`
    peer conflict. Both are now `^19.2.8` / `^19.2.5`.
  - `@azure/msal-react` was pinned to `^5.7.0`, a version that has since been
    **unpublished from npm** and now returns `E404`. Because the Dockerfile
    uses `npm install` rather than `npm ci`, the lockfile did not protect the
    build. Pinned to `^3.0.29`, the newest major that accepts React 19 while
    keeping the `navigateToLoginRequestUrl` and `storeAuthStateInCookie`
    options that `src/lib/auth.ts` relies on; `@azure/msal-browser` moves to
    `^4.21.0` to satisfy its peer range.
  - `tailwindcss` was raised from `3.x` to `4.x`, which is a migration and
    not a bump: v4 moves the PostCSS plugin to `@tailwindcss/postcss` and
    replaces the `@tailwind` directives with `@import "tailwindcss"`. Neither
    `postcss.config.js` nor `src/index.css` was migrated, so `vite build`
    failed. Reverted to `^3.4.19`.
- `frontend/package-lock.json` is regenerated with `npm install --package-lock-only`
  so it keeps every platform-specific optional dependency. A lockfile written by
  a full `npm install` on Windows prunes them, leaving 2 of the 25
  `@rollup/rollup-*` entries and breaking `vite build` on Linux with
  `Cannot find module @rollup/rollup-linux-x64-gnu`.

### Added

- A `frontend build` job in `.github/workflows/pr_pipeline.yaml`. The
  frontend previously had no CI coverage at all, which is why all three
  regressions reached a tagged release.
## [v4.1.0] - 2026-09-03

### Fixed

- **Citation links returned to clients that render the answer directly — the
  Foundry Playground, `azd ai agent invoke`, and any custom caller — are now
  signed and therefore openable.** Foundry IQ returns an absolute blob URL for
  each retrieved document, and the orchestrator passed it into the model
  context verbatim. On a network-isolated deployment the target storage account
  has `publicNetworkAccess=Disabled` and `allowBlobPublicAccess=false`, so
  clicking the citation failed with HTTP 403 from outside the virtual network
  and `PublicAccessNotPermitted` (HTTP 409) from inside it — joining the network
  does not help, because the request carries no credential. Only the Chainlit
  front end resolved this, by signing the href in its own rendering layer, so
  every other surface was left broken. The link is now signed with a
  short-lived (1 hour) read-only user-delegation SAS before it enters the
  context, which makes the citation work on every surface. Signing degrades
  gracefully: a URL that is relative, already signed, or points at another host
  is returned untouched, and any failure to obtain a delegation key falls back
  to the unsigned URL rather than dropping the citation. The audit trail keeps
  recording the unsigned URL, so no credential reaches telemetry.
- **Foundry Playground and `azd ai agent invoke` requests no longer fail with
  HTTP 422 when the platform injects top-level Responses fields the adapter
  does not honor.** The hosted `/responses` adapter validated the request body
  against a strict allowlist, so *any* field a Foundry client sent that the
  adapter did not already know about produced a hard 422 and an unusable
  hosted agent. Because the platform injects a different field set per client,
  per surface, and per release, fixing them one at a time
  (`conversation`, then `session_id`, then `model`) could never converge. The
  policy is now inverted: unknown top-level fields are logged and dropped
  before `azure-ai-agentserver-responses` parses the body — so they are still
  never resolved through managed Conversations and cannot override the
  server-side agent definition — and only an explicit deny-list is rejected.
  `previous_response_id` remains rejected because discarding it could silently
  answer from truncated history.
- **Optional Responses parameters serialized as explicit `null` no longer
  trigger a spurious 422.** Foundry clients emit unset optionals as `null`, and
  the rejection check tested key presence rather than value, so
  `"previous_response_id": null` was rejected as if the caller had requested
  server-side history. A `null` now means absent.
- **Every hosted `/responses` rejection is now logged with its reason.** The
  422 detail was returned to the caller but never written to the logger, and
  the uvicorn access line that reaches the Foundry log stream carries only the
  status code. A refused Playground request was therefore invisible in
  Application Insights, which is what allowed the allowlist defect above to
  survive several diagnosis cycles.
- **Streaming turns no longer flood the log with
  `ERROR:opentelemetry.context:Failed to detach context`.** OpenTelemetry logs
  that record, with a full traceback, whenever a context token is detached from
  a different asyncio task than the one that attached it. Async generators have
  no context of their own — they run in the context of whichever task iterates
  them — so an instrumented generator that holds a span current across a
  `yield` always detaches in a foreign context once it is closed by the garbage
  collector or by the ASGI server. Every streamed response therefore emitted at
  least one bogus ERROR. `detach` swallows the underlying exception, so the
  record was never actionable; it only masked real errors. The noise
  originates in `agent_framework`'s own GenAI instrumentation, which cannot be
  fixed at our call sites, so the specific record is now filtered out on the
  `opentelemetry.context` logger in both the hosted and container observability
  setups. Every other `opentelemetry.context` record still propagates.

## [v4.0.2] - 2026-08-11

### Security

- **Hosted `/responses` now unconditionally forces `store: False`, closing an
  implicit Foundry managed-persistence path that violated the ADR-0004
  zero-RBAC, stateless hosted container contract.** The pinned
  `azure-ai-agentserver-responses==2.0.0b1` host
  (`ResponsesAgentServerHost.__init__`) auto-activates its own network-bound
  `FoundryStorageProvider` (built from `DefaultAzureCredential`) whenever no
  explicit `store` override is supplied and the process detects it is running
  as a hosted agent — true on every real deployment. Its orchestrator only
  invokes that provider when the *caller's* `store` is true, and the
  OpenAI/Foundry Responses contract defaults an omitted `store` to `true`.
  Live validation confirmed the resulting split: a caller that explicitly sent
  `store: false` succeeded, while the same request with `store` left unset or
  `true` deterministically failed with a platform `storage_error` once
  `NETWORK_ISOLATION=true` blocked the provider's path to the Foundry storage
  plane. The container's own internal model invocation
  (`single_agent_rag_strategy_v2.py`, `maf_agent_service_strategy.py`) already
  hardcoded `store: False` on the *separate*, inner Foundry agent/model call
  for the hosted runtime — that guarantee was never the gap. The gap was the
  *outer* wire-level `store` field on the incoming `POST /responses` request,
  which every caller could still set (or omit) freely, reaching the
  auto-activated storage provider before any GPT-RAG code ran. Every hosted
  `/responses` create call (`HostedResponsesAgentServerHost._create_endpoint`)
  now overrides `store` to `False` unconditionally — regardless of what a
  caller sends or omits, for both streaming and non-streaming (sync) requests
  — so the container never depends on Foundry managed persistence and never
  needs Conversations data-plane RBAC to answer a request. `background: true`
  requires `store: true` in the pinned SDK and is therefore no longer
  reachable; it now fails fast with an explicit HTTP 422 instead of the
  opaque SDK-level 400, since a queued/polled response that can never be
  retrieved is not a meaningful capability for a stateless container.
  `/invocations` and classic (non-hosted, Cosmos-backed) `/responses` behavior
  are **unaffected**. Added `test_responses_ignores_caller_store_and_fails_closed`
  (parametrized over explicit `store: true` and an omitted `store` field),
  `test_responses_stream_ignores_caller_store_and_fails_closed`, and
  `test_responses_rejects_background_mode`; replaced
  `test_responses_persists_retrieves_and_deletes_response`, which asserted the
  now-removed caller-controlled persistence round trip.

## [v4.0.1] - 2026-08-09

### Fixed

- **Hosted-agent entrypoint no longer crashes on import (circular import
  between `dependencies` and `connectors`).** `dependencies.py` eagerly
  imported `connectors.appconfig` at module level. `connectors/__init__.py`
  eagerly imports several connector submodules (`cosmosdb`, `search`,
  `aifoundry`, ...) that each import `dependencies` back, at their own module
  level, to call `get_config()`. Any entrypoint whose first touch of this
  package tree is `dependencies` — notably `src/api/hosted_entrypoint.py`,
  which is intentionally Cosmos-free and never imports `connectors` first —
  hit `ImportError: cannot import name 'get_config' from partially
  initialized module 'dependencies' (most likely due to a circular import)`
  and crashed before uvicorn could bind a port or serve `GET /readiness`.
  Deployed as a Microsoft Foundry hosted agent, this was observed as the
  platform returning HTTP 424 `session_not_ready` on invoke ("container
  started but `/readiness` didn't return HTTP 200 within the timeout"),
  because the container's Python process never reached that far.
  `AppConfigClient` construction is now deferred into `get_config()` instead
  of imported eagerly at module level, breaking the cycle with no change to
  `get_config()`'s public behavior or the `connectors` package's re-export
  surface. Added `tests/test_hosted_entrypoint_import.py`, which spawns a
  fresh interpreter (mirroring the container's `uvicorn ...:app` startup) to
  regression-test that `api.hosted_entrypoint` imports cleanly and that
  `dependencies` does not eagerly pull in `connectors` at module level.

## [v4.0.0] - 2026-08-07

Supersedes v3.12.0 as the SemVer-correct canonical release for the same
validated wire-compatibility change. v3.12.0 shipped a breaking change to the
hosted `/responses` contract under a MINOR version number; this repository's
own versioning rule (`MAJOR: breaking changes`) required a MAJOR bump for
that change. v3.12.0 remains published, tagged, and **immutable** — it is not
deleted, retagged, or edited — but it must not be treated as the supported
integration target and must not be pinned by the GPT-RAG umbrella manifest.
v4.0.0 is the supported release for this contract going forward and contains
the same reviewed code as v3.12.0; no additional feature or behavior change
is introduced by this release.

### Changed

- **Reclassified as MAJOR for SemVer correctness — no code change from
  v3.12.0.** This release republishes the identical, already-reviewed
  breaking change first shipped in v3.12.0 under the version number that
  correctly reflects its impact. The hosted `/responses` route continues to
  reject the `conversation` and `previous_response_id` top-level request
  fields with HTTP 422 and requires the authenticated caller to supply the
  complete, ordered turn history as the `input` array (or the existing plain
  string `input`) on every request. `/invocations`'s opaque `conversation_id`
  and classic (non-hosted, Cosmos-backed) `/responses` behavior remain
  **unaffected**. See the `v3.12.0` entry below for the full technical
  description of the underlying change — it is intentionally not repeated
  here to avoid ambiguity about which release introduced which behavior.

### Migration

- **Hosted `/responses` callers on any version before v4.0.0.** Remove
  `conversation` and `previous_response_id` from hosted `/responses`
  requests and instead accumulate and send the full ordered turn history as
  the `input` array on every call. Requests still using `conversation` or
  `previous_response_id` will fail with HTTP 422. No migration is required
  for `/invocations` callers or for classic (non-hosted) deployments.
- **Callers already on v3.12.0.** No client or server behavior changes; this
  is a version-number correction only. Re-point any pin from `v3.12.0` to
  `v4.0.0`. Do not pin `v3.12.0` in new integrations or in the GPT-RAG
  umbrella manifest.

### Validation

- Same reviewed code as v3.12.0, re-validated at release time on
  `release/4.0.0` (branched from `develop`, changed only `VERSION` and
  `CHANGELOG.md`): full pytest suite passed (718 passed), pyflakes-level
  Ruff checks (`--select F`) passed with zero findings on every file changed
  by this release, `compileall` passed for `src` and `tests`, `pip check`
  reported no broken requirements, and the Copilot engineering asset
  validator passed (2 agents, 5 skills, 8 scoped instructions). No frontend
  or `contracts/` files changed in this release, so no additional frontend or
  schema-hash validation was required.

## [v3.12.0] - 2026-08-07

Supersedes v3.11.0. This release contains a **wire compatibility change** for
the canonical `/responses` route on the hosted runtime — see the migration
note below before upgrading a caller that uses managed conversation identity.

### Security

- **Stateless, history-blind hosted container (PR #308).** The hosted Foundry
  agent container now performs zero managed Foundry Conversations data-plane
  operations: it never constructs a Conversations client and never
  creates, reads, appends to, or deletes a managed Conversation. Previously,
  the hosted `/invocations` and `/responses` paths resolved (and, for
  `single_agent_rag`, appended to) a Foundry Conversation using a
  caller-supplied or platform-chained id — letting a caller-selected
  identifier drive service-identity read/write. The canonical `/responses`
  route now explicitly rejects the `conversation` and `previous_response_id`
  top-level fields (422) instead of resolving them, and accepts a complete,
  bounded, ordered message-array `input` (in addition to the existing plain
  string `input`) so the authenticated caller supplies full history directly.
  Hosted-eligible strategies (`single_agent_rag`, `maf_agent_service`) replay
  that ordered history on a purely local, ephemeral model-call thread and
  explicitly disable server-side storage (`store: False`) instead of binding
  to or creating any service-managed thread/conversation. Classic
  Cosmos-backed (non-hosted) behavior, Toolbox call-context validation
  (ADR-0001), and Responses streaming/sync/store/background/lifecycle
  support are unchanged.

### Changed

- **Wire compatibility change — breaking for hosted callers using managed
  conversation identity.** The hosted `/responses` route no longer accepts
  the `conversation` or `previous_response_id` top-level request fields;
  both are now rejected with HTTP 422 instead of being resolved against a
  managed Foundry Conversation. Callers that previously relied on either
  field to carry history must switch
  to sending the complete, ordered conversation as the `input` array (a new
  accepted shape alongside the existing plain string `input`) on every
  request; the hosted runtime is now fully stateless and history-blind and
  will not reconstruct prior turns on the caller's behalf. There is no server
  fallback and no deprecation window for the old fields on the hosted route.
  `/invocations`'s opaque `conversation_id` and classic (non-hosted,
  Cosmos-backed) `/responses` behavior are **unaffected** — both continue to
  accept their existing request shapes unchanged.

### Migration

- **Hosted `/responses` callers only.** Before upgrading a hosted deployment
  to v3.12.0, update any client that sends `conversation` or
  `previous_response_id` to the hosted `/responses` endpoint to instead
  accumulate and send the full ordered turn history as the `input` array on
  every call. Requests still using `conversation` or `previous_response_id`
  will start failing with HTTP 422 immediately after the upgrade. No
  migration is required for `/invocations` callers or for classic
  (non-hosted) deployments.

### Validation

- **Reviewed release evidence.** At reviewed commit
  `a828253b85c6ed7a63f6085c1666f75a9ca2b7d8` (`develop`, merged PR #308), the
  full pytest suite passed (718 passed), pyflakes-level Ruff checks (`--select
  F`) passed with zero findings on every file changed by this release,
  `compileall` passed for `src` and `tests`, `pip check` reported no broken
  requirements, and the Copilot engineering asset validator passed (2 agents,
  5 skills, 8 scoped instructions). No frontend or `contracts/` files changed
  in this release, so no additional frontend or schema-hash validation was
  required.

## [v3.11.0] - 2026-08-06

### Fixed

- **Managed Conversation persistence for hosted Responses.** Completed
  user/assistant turns are now appended through the Foundry Conversations Items
  API because the pinned Agent Framework client does not support the Responses
  `store` option. Both agent and empty-index direct-LLM routes persist history,
  reject legacy thread IDs, initialize correctly on fresh workers, and
  reconcile ambiguous write failures without duplicating a committed turn.

- **Offline tokenizer initialization for network-isolated hosted agents.**
  Runtime images now pre-cache the `o200k_base` tokenizer during the image
  build, preventing the first hosted request from attempting public Blob
  Storage egress in private-only deployments.

- **Microsoft Foundry hosted-agent Responses route contract.** Added canonical
  Responses v2 hosting through `azure-ai-agentserver-responses`, so real
  Foundry requests to `POST /responses` no longer receive HTTP 404 or require
  the separate Invocations schema. The adapter accepts standard string `input`,
  streaming and synchronous execution, storage controls, managed
  `conversation` identity, `previous_response_id`, metadata, and the
  platform-injected `agent_reference`, and supplies protocol retrieval,
  input-items, cancellation, and deletion routes. List input and unsupported
  request fields are rejected instead of silently dropping semantics, and
  mutually exclusive `conversation` and `previous_response_id` inputs cannot
  contaminate each other's history. The
  existing `POST /invocations` route retains its distinct message-list schema
  and compatibility behavior. Toolbox-backed deployments validate the Foundry
  call context before any create or stored-response route can reach the
  platform storage provider. Invalid managed-conversation identifiers are
  rejected instead of creating a new thread, and generative-AI content capture
  is disabled by default unless explicitly enabled by the operator. Shutdown,
  client cancellation, and malformed call-id boundaries now stop upstream
  strategy work without leaking tasks or forwarding a different value than the
  one validated.

- **Microsoft Foundry hosted-agent readiness probe contract.** Added
  `GET /readiness` to `api.hosted_entrypoint` so the platform readiness probe no
  longer receives HTTP 404 after Uvicorn starts. The existing `GET /health`
  compatibility route remains available, and both endpoints return the same
  immutable image version and hosted-eligible strategy set.

### Added

- **Repository-local release skill with publication safety gates.**
  Added `.github/skills/release/SKILL.md` as the reusable Copilot workflow for
  release preparation, versioning, tagging, and release notes. The skill
  reconciles tags, GitHub Releases, `VERSION`, and `CHANGELOG.md`; enforces
  `release/X.Y.Z` branches from `develop` with pull requests to `main`; requires
  exact SemVer metadata and sanitized public notes; documents rollback and
  reconciliation; and blocks every tag, release, package, image, or deployment
  publication until a human explicitly approves the exact publication plan.
  Added focused asset tests for skill discovery, required release contracts,
  and continued absence of the abandoned release custom agent.

## [v3.10.0] - 2026-08-05

### Added

- **Foundry Toolbox OAuth identity passthrough for hosted `mcp` retrieval.**
  Added `util.foundry_platform` with the platform-injected header contract
  for Microsoft Foundry hosted-agent protocol 2.0: `x-agent-user-id` (container
  partition key, never forwarded) and `x-agent-foundry-call-id` (opaque
  per-request call identifier — the only supported identity passthrough
  correlation token). `POST /invocations` now captures and strictly validates
  `x-agent-foundry-call-id` (printable ASCII, no whitespace/control
  characters, 256-char max) whenever the resolved strategy is in the new
  `strategies.hosted_strategies.HOSTED_TOOLBOX_STRATEGIES` set (currently
  ``mcp``, the only strategy that calls Toolbox). Requests for a Toolbox
  strategy with a missing or malformed call id are rejected with HTTP 401
  before strategy construction — there is no service-identity or manual
  group-filter fallback. The validated call id flows through the
  runtime-neutral `TurnRequest.foundry_call_id` field into
  `BaseAgentStrategy.foundry_call_id` and is echoed as the sole outbound
  identity header by `connectors.mcp_client.open_mcp_tool` /
  `build_mcp_headers` on every Toolbox MCP HTTP call, so Toolbox can resolve
  the signed-in user and mint per-user credentials server-side. The
  container never reads, stores, or forwards the `Authorization` header
  (already stripped by the platform gateway) and never trusts caller/model
  identity fields or `x-client` group claims for document-security decisions,
  per
  [ADR-0001](https://github.com/Azure/GPT-RAG/blob/main/docs/adr/ADR-0001-hosted-agents.md).
  Non-Toolbox hosted strategies (`maf_lite`, `maf_agent_service`,
  `single_agent_rag`) are unaffected and continue to run with
  `foundry_call_id=None`. See
  [Azure/GPT-RAG#591](https://github.com/Azure/GPT-RAG/issues/591).

- **Focused Toolbox identity passthrough tests.** Added coverage proving:
  outbound header propagation to the MCP client (present, absent, and
  concurrent-call isolation); `/invocations` and `_hosted_stream` fail-closed
  behavior for the `mcp` strategy when the call id is missing or malformed,
  with the strategy factory never invoked; correct propagation of a valid
  call id into the constructed strategy; classic (non-Toolbox) hosted
  strategies remain unaffected; genuine concurrent-request isolation of call
  ids at the HTTP boundary (`httpx.AsyncClient` + `ASGITransport`); and that
  neither the call id nor the `Authorization` header value is ever emitted
  to application logs.

### Fixed

- **Hardened `x-agent-foundry-call-id` validation against trailing-newline
  bypass.** `util.foundry_platform.require_foundry_call_id` validated the
  call id with `re.match()` against an anchored `^[\x21-\x7e]+$` pattern.
  Without `re.MULTILINE`, Python's `$` matches immediately before a single
  trailing `\n`, so `.match()` would incorrectly accept a raw value like
  `"call-id\n"`. `extract_foundry_call_id`'s `.strip()` happened to remove
  such trailing whitespace before validation today, masking the defect
  end-to-end, but the validation itself must not depend on that as its only
  safeguard — a future direct call to `require_foundry_call_id`, or a
  refactor of extraction, could bypass the strip and forward an
  injection-bearing value to Toolbox. Switched to `.fullmatch()`, which
  anchors to the true end of the string regardless of trailing newlines.
  Added direct unit tests in `tests/test_foundry_platform.py` (bypassing the
  strip via monkeypatching) proving the regex rejects a trailing `\n`/`\r`
  even when extraction does not strip it, plus general direct-call
  coverage for `require_foundry_call_id`/`extract_foundry_call_id` (valid,
  missing, empty, too-long, and embedded-whitespace call ids).

- **`MissingFoundryCallContextError` could be silently downgraded to a
  generic SSE `internal_error` frame.** `_hosted_stream`'s defense-in-depth
  check (the fail-closed guard that exists in case a future/internal caller
  invokes it directly, bypassing the `/invocations` HTTP handler's 401
  precheck) raises `MissingFoundryCallContextError`, a `ValueError`
  subclass. `_sse_generator`'s inner generator caught it only via its broad
  `except Exception` clause, so it was indistinguishable from any other
  internal failure and reported to the client as `{"code":
  "internal_error"}`, losing its security-relevant classification. Added an
  explicit `except MissingFoundryCallContextError` clause in `_sse_generator`,
  ordered before the broad `except Exception`, that emits a distinctly coded
  `{"code": "missing_call_context"}` SSE error frame instead. The primary
  pre-`StreamingResponse` HTTP 401 guard in `/invocations` is unchanged and
  remains the only path that can return a 401 — this fix only prevents a
  hypothetical bypass of that guard from being misreported once the SSE
  stream (HTTP 200) is already open; it does not claim a post-header 401 is
  possible. Also unified the exception message text used at both raise sites
  into a single `util.foundry_platform.MISSING_CALL_CONTEXT_MESSAGE`
  constant, so the HTTP-level and defense-in-depth paths report identical,
  already-vetted-safe text. Added a direct-generator regression test that
  calls `_sse_generator` without going through `/invocations`, asserting the
  distinct error code, the canonical message, and that
  `AgentStrategyFactory.get_strategy` is never invoked.

## [v3.9.0] - 2026-07-30

### Added

- **Foundry Responses API adapter.** Added `api.responses_adapter` which
  translates every typed `TurnOutputEvent` (conversation identity, text delta,
  citation, tool-call started/completed/failed, error, cancellation) into the
  Azure AI Foundry Responses API SSE wire format.  `serialize_responses_events`
  expands each event into one or more SSE frames; `responses_terminal_events`
  emits the four closing frames (`response.output_text.done`,
  `response.content_part.done`, `response.output_item.done`,
  `response.completed`) once all deltas have been streamed.

- **Hosted agent entrypoint with managed Conversations.** Added
  `api.hosted_entrypoint` — a standalone FastAPI application for running the
  GPT-RAG orchestration core as an Azure AI Foundry hosted agent.  The
  `POST /invocations` endpoint accepts the Foundry invocation request format,
  projects the complete ordered prior request history into every admitted
  strategy contract (including `mcp`), validates supplied managed Conversation
  ids for Responses-backed strategies, and creates a real Foundry Conversation
  through the supported SDK when an id is absent.  Requests with messages after
  the current user ask are rejected rather than truncated.  The endpoint streams
  a Responses API SSE response without constructing or accessing a Cosmos
  client. Caller-provided identity is not trusted: Cosmos-backed profile memory
  remains disabled until the platform supplies authenticated Foundry identity.
  The `GET /health` endpoint returns the immutable image version and the set of
  admitted strategies for container readiness probes.

- **Explicit hosted-runtime strategy guard.** Added `strategies.hosted_strategies`
  with `HOSTED_ELIGIBLE_STRATEGIES` (``maf_lite``, ``maf_agent_service``,
  ``single_agent_rag``, ``mcp``) and `guard_hosted_strategy(key)` which raises
  `ValueError` immediately for any strategy outside that set.  The guard is
  called in `_hosted_stream` before strategy construction and surfaced as
  HTTP 422 from `POST /invocations`.  `multimodal` and `nl2sql` are excluded
  pending ADR approval.

- **Focused hosted adapter tests.** Added coverage for Responses API SSE
  serialization of every event kind and optional field; terminal closing
  frames; strategy guard pass/fail; genuine two-turn history and thread
  continuity; no-Cosmos construction and profile access; caller/conversation
  isolation; hosted stream errors and cancellation; and `GET /health` plus
  `POST /invocations` behavior including the `X-Response-ID` response header.
  See
  [Azure/GPT-RAG#598](https://github.com/Azure/GPT-RAG/issues/598).

- **Dependency-neutral turn contracts.** Added stdlib dataclasses for
  `TurnRequest` and typed output events covering conversation identity, streamed
  text, citations, tool activity, errors, and cancellation. The
  `orchestration` package now resolves `Orchestrator` lazily, so importing
  `orchestration.turn` does not load the web framework, Pydantic, or Azure
  runtime modules.

- **Typed orchestration output boundary with classic SSE compatibility.**
  Added `Orchestrator.from_turn_request()` and `stream_turn()`, with FastAPI
  serialization isolated in `api.turn_sse`. The existing `create()` and
  `stream_response()` APIs remain available, and the `/orchestrator` endpoint
  retains the classic conversation-prefix, text-chunk, error, and cancellation
  wire behavior. Eligible Agent Framework strategies translate explicit
  citation and tool-call signals into the typed event stream without adding
  bytes to the classic response. In Phase 1, `nl2sql` and `multimodal` remain
  text-only, and direct-LLM or other non-Agent-Framework paths do not emit typed
  tool or citation events. See
  [Azure/GPT-RAG#590](https://github.com/Azure/GPT-RAG/issues/590).

- **Boundary regression evidence.** Added clean-subprocess import isolation,
  typed-event and terminal-state coverage, byte-for-byte classic SSE parity,
  request-field propagation, and explicit strategy-selector forwarding tests.
  The selector tests intentionally do not claim to construct Azure-backed
  strategies.

## [v3.8.0] - 2026-07-20

### Added

- **Optional, privacy-conscious audit evidence.** Operators can now enable a v1
  activity trail over the existing OpenTelemetry and Application Insights
  pipeline for correlated request lifecycle, route, grounding-source, tool, and
  outcome events. Auditing remains disabled by default and metadata-only by
  default. Capturing prompts, responses, source excerpts, tool arguments, or tool
  results requires both explicit sensitive-content opt-in and a non-empty field
  allowlist. Events use bounded metadata, keyed pseudonyms, recursive
  prohibited-data filtering, fixed names and enums, and explicit cancellation
  and partial-output states. The orchestrator now returns a server-generated
  `X-Correlation-ID`; audit-only export does not enable normal application log
  export. See [Azure/GPT-RAG#571](https://github.com/Azure/GPT-RAG/issues/571)
  and [PR #277](https://github.com/Azure/gpt-rag-orchestrator/pull/277).

- **Shared v1 contract for component integration.** Added reusable logical and
  Application Insights wire schemas. The canonical ingestion taxonomy reserves
  exactly seven events: `ingestion.run.started`, `ingestion.run.completed`,
  `ingestion.run.failed`, `ingestion.run.cancelled`,
  `ingestion.document.indexed`, `ingestion.document.rejected`, and
  `ingestion.document.deleted`. These names are reserved for ingestion component
  integration and are not emitted by the orchestrator.

### Changed

- **Explicit evidence boundaries.** Audit events are best-effort operational
  evidence, not an immutable ledger or a legal or regulatory compliance claim.
  Fixed event budgets can omit detail while reporting omission counters;
  asynchronous Azure Monitor delivery has no application callback; failures
  before endpoint handling may not include `X-Correlation-ID`; and reconstructed
  Foundry IQ tool start times are approximate rather than proof of remote
  execution timing.

### Fixed

- **Bounded, observational audit instrumentation.** Restored audit-disabled
  retrieval and cache compatibility for empty Search and Foundry references,
  gated Foundry timing reconstruction behind the effective audit flag, rejected
  malformed or unbounded reconstructed durations without failing retrieval,
  bounded sanitizer traversal and per-request source/tool/event emission,
  reserved request terminal capacity with omission counts, made emission
  failures payload-free and constant-safe, and documented the logical-null to
  Azure Monitor all-zero root-parent translation.

### Validation

- **Reviewed release evidence.** At reviewed commit
  `844fe14757d07a2cdc828189105fbce831f3c11d`, all 533 tests passed, the Docker
  build passed, Ruff passed for every changed Python file, `compileall` passed,
  and `pip check` passed in the freshly built pinned image. The committed logical
  and Application Insights schema raw-byte SHA-256 hashes were independently
  reproduced.

## [v3.7.0] - 2026-07-19

### Added

- **Optional generic Foundry IQ MCP Server knowledge sources (Preview).**
  Operators can opt in to preprovisioned `mcpServer` sources with explicit tool
  allowlists, trusted-host validation, bounded planning/runtime/output, and
  request-scoped managed identity, OBO, or Key Vault credentials. The disabled
  default preserves the existing minimal-reasoning intents request and headers.

### Changed

- **MCP-aware Foundry IQ retrieval.** Enabled requests can plan across multiple
  generic MCP sources and existing knowledge source kinds, return activity and
  partial-result details, and normalize successful MCP results into the existing
  citation contract while enforcing configured reasoning, runtime, document, and
  tool-output bounds.

### Fixed

- **Canonical and safe MCP source handling.** MCP configuration now uses the
  Search-compatible `serverURL`, strictly discriminated object-shaped output
  parsing including `splitParameters`, and `reranked`/`always` inclusion modes;
  URL query strings are rejected and debug argument logging recursively redacts
  credentials and paired headers.
- **Disabled Foundry IQ compatibility.** Search OBO failures continue to log
  and fall back under the legacy disabled MCP path, while enabled MCP sources
  retain strict credential and source-failure propagation.

## [v3.6.0] - 2026-07-17

### Added

- **Streamable HTTP transport support.** The `mcp` strategy now accepts
  `streamable_http` and resolves the MCP endpoint to `/mcp`. Existing SSE
  deployments require no configuration changes because `sse` remains supported
  and remains the default.

### Changed

- **Microsoft Agent Framework MCP runtime and request-scoped headers.** The
  `mcp` strategy now runs on request-scoped Microsoft Agent Framework agents and
  MCP clients instead of Semantic Kernel. Native per-request `user-context` and
  optional `X-API-KEY` headers replace the process-wide `h11` patch, preventing
  caller context from leaking between concurrent requests while preserving the
  streaming and conversation persistence contracts.

- **MCP configuration validation.** The orchestrator now rejects unsupported
  transport values, non-positive timeouts, and endpoint suffixes that conflict
  with the selected transport. It appends `/sse` or `/mcp` once when the base
  endpoint does not already contain the matching suffix.

### Fixed

- **Deterministic Azure OpenAI client cleanup.** Request-scoped clients now
  close after successful, failed, or cancelled streams without masking the
  original request failure.

### Removed

- **Semantic Kernel runtime dependency.** The remaining plugin decorators now
  use MAF `ai_function`, so the orchestrator no longer installs or imports
  Semantic Kernel.

## [v3.5.1] - 2026-07-15

### Added

- **MSAL sign-in for the admin dashboard SPA.** The dashboard SPA served
  under `/dashboard/` now authenticates admins with Microsoft Entra ID
  using the Authorization Code + PKCE flow (`loginRedirect`). A new
  unauthenticated `GET /api/dashboard/auth-config` endpoint tells the SPA
  whether auth is enabled and, when it is, returns the `clientId`,
  `tenantId`, `authority`, and `apiScope` it needs to bootstrap MSAL. The
  SPA acquires an access token via `acquireTokenSilent` and falls back to
  `acquireTokenRedirect` on `InteractionRequiredAuthError`. MSAL tokens
  are kept in `sessionStorage` (not `localStorage`). The verified token's
  `Admin` app role is enforced by the API, and incomplete tenant/client
  configuration now fails closed instead of skipping audience validation.
  Operator setup is documented in the
  [Admin Dashboard Sign-in guide](https://azure.github.io/GPT-RAG/howto_dashboard_signin/).

### Changed

- **Dashboard bearer token source.** The dashboard SPA no longer reads
  `localStorage["dashboard.bearer"]` by default. Requests get their
  bearer from the MSAL token provider registered at bootstrap. An
  explicitly-passed `Authorization` header on a request still wins,
  which preserves the scripted-test workflow (`api.request(url, {
  headers: { Authorization: "Bearer <token>" } })`).

## [v3.5.0] - 2026-07-13

### Added

- **SharePoint remote (Copilot Retrieval API) knowledge source (preview, default off).**
  The Foundry IQ retrieve client can now include a `remoteSharePoint` knowledge
  source that live-queries SharePoint through the Microsoft Graph Copilot
  Retrieval API. Behavior is gated by `SHAREPOINT_REMOTE_ENABLED` and
  `SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME`; when both are set and an
  on-behalf-of user token is available, a `kind="remoteSharePoint"` entry is
  appended to `knowledgeSourceParams`. Per-user ACL is enforced natively by
  SharePoint via the forwarded token (same `x-ms-query-source-authorization`
  header used by Work IQ / Fabric ontology / Fabric Data Agent). MI fallback
  is never used for remote kinds.

- **OneLake indexed knowledge source (preview, default off).**
  The Foundry IQ retrieve client can now include an `indexedOneLake`
  knowledge source, letting Foundry IQ query a Microsoft Fabric OneLake
  workspace/lakehouse that has been pre-indexed by Foundry IQ. Behavior is
  gated by `ONELAKE_KS_ENABLED` and `ONELAKE_KNOWLEDGE_SOURCE_NAME` (plus
  workspace/lakehouse identifiers). Unlike the three remote kinds above,
  `indexedOneLake` is served by Foundry IQ's own index and does not require
  an OBO token at retrieve time.

- **SharePoint Indexed knowledge source (preview, default off).**
  The Foundry IQ retrieve client can now include an `indexedSharePoint`
  knowledge source, letting Foundry IQ query a SharePoint site indexed by
  Foundry IQ using app-only Microsoft Graph auth (Sites.Selected preferred).
  Behavior is gated by `SHAREPOINT_INDEXED_ENABLED` and
  `SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME`. Like `indexedOneLake`, this
  kind is served by Foundry IQ's own index and does not require an OBO
  token at retrieve time.

- **Web grounding (Grounding with Bing) knowledge source (preview, default off).**
  The Foundry IQ retrieve client can now include a `web` knowledge source
  that returns public web results scoped by allow/block domain lists, billed
  per Bing call. Behavior is gated by `WEB_GROUNDING_ENABLED` and
  `WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME`. Because the public web has no
  per-user ACL, `web` is NOT in `_REMOTE_KNOWLEDGE_SOURCE_KINDS` and does
  not require an OBO token. It does count toward the retrieve-time runtime
  ceiling to accommodate Bing latency.

### Fixed

- **`_normalize_references` dispatch specificity.**
  SharePoint remote dispatch now requires the Copilot Retrieval API's unique
  `resourceMetadata` marker rather than a bare `webUrl` or `extracts`, so
  SharePoint Indexed (`webUrl` + top-level `title`/`content`) and Work IQ
  (`attributions` + `extracts`) references route to the correct normalizer.

### Changed

## [v3.4.1] - 2026-07-13

Hotfix release published directly to `main`. Contents equivalent to v3.4.0
(Fabric Data Agent knowledge source, listed in detail under v3.4.0 below).
Back-merged into `develop` on 2026-07-13 via the v3.5.0 sync merge.

## [v3.4.0] - 2026-07-13

### Added

- **Fabric Data Agent (Microsoft Fabric) knowledge source support (opt-in, default off).**
  The Foundry IQ retrieve client can now include a `fabricDataAgent` knowledge
  source alongside existing `azureBlob` / `searchIndex` / `workIQ` /
  `fabricOntology` sources. A Fabric Data Agent acts as a virtual analyst that
  runs queries over Microsoft Fabric data and returns natural-language answers
  plus tabular grounding evidence. Behavior is gated by
  `FABRIC_DATA_AGENT_ENABLED` and `FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME`;
  when both are set and an on-behalf-of user token is available, a
  `kind="fabricDataAgent"` entry is appended to `knowledgeSourceParams`. ACL is
  enforced natively by Fabric via the forwarded per-user token (same
  `x-ms-query-source-authorization` header used by Work IQ / Fabric ontology),
  so no `filterAddOn` is emitted. Managed-identity fallback is never used for
  remote knowledge source kinds: when the OBO token is missing the Fabric Data
  Agent source is skipped (with a warning) and local sources still serve the
  request. Live end-to-end validation against a real Fabric Data Agent was
  deferred at ship time because no test agent was available; the code path is
  covered by unit tests and by an offline stub configuration.

### Fixed

### Changed

## [v3.3.0] - 2026-07-10

### Added

- **Fabric IQ (Microsoft Fabric ontology) knowledge source support (opt-in, default off).**
  The Foundry IQ retrieve client can now include a `fabricOntology` knowledge
  source alongside existing `azureBlob` / `searchIndex` / `workIQ` sources,
  giving grounded answers over a Microsoft Fabric ontology (semantic model,
  lakehouse, warehouse, or KQL database exposed through the ontology).
  Behavior is gated by `FABRIC_IQ_ENABLED` and
  `FABRIC_IQ_KNOWLEDGE_SOURCE_NAME`; when both are set and an on-behalf-of
  user token is available, a `kind="fabricOntology"` entry is appended to
  `knowledgeSourceParams`. ACL is enforced natively by Fabric via the
  forwarded per-user token (same `x-ms-query-source-authorization` header
  used by Work IQ), so no `filterAddOn` is emitted. Managed-identity
  fallback is never used for remote knowledge source kinds: when the OBO
  token is missing the Fabric IQ source is skipped (with a warning) and
  local sources still serve the request. See
  [Azure/GPT-RAG#543](https://github.com/Azure/GPT-RAG/issues/543).

### Fixed

### Changed

## [v3.2.0] - 2026-07-10

### Added

- **Work IQ (Microsoft 365) knowledge source support (opt-in, default off).**
  The Foundry IQ retrieve client can now include a Work IQ knowledge source
  alongside existing `azureBlob` / `searchIndex` sources, giving grounded
  answers over the caller's Outlook mail, Teams chats, SharePoint / OneDrive
  files, and other M365 signals. Behavior is gated by
  `WORK_IQ_ENABLED` and `WORK_IQ_KNOWLEDGE_SOURCE_NAME`; when both are set and
  an on-behalf-of user token is available, a `kind="workIQ"` entry is
  appended to `knowledgeSourceParams`. ACL is enforced natively by M365 via
  the forwarded user token, so no `filterAddOn` is emitted. Managed-identity
  fallback is never used for remote knowledge source kinds: when the OBO
  token is missing the Work IQ source is skipped (with a warning) and local
  sources still serve the request. See
  [Azure/GPT-RAG#543](https://github.com/Azure/GPT-RAG/issues/543) for the
  end-to-end enablement guide (gated preview, admin consent, service
  principal, and Work IQ knowledge source provisioning).

- **Foundry IQ `maxRuntimeInSeconds` plumbing.** Remote knowledge source kinds
  fan out to Microsoft 365 (and, later, Fabric) and can take 40 to 60 seconds
  end-to-end. The retrieve body now carries `maxRuntimeInSeconds` (default
  `120`, override via `FOUNDRY_IQ_MAX_RUNTIME_SECONDS`) whenever a remote
  kind is enabled. The header is not emitted for Pattern A / Pattern B, so the
  default retrieve body is byte-identical to prior releases.

- **Reference-normalization seam for remote knowledge sources.** The
  `_normalize_references` mapper now recognizes the Work IQ `sourceData`
  shape (`attributions[].seeMoreWebUrl` + `extracts[].text`) and returns the
  same `{title, link, content}` contract the rest of the orchestrator
  expects. The seam is generic so future Fabric IQ shapes can be added
  without touching call sites.

## [v3.1.1] - 2026-07-03

### Fixed

- **Foundry IQ conversation upload filter compatibility.** Changed the
  conversational upload sidecar to send the Foundry IQ accepted
  `filterAddOn` format `conversationId eq '<conversation-id>'` instead of the
  compound Pattern B security and shared-corpus filter. This restores chat
  retrieval for documents uploaded into a conversation while keeping the native
  Blob source unchanged and continuing to forward source authorization for
  Foundry IQ permission enforcement.

- **Configuration allowlist import.** Restored the missing
  `SEARCH_RETRIEVAL_ENABLED` setting wrapper in the dashboard configuration
  allowlist so the settings module imports cleanly and the full test suite can
  run against the patch.

## [v3.1.0] - 2026-07-02

### Added

- **Foundry IQ conversational file upload retrieval (Pattern A).** When Foundry
  IQ runs with the native `azureBlob` knowledge source (Pattern A), the
  orchestrator can now also retrieve documents a user uploaded through the UI
  during the same conversation. This lets users upload a file mid-conversation
  and immediately ask questions about it without leaving Foundry IQ retrieval.
  The behavior is opt-in and off by default, gated by the new
  `FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED` flag and the
  `FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME` setting. When both are set, the
  orchestrator adds a second, sidecar knowledge source (over the existing
  conversation search index) alongside the native Blob source, so a single
  Foundry IQ retrieval call spans both the curated corpus and the user's uploaded
  files.

### Security

- **Uploaded documents stay scoped to the conversation and respect access
  control.** The conversational sidecar knowledge source always carries a
  server-computed `filterAddOn` that combines the request's security filter
  (RBAC/ACL trimming, applied through the on-behalf-of identity) with the current
  `conversationId`. Uploaded files are therefore only retrievable inside the
  conversation that produced them and only for users allowed to see them. This
  filter is mandatory: the orchestrator refuses to attach the sidecar source
  without it, and the native Blob source keeps its existing behavior of never
  receiving a conversation filter. The sidecar is only ever added in Pattern A;
  Pattern B (search-index primary) is left unchanged to avoid double-counting the
  same index.

### Fixed

- **NL2SQL read-only enforcement:** Hardened NL2SQL SQL execution so only a
  single read-only `SELECT` query is accepted before execution. Operators should
  also ensure every configured SQL Server, Azure SQL, or Fabric SQL datasource
  uses a least-privilege read-only principal with access only to approved
  schemas, tables, or views.

## [v3.0.4] - 2026-06-29

## [v3.0.3] - 2026-06-28

### Changed

- **MCR-hosted base images replace Docker Hub for Zero Trust ACR builds.** The
  Dockerfile now uses MCR-hosted Node.js and Python dev container images for the
  frontend build and orchestrator runtime stages, so local Docker builds and
  remote ACR builds no longer pull base images from Docker Hub.

- **Remote ACR build retry is bounded and visible.** Remote `az acr build`
  deployment now has bounded retries so transient registry or base-image
  availability failures show attempt counts before ending with actionable
  troubleshooting guidance.

## [v3.0.2] - 2026-06-26

### Fixed

- **Foundry IQ knowledge base retrieve: forward Search-audience token as
  `x-ms-query-source-authorization` when no OBO token is available.** When a
  knowledge base has `permissionFilterOption=enabled` and its knowledge source
  uses `ingestionPermissionOptions=["rbacScope"]`, the retrieve action requires
  a Search-audience token in `x-ms-query-source-authorization` to evaluate the
  per-document RBAC filter - without it the service responds 502 ("Failed to
  query search index"). `FoundryIQClient.retrieve` now falls back to the
  service managed-identity token (acquired for `https://search.azure.com/.default`)
  when no per-user OBO token is present, so anonymous chat against an
  RBAC-filtered knowledge base no longer fails. The behavior is gated by the
  new `FOUNDRY_IQ_FORWARD_SOURCE_AUTH` flag (default `true`) so operators can
  disable it. This closes the orchestrator-side gap behind Azure/GPT-RAG#508
  ("Orchestrator API does not work when RBAC is enabled") for the Foundry IQ
  retrieval backend.

- **Foundry IQ knowledge base retrieve: parse `azureBlob` reference shape
  correctly.** `FoundryIQClient._normalize_references` previously read
  `sourceData.content`, `sourceData.filepath`, and `sourceData.url`. The
  Foundry IQ `azureBlob` knowledge source returns `sourceData.snippet` and
  `sourceData.blob_url` instead (true for both `contentExtractionMode=minimal`
  and `standard`/OCR), with no `title` field, so every reference was being
  dropped as empty. The parser now accepts both shapes with explicit priority
  (`snippet` → `content` → `text`; `blob_url` → `url` → `filepath` → `path` →
  reference-level `blobUrl`) and derives a human-readable title from the
  blob/file name when `sourceData.title` is absent. This restores grounding
  for scanned PDFs ingested via the Content Understanding skill and keeps the
  Pattern B `searchIndex` path working unchanged.

## [v3.0.1] - 2026-06-26

### Added

- **Foundry IQ as a selectable retrieval backend (`RETRIEVAL_BACKEND`).** A new
  non-breaking seam lets operators choose where grounding documents come from:
  `ai_search` (default, current Azure AI Search RAG index) or `foundry_iq`
  (Foundry IQ knowledge base retrieve action). The selector is resolved once at
  startup and read by `search_knowledge_base` and the MAF strategy
  `_create_search_provider` seams. A new `FoundryIQClient` targets the knowledge
  base retrieve endpoint with a pinned, configurable
  `FOUNDRY_IQ_API_VERSION=2026-05-01-preview` and forwards the per-user OBO token
  in `x-ms-query-source-authorization`. A new `FoundryIQContextProvider` emits
  context that is byte-identical to the AI Search path via a shared
  context-shaping helper, so citations are unchanged. New settings:
  `RETRIEVAL_BACKEND`, `KNOWLEDGE_BASE_NAME`, `KNOWLEDGE_BASE_ENDPOINT`,
  `KNOWLEDGE_BASE_CONNECTION_ID`, `FOUNDRY_IQ_API_VERSION`. The default stays
  `ai_search`, so this changes no runtime behavior until an operator opts in.
  Multimodal retrieval stays on `ai_search` for image grounding; the `foundry_iq`
  selection routes it to text-only retrieval (Pattern A image parity deferred).
  (Azure/GPT-RAG#526)

- **Foundry IQ Pattern B query-time filtering.** When an existing GPT-RAG Azure
  AI Search index is registered as a Foundry IQ `searchIndex` knowledge source,
  the orchestrator can now send a `filterAddOn` OData filter using the GPT-RAG
  security field (`metadata_security_id` by default) and conversation scope.
  This keeps Pattern B security-field trimming separate from the native
  `x-ms-query-source-authorization` OBO path used by Foundry IQ sources that
  ingest permissions. New settings: `FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME`,
  `FOUNDRY_IQ_FILTER_ADD_ON_ENABLED`, `FOUNDRY_IQ_SECURITY_FIELD_NAME`, and
  `FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS`. (Azure/GPT-RAG#526)

### Changed

- Reconciled the Azure AI Search query api-version setting to the single
  canonical `SEARCH_API_VERSION` key (the orchestrator previously read an
  undocumented `AZURE_SEARCH_API_VERSION` in one place).

- **Dev CI/CD pipeline now soft-fails Azure environment connectivity failures.**
  The `develop` deployment workflow now reports a GitHub Actions warning and
  skips the `dev` deployment when Azure login, subscription/RBAC access, DNS, or
  App Configuration connectivity is unavailable. Build/test failures and
  non-Azure deployment failures still fail the job. The Azure login step is
  handled by the workflow shell so expected dev credential failures do not emit
  extra red `azure/login` annotations. This is a temporary dev-only workaround
  until the `dev` environment credentials and `APP_CONFIG_ENDPOINT` are
  repaired.

- **Foundry IQ defaults to native Blob or ADLS Knowledge Sources.** The
  orchestrator now reads `FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND` or
  `FOUNDRY_IQ_PATTERN`, defaults to `azureBlob`, and sends `searchIndex` only
  when Pattern B is explicitly configured. `filterAddOn` is rejected for native
  sources because it only applies to `searchIndex` Knowledge Sources.

### Removed

- Removed the dead `ENABLE_AGENTIC_RETRIEVAL` flag from `.env.sample` and the
  stale `enable_agentic_retrieval` Jinja example in the base strategy docstring.
  The orchestrator never read this flag. (Azure/GPT-RAG#526)

- Standalone `evaluations/` harness (scripts and pinned `requirements.txt`).
  Evaluation now runs through the AgentOps Accelerator against the live
  orchestrator endpoint; see the AgentOps HTTP agent tutorial
  (https://azure.github.io/agentops/tutorial-http-agent/) for the full workflow
  and the GPT-RAG retrieval optimization how-to
  (https://azure.github.io/GPT-RAG/howto_retrieval_optimization/) for retrieval
  tuning. `evaluations/README.md` now redirects to both. Also dropped the
  `/evaluations` Dependabot pip entry that tracked the deleted requirements file.

## [v2.8.13] - 2026-06-19

### User and operator impact

Hotfix on top of v2.8.12. The Conversations detail dialog was still showing
`(empty)` cards for every user and assistant turn because the backend
reconstruction in v2.8.12 emits each message body under the `text` field but
the dialog component only read `content`. The dialog now reads `content` and
falls back to `text` so persisted prompts and answers render correctly.

### Fixed

- Conversations detail: dialog renders message bodies returned under either
  `content` or `text`, removing the `(empty)` cards seen after upgrading to
  v2.8.12 (`frontend/src/components/ConversationDetailDialog.tsx`,
  `frontend/src/lib/api.ts`).

## [v2.8.12] - 2026-06-19

### User and operator impact

Patch release that fixes five operator-reported dashboard bugs against the
v2.8.11 build ([Azure/gpt-rag-orchestrator#247](https://github.com/Azure/gpt-rag-orchestrator/issues/247)).
The Overview Custom range picker no longer accepts future dates and the `to`
day is now included in the chart, info tooltips render against a fully opaque
background so the text is readable on top of cards, the Conversations dialog
shows the prompts that were actually persisted instead of empty `(empty)`
cards plus a friendly note explaining where the assistant transcript lives,
and the Configuration tab's footer buttons are relabeled with sentence-case
helper tooltips. No configuration changes are required.

### Fixed

- **Overview tab - Custom range accepted future dates ([#247](https://github.com/Azure/gpt-rag-orchestrator/issues/247)).** Both `From` and `To` `<input type="date">` controls now cap at today's UTC date via `max={todayIso()}`, the picker rejects any manually-typed future value with `Dates can't be in the future.`, the persisted-range loader clamps any future value left behind by an earlier session in `localStorage`, and the backend `/api/dashboard/overview` endpoint returns `400 'from' and 'to' cannot be in the future` so a hand-crafted query is rejected too.
- **Overview tab - `to` excluded the last day of the range ([#247](https://github.com/Azure/gpt-rag-orchestrator/issues/247)).** `_parse_iso_date` parsed `to=2026-06-19` as midnight UTC, which silently dropped every conversation created during 2026-06-19 because the Cosmos predicate is `_ts <= to_ts`. The helper now takes an `end_of_day` flag and parses `to` as `23:59:59` UTC of that day, so the range is inclusive of both bounds. `window_days` math still produces the correct inclusive span. New regression test `test_overview_to_is_end_of_day_inclusive` pins the behavior.
- **Overview tab - info tooltip rendered on a translucent background ([#247](https://github.com/Azure/gpt-rag-orchestrator/issues/247)).** The popover used `bg-popover`, which is a translucent surface token, so when the tooltip overlapped another KPI card the underlying card text bled through and made the tooltip prose unreadable. Switched the popover body to the fully opaque `bg-card` token with `border-border` and `shadow-md`, and kept the `normal-case tracking-normal` reset from v2.8.11.
- **Conversations tab - dialog showed `user (empty)` and `assistant (empty)` for every conversation ([#247](https://github.com/Azure/gpt-rag-orchestrator/issues/247)).** The orchestrator stores user prompts under `questions[]` and persists assistant replies on the Azure AI Foundry agent thread referenced by `thread_id`, not in a `messages[]` array on the Cosmos document. The dashboard read `conversation_doc.get("messages", [])` and found nothing for every doc. The detail endpoint now projects `questions[]` into `{role: "user", content, question_id}` entries, passes `thread_id` and `feedback` through, and the dialog renders a friendly note explaining where the full transcript lives plus a placeholder card after each user turn ("Assistant response stored on Foundry thread."). Backend regression `test_conversation_detail_reconstructs_from_questions` covers the projection.
- **Configuration tab - `Reload settings cache` button label was technical ([#247](https://github.com/Azure/gpt-rag-orchestrator/issues/247)).** Renamed the button to `Refresh from App Configuration` and added a sentence-case info tooltip explaining that it re-reads values from Azure App Configuration and does not save anything. The sibling `Apply changes` button also gets an info tooltip clarifying that it refreshes the running app's settings cache so saved changes take effect without restarting the container. Both tooltips use the now-opaque `InfoTooltip` from the same release.

## [v2.8.11] - 2026-06-19

### User and operator impact

Patch release that fixes three operator-reported Overview tab bugs against the
v2.8.10 dashboard ([Azure/gpt-rag-orchestrator#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).
Info tooltips on metric cards now render in sentence case, the Active users
metric no longer treats every anonymous conversation as a distinct user, and
the Custom range chip now reveals two clearly visible From/To date inputs
without zeroing the chart. No configuration changes are required.

### Fixed

- **Overview tab - info tooltip bodies rendered in ALL CAPS ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** The `StatCard` label span uses `uppercase tracking-wide` for the metric name, and `text-transform: uppercase` is inherited by descendant elements, so the popover body inside `InfoTooltip` rendered the tooltip prose in capitals. The popover now resets to `normal-case tracking-normal` regardless of the surrounding label styling.
- **Overview tab - Active users metric inflated for anonymous traffic ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** Anonymous conversations use `anonymous-<conversation_id>` as both their Cosmos partition key and `principal_id` to avoid hot-partitioning. The Overview aggregation counted distinct `principal_id` values verbatim, so 57 anonymous conversations reported 57 active users. `fetch_overview` now collapses any `principal_id` that starts with `anonymous-` (or equals `anonymous`) into a single bucket before counting; authenticated users continue to be counted by their Entra object id. The Active users tooltip wording is updated to match. Regression test `test_fetch_overview_buckets_anonymous_principal_ids` seeds three anonymous docs plus one authenticated GUID and asserts `active_users == 2`.
- **Overview tab - Custom range chip hid the date inputs and zeroed the chart ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** Selecting `Custom range` already populated a default last-7-days range and triggered a refetch, but the From/To inputs sat in a second flex row inside a tight `justify-between` header layout and were easy to miss, and every range change unmounted the chart in favor of a full-page spinner so the chart visibly went to zero. The picker now renders the inputs on a dedicated full-width row directly under the chips, with visible `From` / `To` labels, a short hint about the 365-day cap, and an inline error for invalid ranges. Refreshes after the first successful load keep the existing chart and KPIs mounted and surface a small `Refreshing...` indicator instead.

### Operational

- **Smaller, faster ACR builds (`.dockerignore`).** Added `frontend/node_modules/` and `frontend/dist/` to `.dockerignore`. The Docker frontend stage runs `npm ci` and `npm run build` from a clean copy, so shipping a local `node_modules` or stale `dist` to the ACR build context just slowed the upload and risked platform/arch mismatches. Identified during the v2.8.10 sandbox deploy.

## [v2.8.10] - 2026-06-19

### User and operator impact

Patch release that fixes four operator-reported dashboard bugs against the
v2.8.9 dashboard ([Azure/gpt-rag-orchestrator#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).
Conversation date columns now render correctly, the `REASONING_EFFORT`
dropdown can be saved, the Overview tab gains a time-range picker, and every
Overview metric gets the same accessible info tooltip already used on the
Configuration tab. No configuration changes are required.

### Fixed

- **Conversations tab - Created and Last updated columns showed `-` for every row ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** `ConversationListItem`, `ConversationMetadata`, `ConversationDetail`, `ConversationUpdateResponse`, `DashboardConversationSummary`, and `DashboardConversationDetail` declared their date fields with `alias="_ts"` / `alias="lastUpdated"`, which made FastAPI serialize the JSON under the Cosmos field names instead of the Python field names. The frontend then read `c.created_at` / `c.last_updated` and got `undefined`. Switched to `validation_alias=` so the alias is accepted on input only and the response JSON uses `created_at` and `last_updated`. A regression test asserts the serialized payload.
- **Configuration tab - saving `REASONING_EFFORT=Low` failed with `Validation failed for REASONING_EFFORT` ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** The backend validator already accepted the lowercase `minimal/low/medium/high` values that Azure OpenAI's `reasoning_effort` parameter expects. Added a round-trip regression test for all four canonical values plus an uppercase-rejection test so this stays detectable.

### Added

- **Overview tab - time-range picker ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** Header now has a picker with presets Today / Last 7 days / Last 30 days (default) / Last 90 days / Custom range. The selection drives the time-series chart, the Engagement panel, the Active users card subtitle (`Distinct, <label>`), and the new `in_window_count` total. The four KPI cards keep their fixed semantic windows. Selection persists in `localStorage` under `gpt-rag-orchestrator.overview.range`. The backend endpoint accepts `from` and `to` ISO query params, validates `from <= to`, caps the range at 365 days, returns 400 on invalid input, and keys the in-process cache on `(from, to)`.
- **Overview tab - accessible info tooltips ([#241](https://github.com/Azure/gpt-rag-orchestrator/issues/241)).** Reuses the existing `InfoTooltip` primitive from the Configuration tab next to each of the four KPI cards, the `Conversations over time` chart title, the `Engagement` panel title, and each of the three engagement rows. Wording lives in `frontend/src/components/overview/copy.ts`.

## [v2.8.9] - 2026-06-18

### User and operator impact

This release introduces a built-in **operator dashboard** for the orchestrator at `/dashboard`. The dashboard is opt-in (off by default) and gated by the `Admin` Entra app role when authentication is enabled, so existing deployments are unchanged unless you turn it on. Once enabled, operators get three tabs - Overview, Conversations, and Configuration - and can tune common runtime settings without leaving the app or restarting the container.

### Added

- **Operator dashboard at `/dashboard` ([Azure/GPT-RAG#511](https://github.com/Azure/GPT-RAG/issues/511)):** Turn it on by setting `ENABLE_DASHBOARD=true` in App Configuration. When off (the default), neither the page nor its API routes are registered.
  - **Overview tab.** Today / 7-day / 30-day conversation counts, a conversations-over-time line chart, average user turns per conversation, and active user count. The overview query is cached in-process for 60 seconds so refreshes stay cheap.
  - **Conversations tab.** Paginated, newest-first list of conversations with a detail dialog that renders the full message history. Data is read cross-partition from the existing conversation Cosmos container - no new storage.
  - **Access control.** When authentication is enabled, every `/api/dashboard/*` route (except `/api/dashboard/version`) requires the caller's bearer token to include the `Admin` app role from Entra. The `/dashboard` HTML page itself is open so the SPA can load and render a clear access-denied state on 403.
  - **Frontend.** Vite + React + TypeScript + Tailwind SPA, same conventions as `gpt-rag-ingestion`. Recharts is added for the time-series chart. The production bundle is built into `src/static` by a new `node:20-slim` stage in the Dockerfile.

- **Configuration tab in the dashboard ([Azure/GPT-RAG#512](https://github.com/Azure/GPT-RAG/issues/512)):** A new third tab lets an admin view and edit a curated set of runtime settings without going to the Azure portal.
  - **What you can edit.** Five sections matching the way operators actually think about the orchestrator: *Agent and generation* (`AGENT_STRATEGY`, `REASONING_EFFORT`, `CHAT_TEMPERATURE`, `CHAT_TOP_P`, `MAX_COMPLETION_TOKENS`), *Conversation history*, *Retrieval and search*, *Reliability*, and *Multimodal*. Each field has the right input type - dropdowns for enums, a range slider plus number input for `CHAT_TEMPERATURE` and `CHAT_TOP_P`, number boxes for counters, and toggles for booleans.
  - **Accessible tooltips.** Every field has an info popover that is keyboard- and screen-reader-reachable (not hover-only). The text comes from a single backend metadata module (`src/api/config_settings.py`), so docs and UI never drift.
  - **Two safety nets.** An explicit allow-list of editable keys, plus a defense-in-depth denylist that rejects any write to a key whose name matches sensitive suffixes (`_APIKEY`, `_SECRET`, `_PASSWORD`, `_CONNECTION_STRING`, `_TOKEN`, ...) or to specific keys like `KEY_VAULT_URI` and `MCP_APP_APIKEY` - even if a future refactor accidentally widens the allow-list.
  - **Honest action buttons.** *Reload settings cache* refreshes the in-process App Configuration cache (`POST /api/dashboard/config/refresh`). *Apply changes* (`POST /api/dashboard/config/apply`) is a soft restart that refreshes the cache and returns a clear status string. We deliberately did not ship a button called "Restart" that does not actually restart the container - a hard revision restart would require `azure-mgmt-appcontainers` and extra RBAC and remains a follow-up.
  - **Helpful errors.** Validation errors come back as `422` with a per-key error list and surface inline in the UI without breaking the page. Write errors come back as `500` with the same shape. Valid fields are not blocked by invalid ones.
  - **Where values are written.** Accepted values are written to App Configuration under the `gpt-rag-orchestrator` label so it is easy to filter who wrote what in the Azure portal.

### Fixed

- **Restored `frontend/src/lib/api.ts` (fix for [#236](https://github.com/Azure/gpt-rag-orchestrator/pull/236)):** PR #236 merged the dashboard frontend without `lib/api.ts`, so a fresh `npm run build` in `frontend/` could not resolve the helpers imported by `App.tsx`, `OverviewTab.tsx`, `ConversationsTab.tsx`, and `ConversationDetailDialog.tsx`. Root cause was a `.gitignore` rule (`lib/`) silently swallowing the directory. The module is restored with the original typed wrapper around `fetch` (`ApiError`, `fetchVersion`, `fetchOverview`, `fetchConversations`, `fetchConversationDetail`, `formatUtc`) and extended with the helpers used by the new Configuration tab. `.gitignore` is updated with `!frontend/src/lib/**` so this cannot recur.

### Validation

- Full pytest suite: 220 passed.
- Frontend: `npm run lint` clean, `npm run build` clean (no missing-import errors).
- Endpoint surface manually exercised: `GET /api/dashboard/overview`, `GET /api/dashboard/conversations`, `GET /api/dashboard/config`, `PUT /api/dashboard/config` (allow-list accept, denylist reject, range reject, enum reject), `POST /api/dashboard/config/refresh`, `POST /api/dashboard/config/apply` - `require_admin` honored in each case when auth is on.

## [v2.8.8] - 2026-06-18

### Added
- **Optional `custom_metadata` in the LLM context block (refs [Azure/GPT-RAG#506](https://github.com/Azure/GPT-RAG/issues/506)):** Retrieval can now surface the already-indexed `custom_metadata` field (from [Azure/GPT-RAG#487](https://github.com/Azure/GPT-RAG/issues/487)) into each document's context, behind a default-off flag. When `SEARCH_INCLUDE_METADATA_IN_CONTEXT` is `true`, the connector path (`search_knowledge_base`) and both context providers (`SearchContextProvider`, `MultimodalSearchContextProvider`) select the field and prepend a compact, deterministic `[Document metadata]` block (sorted `key: value` lines) before the document content. `SEARCH_METADATA_MAX_CHARS` (default `500`) caps the block size and `SEARCH_METADATA_ALLOWED_KEYS` (CSV, default empty = all keys) restricts which keys are shown. The change is additive and orchestrator-only - no ingestion, embedding, or vector changes. When the flag is off, `custom_metadata` is not added to the select and retrieval behavior is byte-for-byte unchanged; this is required because pre-#487 indexes lack the field and selecting a missing field makes Azure AI Search reject the whole query.

## [v2.8.7] - 2026-06-18

### Changed
- **Standardized logging for swallowed retrieval and AI Search auth failures (refs [Azure/GPT-RAG#508](https://github.com/Azure/GPT-RAG/issues/508)):** The retrieval connector (`search_knowledge_base`, OBO token helpers, trimming fallback), the text retrieval provider (`SearchContextProvider`), and the multimodal retrieval provider (`MultimodalSearchContextProvider`) now emit structured log markers at every point where a retrieval or AI Search authentication failure used to be swallowed silently. AI Search 401/403 responses log `[Retrieval][AUTH_FAILURE]` at `ERROR`; other retrieval failures log `[Retrieval][ERROR]` at `WARNING`. Each record carries structured `extra` fields (`retrieval_status`, `retrieval_index`, `retrieval_credential_type`) so operators can pinpoint the failing call, index, and credential type (managed identity vs OBO). Behavior is unchanged: `search_knowledge_base` still returns `{"results": [], "error": "search_failed"}`, `SearchContextProvider.invoking` still returns an empty `Context()`, and OBO helpers still return `None` on failure. Two `caplog` tests lock the marker contract.

## [v2.8.6] - 2026-06-17

### Fixed
- **`single_agent_rag` follow-up turns with tool calls now resume from a stable Foundry conversation object (issue [Azure/GPT-RAG#505](https://github.com/Azure/GPT-RAG/issues/505)):** The strategy now creates one server-side `conv_` conversation per chat and reuses it across turns, preventing follow-up tool outputs from being chained to the wrong prior `resp_` id and failing with `400 No tool call found for function call output`.
- **`single_agent_rag` now reuses the configured Foundry prompt agent:** `AGENT_ID` is honored as the persistent agent name to reuse, so configuration changes such as `REASONING_EFFORT` no longer create a second fingerprinted Foundry agent. When `AGENT_ID` is empty, the strategy falls back to the stable default `gptrag-single-agent-rag` name.
- **Reasoning model response budget defaults:** `single_agent_rag` now defaults `MAX_COMPLETION_TOKENS` to `8000` and `REASONING_EFFORT` to `low`, leaving enough output budget for reasoning models such as `gpt-5-nano` and avoiding empty responses with `finish_reason=length` / `max_tokens`.
- **Global knowledge chunks with null conversation ids are now retrievable:** The shared AI Search filter now matches both `conversationId eq 'NaN'` and `conversationId eq null`, so globally ingested documents remain visible regardless of which no-conversation sentinel was used.
- **Direct LLM empty-index path now preserves valid chat history:** Persisted messages are normalized from `text` to `content`, empty/unsupported entries are skipped, and the no-context prompt stays strictly grounded when a configured knowledge base has no indexed content.

### Changed
- **Copilot repository instructions filename:** Renamed `.github/copilot_instructions.md` to `.github/copilot-instructions.md` so Copilot loads the repository instructions from the expected filename.

## [v2.8.5] - 2026-06-15

### Reverted
- **`semantic-kernel` bump to 1.43.0 ([#214](https://github.com/Azure/gpt-rag-orchestrator/pull/214))** reverted back to `1.34.0`. The bumped version requires `azure-ai-projects~=1.0.0b12`, which conflicts with the pinned `azure-ai-projects==2.0.0b3` and breaks `pip install -r requirements.txt` in the runtime image. The bump will be re-evaluated together with the `azure-ai-projects` upgrade path.

## [v2.8.4] - 2026-06-15

### Changed
- **Dependency refresh:** Absorbed Dependabot bumps merged to `develop`:
  - `uvicorn` 0.34.1 → 0.49.0 in `/evaluations` ([#205](https://github.com/Azure/gpt-rag-orchestrator/pull/205))
  - `httpx` requirement updated in `/evaluations` ([#206](https://github.com/Azure/gpt-rag-orchestrator/pull/206))
  - `openai` 2.9.0 → 2.41.1 in `/evaluations` ([#207](https://github.com/Azure/gpt-rag-orchestrator/pull/207))
  - `azure-monitor-opentelemetry-exporter` ([#210](https://github.com/Azure/gpt-rag-orchestrator/pull/210))
  - `sqlparse` 0.5.4 → 0.5.5 ([#212](https://github.com/Azure/gpt-rag-orchestrator/pull/212))
  - `tiktoken` 0.8.0 → 0.13.0 ([#213](https://github.com/Azure/gpt-rag-orchestrator/pull/213))
  - `semantic-kernel` 1.34.0 → 1.43.0 ([#214](https://github.com/Azure/gpt-rag-orchestrator/pull/214))

## [v2.8.3] - 2026-06-14

### Changed
- **Warn when `$env:APP_CONFIG_ENDPOINT` diverges from the azd environment during component deploy (issue [Azure/GPT-RAG#491](https://github.com/Azure/GPT-RAG/issues/491)):** `scripts/deploy.ps1` and `scripts/deploy.sh` now read both the shell `APP_CONFIG_ENDPOINT` and the azd env value and, when both are present and disagree (trimmed, case-insensitive), print a yellow warning that shows both values, states which one is being used (the shell value still wins, preserving existing precedence for jumpbox and CI flows), and tells the operator how to clear the shell override (`Remove-Item env:APP_CONFIG_ENDPOINT` in PowerShell, `unset APP_CONFIG_ENDPOINT` in bash). When only one source is set, the previous behavior is unchanged.

## [v2.8.2] - 2026-06-10

### Fixed
- **Foundry Agent Service v2 create-once regression (issue #484):** Restored the `single_agent_rag` and `maf_agent_service` migration to declarative/versioned Foundry prompt agents (`AIProjectClient.agents.create_version()` with `PromptAgentDefinition`) after it was dropped from the later orchestrator line. The restoration preserves the uploaded-document conversation scope fix from issue #478, so Agent Service strategies reuse definition-fingerprinted agents without regressing per-conversation retrieval.
- **Uploaded documents now retrieved in chat (issue #478):** `SingleAgentRAGStrategyV2` now scopes the Azure AI Search retrieval to the active conversation. It passes the chat `conversation_id` (from the orchestrator-provided conversation dict, falling back to `set_context()`) into `set_request_context`, so the search filter includes per-conversation uploaded chunks (`conversationId eq '<cid>' or conversationId eq 'NaN'`) instead of only the shared corpus. Previously the conversation id was never propagated, so uploaded documents were filtered out and the model answered without them.

All notable changes to this project will be documented in this file.
This format follows [Keep a Changelog](https://keepachangelog.com/) and adheres to [Semantic Versioning](https://semver.org/).

## [v2.6.11] - 2026-05-27

### Changed
- **Dependency refresh:** Updated `python-dotenv` to 1.2.2 for runtime configuration loading.

## [v2.6.10] - 2026-05-27

### Changed
- **Dependency refresh:** Updated `requests` to 2.33.0, `aiohttp` to 3.13.4 for the runtime and evaluation dependencies.

## [v2.6.9] - 2026-05-27

### Added
- **Bounded conversation persistence:** Added byte- and message-based compaction for persisted conversation documents so long chats keep recent context without growing Cosmos DB items indefinitely. Implements [Azure/GPT-RAG#448](https://github.com/Azure/GPT-RAG/issues/448).
- **Retrieval-needed triage:** Extended local MAF strategies to distinguish greetings, retrieval-needed questions, and no-retrieval follow-ups such as formatting, translation, summarization, or rephrasing of the previous answer.

## [v2.6.8] - 2026-05-26

### Changed
- **NL2SQL now uses Microsoft Agent Framework without Agent Service agent creation:** Replaced the Semantic Kernel Agent Service group-chat implementation with a direct Microsoft Agent Framework `ChatAgent` workflow backed by local NL2SQL tools, eliminating per-request Foundry Agent Service agent creation and the `AgentsOperations.create_agent` failure path. Fixes [Azure/GPT-RAG#461](https://github.com/Azure/GPT-RAG/issues/461).

### Fixed
- **NL2SQL startup and response latency:** NL2SQL no longer pre-warms Agent Service on startup or creates three server-side agents per request; it now performs deterministic datasource/table/schema/query steps locally and uses MAF only for triage, SQL generation, and answer synthesis. Fixes [Azure/GPT-RAG#462](https://github.com/Azure/GPT-RAG/issues/462).
- **Conversation rename API schema:** Added typed request/response models for the existing conversation rename endpoint while preserving its authorization and ownership checks. Supports [Azure/GPT-RAG#435](https://github.com/Azure/GPT-RAG/issues/435).

## [v2.6.7] - 2026-05-26

### Fixed
- **Azure CLI warning-safe deploy verification:** Filter Azure CLI warning and progress lines from App Configuration, Container Apps update, and image verification output before consuming TSV values, so Windows deploys do not fail when the Azure CLI or Container Apps extension emits non-data output. Fixes [Azure/GPT-RAG#449](https://github.com/Azure/GPT-RAG/issues/449).

## [v2.6.6] - 2026-05-26

### Fixed
- **Container Apps image update verification:** Replaced the mandatory latest-revision restart with explicit image verification after `az containerapp update --image`, avoiding transient `Not Found` failures immediately after revision creation while still confirming the new image is configured. Fixes [Azure/GPT-RAG#449](https://github.com/Azure/GPT-RAG/issues/449).

## [v2.6.5] - 2026-05-25

### Fixed
- **Docker-free component deployment:** Updated Bash and PowerShell deploy scripts to choose the build mode before touching Docker, use `az acr build` when Docker is unavailable or remote build is requested, configure Container App registry identity, and restart the latest revision after image updates. Fixes [Azure/GPT-RAG#449](https://github.com/Azure/GPT-RAG/issues/449).

## [v2.6.4] - 2026-05-25

### Fixed
- **Strategy-aware Agent Service startup warmup:** The orchestrator now pre-warms Azure AI Foundry Agent Service only when the active `AGENT_STRATEGY` uses it. The default `maf_lite` strategy no longer creates or contacts Agent Service during startup, preventing unwanted startup-created agents. The `single_agent_rag` startup path now reuses an existing `gpt-rag-agent-v2` agent by name before creating a new reusable agent, reducing duplicate agent creation. Fixes [Azure/GPT-RAG#456](https://github.com/Azure/GPT-RAG/issues/456).

## [v2.6.3] - 2026-05-19

### Added
- **Per-conversation document retrieval filter:** Updated retrieval across all strategies to filter Azure AI Search chunks by `conversationId eq '<cid>' or conversationId eq 'NaN'`, so retrieval returns both chunks ingested for the current conversation (via the new `POST /ingest-documents` endpoint in `gpt-rag-ingestion`) and chunks shared across all users (`conversationId = 'NaN'`). Implements [Azure/GPT-RAG#401](https://github.com/Azure/GPT-RAG/issues/401). ([#188](https://github.com/Azure/gpt-rag-orchestrator/pull/188))

### Fixed
- **Missing `regex` dependency in `requirements.txt`:** Added `regex>=2022.1.18` as an explicit dependency. The `tiktoken==0.8.0` package requires `regex` at runtime, but it was not listed in `requirements.txt`. This caused pip dependency resolver warnings when installing additional packages (such as `agentops-toolkit`) on top of the project dependencies, since `tiktoken` would report an unsatisfied requirement. Users installing into a fresh virtual environment could also encounter import errors from `tiktoken`.

## [v2.6.2] - 2026-04-18
### Fixed
- **OpenTelemetry version pinning:** Pinned `azure-monitor-opentelemetry==1.8.7`, `azure-monitor-opentelemetry-exporter==1.0.0b49`, `opentelemetry-instrumentation-httpx==0.61b0`, and `opentelemetry-instrumentation-fastapi==0.61b0` in `requirements.txt`. Unpinned versions caused non-deterministic Docker builds where an older exporter (referencing the removed `LogData` class) could be paired with `opentelemetry-sdk>=1.39.0`, crashing the container on startup with `ImportError: cannot import name 'LogData' from 'opentelemetry.sdk._logs'`. ([#445](https://github.com/Azure/GPT-RAG/issues/445))
- **Permission trimming header format:** Removed erroneous `Bearer ` prefix from the `x-ms-query-source-authorization` header value in both the REST API path (`search.py`) and the SDK path (`search_context_provider.py`). Azure AI Search expects the raw OBO token without the prefix; including it caused `400 Invalid header` errors when `permissionFilterOption` was enabled on the search index. ([#447](https://github.com/Azure/GPT-RAG/issues/447))

## [v2.6.1] - 2026-04-01
### Fixed
- **Multimodal image rendering:** Fixed images not appearing in multimodal responses caused by `reasoning_effort` parameter being rejected by gpt-5-nano in the image classifier, validator, and intent classification calls. Removed the unsupported parameter from all three LLM classification calls.
- **Image validation guardrail:** Fixed the post-response image validator returning verbose 200-token descriptions instead of "VALID"/"INVALID" classifications. Strengthened the validation prompt, reframed the user message to prevent Q&A-style responses, and reduced `max_completion_tokens` from 200 to 10.
- **Inline image placement:** Fixed images being grouped at the bottom of responses in a "Visual Guides" section instead of appearing inline. Added a few-shot example to the system prompt demonstrating correct inline placement with introductory sentences, and reinforced inline rules in both the main prompt and the search context provider prompt.
- **Search retry without OBO:** Added fallback logic in `MultimodalSearchContextProvider` to retry search without the `x_ms_query_source_authorization` header when the OBO token exchange fails, preventing total search failure when admin consent is not granted.
- **Assistant history few-shot poisoning:** Stripped `![](path)` markdown from old assistant messages in conversation history to prevent them from acting as few-shot examples that teach the model to omit image embedding.

## [v2.6.0] - 2026-03-31
### Added
- **Conversation History REST API:** New CRUD endpoints (`GET /conversations`, `GET /conversations/{id}`, `PATCH /conversations/{id}`, `DELETE /conversations/{id}`) enabling front-end conversation list, detail view, rename, and soft-delete operations.
- **Pydantic response schemas:** Added `ConversationMetadata`, `ConversationListResponse`, and `ConversationDetail` models for typed API responses.
- **CosmosDB conversation query helpers:** Added `query_user_conversations`, `read_user_conversation`, `update_conversation_name`, and `soft_delete_conversation` module-level functions with parameterized queries.
- **Conversation history unit tests** (`test_conversation_history.py`).

### Changed
- **Multi-tenant partition key support:** `CosmosDBClient.get_document` and `create_document` now accept an optional `partition_key` parameter, enabling per-user partition isolation (`principal_id` for authenticated users, `anonymous-{conversation_id}` for unauthenticated users).
- **Message persistence in strategies:** `SingleAgentRAGStrategyV2` now captures the full streamed response and appends `{"role": "user", "text": ...}` / `{"role": "assistant", "text": ...}` messages to the conversation document for both direct-LLM and Agent SDK paths.
- **Orchestrator user identity plumbing:** `Orchestrator.__init__` now receives `principal_id` from `user_context`, populates partition key, and stores `name`, `principal_id`, and `lastUpdated` on new conversation documents.
- **Automatic `lastUpdated` timestamps:** `CosmosDBClient.create_document` and `update_document` now set `lastUpdated` automatically.
- **Conversation not-found handling:** When a conversation ID is provided but the document does not exist, the orchestrator now creates a new conversation instead of raising an error.

### Improved
- **Citation prompt rules:** Strengthened citation instructions in `single_agent_rag/main.jinja2` and added `_CITATION_RULES` constant in `SingleAgentRAGStrategyV2` to enforce `[title](link)` format, prevent link omission, and limit citation repetition.

### Fixed
- Normalized line endings from CRLF to LF across 32 files for cross-platform consistency.

## [v2.5.0] - 2026-03-24
### Added
- **MafLiteStrategy (`maf_lite`):** New orchestration strategy using the Microsoft Agent Framework with direct Azure OpenAI model access. Provides the same MAF features (ChatAgent, UserProfileMemory, AzureAISearchContextProvider, retrieval plugins) without requiring Azure AI Foundry Agent Service v2, offering a lighter-weight deployment option.
- **OpenAIChatClient:** Custom `ChatClientProtocol` adapter wrapping `AsyncAzureOpenAI` for direct model access, used by the `maf_lite` strategy.
- **Citation utilities module (`src/util/citations.py`):** Extracted citation processing functions into a shared utility module.
- **Unit test suite:** Added comprehensive unit tests for strategies, factory, citations, and OpenAI chat client (46 tests).

### Changed
- **Renamed `MafStrategy` to `MafAgentServiceStrategy` (`maf_agent_service`):** Clarifies that this strategy uses the Microsoft Agent Framework **with** Azure AI Foundry Agent Service v2. The App Configuration key changed from `maf` to `maf_agent_service`.
- **Strategy lineup table in README:** Added a quick-reference table listing all available strategies with their configuration keys.
- **Default strategy changed to `maf_lite`:** The fallback strategy when `AGENT_STRATEGY` is not set in App Configuration is now `maf_lite` instead of `single_agent_rag`.

### Fixed
- **MAF Agent Service lifecycle cleanup:** Updated `maf_agent_service` to use request-scoped Agent Service client ownership, preventing leaked async HTTP sessions that produced `Unclosed client session`, `Unclosed connector`, and `SSL shutdown timed out` errors in runtime logs.
- **User profile extraction warnings in Agent Service path:** Guarded unsupported extraction path in `UserProfileMemory` for `AzureAIAgentClient`, preventing noisy `'dict' object has no attribute 'conversation_id'` warnings in normal operation.
- **Conversation persist teardown path:** Updated orchestrator stream teardown to persist conversation with awaited completion at end-of-stream, reducing detached-task/span lifecycle noise during request finalization.
- **`azure-search-documents` version conflict:** Updated from `11.7.0b1` to `11.7.0b2` to align with `agent-framework-azure-ai-search` dependency.
- **`opentelemetry-semantic-conventions-ai` version incompatibility:** Pinned to `>=0.4.13,<0.5.0` to prevent `SpanAttributes.LLM_SYSTEM` missing error in `agent-framework-core`.
- **`opentelemetry-instrumentation-httpx` version conflict:** Unpinned from `==0.52b1` to allow pip to resolve a compatible version with `agent-framework-core` and `semantic-kernel`.

### Removed
- **`SingleAgentRAGStrategyV1` (`single_agent_rag_v1`):** Removed the legacy V1 RAG strategy and its alias module. The `single_agent_rag` key now exclusively maps to V2.

## [v2.4.2] - 2026-02-28
### Added
- **Microsoft Agent Framework (MAF) + Azure AI Foundry Agent Service v2:** Upgraded the orchestration platform from `azure-ai-projects==1.1.0b3` to `azure-ai-projects==2.0.0b3`, introducing the new decoupled `AgentsClient` (`azure.ai.agents.aio.AgentsClient`) with native event-handler streaming, replacing the legacy `AIProjectClient` polling-based approach.
- **SingleAgentRAGStrategyV2:** New orchestration strategy optimized for latency built on top of MAF v2. Incorporates dynamic routing that cuts Time To First Byte (TTFB) from multiple seconds to sub-milliseconds by bypassing the cloud Agent backend when the index is empty, routing to direct in-memory LLM streaming.
- **Granular MAF Latency Telemetry:** Added detailed millisecond-level latency tracking (`[Agent Flow V2][Telemetry]`) to measure Thread initialization, Agent Creation, Message Insertion, and Time To First Token (TTFB) when interacting with the V2 SDK.
- **Identity Singleton Pattern:** Implemented a new `IdentityManager` to centralize Azure Entra ID credential instantiation across plugins and connections, significantly minimizing repetitive token acquisition overhead (`~0.5s` savings per external request).
- **FastAPI Startup Pre-warming:** Injected `lifespan` routines in `main.py` to pre-fetch Azure tokens and pre-warm Azure AI Search indices upon application boot, successfully eliminating Cold-Start latencies on the first user query.

### Changed
- **Asynchronous CosmosDB Persistence (Latency Reduction):** Conversation history saving now runs in the background using `asyncio.create_task` within `orchestrator.py`, decoupling database I/O from the HTTP response and reducing total request latency by ~4 seconds.
- **Index Validation Cache (Latency Bypass):** The `is_index_empty` check in Azure AI Search now utilizes a local in-memory cache. This prevents repetitive network penalties (~3-5 seconds) caused by constantly probing the index; validation now takes `0.00s`.
- **Native Streaming Integration (MAF V2):** Replaced the old asynchronous polling loop for Azure AI Foundry with Event Handlers from the new `AgentsClient` API to dispatch the response stream directly to the user at ultra-fast speeds.

### Performance
- **CosmosDBClient Persistent Connection + Singleton:** Replaced per-operation `CosmosClient` creation (`async with CosmosClient(...)` on every CRUD call) with a single persistent client instance reused across the application lifetime. Conversation persist latency reduced from `~4.38s` to `~0.88s` (`-80%`).
- **SearchClient Singleton + Shared aiohttp Session:** Eliminated per-request `aiohttp.ClientSession()` creation by introducing a lazily-initialized shared session (`_get_session()`) and a module-level `get_search_client()` singleton. Removes redundant TCP/TLS handshakes on every search call.
- **AgentsClient Module-Level Singleton:** Replaced per-stream `AgentsClient` instantiation with a module-level singleton (`_agents_client`), eliminating repeated TCP/TLS negotiation and token acquisition on every user request.
- **AgentsClient Startup Pre-warming (`prewarm_agents_client`):** Added a startup routine that creates the `AgentsClient` singleton and forces the first HTTP round-trip (`list_agents(limit=1)`) during FastAPI `lifespan`, so the initial user request avoids the `~5-6s` cold-start penalty for connection setup and token acquisition.
- **GenAIModelClient Singleton Reuse:** Strategy V2 now uses `get_genai_client()` instead of creating a new `GenAIModelClient()` per instantiation, sharing the pre-warmed embedding and LLM client across all strategies and plugins.
- **Parallel Thread + Agent Creation (`asyncio.gather`):** Thread initialization and Agent creation API calls to Azure AI Foundry now execute concurrently via `asyncio.gather()`, saving `~1-2s` by overlapping two independent network round-trips that were previously sequential.
- **Agent Pre-Creation at Startup (`prewarm_agents_client`):** The reusable MAF agent (with tools and instructions) is now created once during FastAPI startup and cached at module level (`_cached_agent`). Every request resolves the agent in `~0ms` instead of issuing a `create_agent` POST + `delete_agent` per request. Thread + Agent setup reduced from `~4.34s` to `~2.60s` (`-40%`), where the remaining time is purely thread creation (irreducible Azure API latency). Also supports pre-fetching when `AGENT_ID` is configured externally.
- **Orchestrator.create Overhead Reduction:** All connector initializations in `Orchestrator.__init__` and strategy constructors now resolve to pre-warmed singletons, reducing `Orchestrator.create` latency from `~0.62s` to `~0.005s` (`-99%`).
- **Overall Controllable Overhead:** Total application-side controllable overhead (excluding irreducible Azure service latency) reduced from `~12.2s` to `~6.2s` (`-49%`).

### Fixed
- Fixed `AttributeError: enable_auto_function_calls` due to a change in the tool registration API in SDK v2 compared to V1.
- Fixed `AttributeError: threads` on `AIProjectClient` by refactoring the orchestration to use the new decoupled 2.0 API `azure.ai.agents.aio.AgentsClient`.
- Resolved Jinja2 error (`'aisearch_enabled' is undefined`) ensuring the `aisearch` and `bing` contexts are properly added when injecting base prompts.
- Reduced Cosmos DB update error log verbosity by emitting concise status-based warnings and moving detailed diagnostics to debug logs.
- Fixed V2 direct LLM bypass streaming call to use the correct OpenAI chat client API (`openai_client.chat.completions.create`) instead of an invalid `model_client` attribute.
- Removed explicit `temperature/top_p` in V2 direct LLM bypass streaming to stay compatible with models that only support default sampling values.
- Reinforced single-agent prompt language policy so fallback/uncertainty responses stay in the user's language instead of defaulting to English.
- Fixed stale Azure AI Search empty-index cache in V2 routing by adding TTL-based cache revalidation and cache self-healing when retrieval returns documents.

## [v2.4.1] – 2026-02-04
### Fixed
- Updated the Docker image to install Microsoft's current public signing key, fixing build failures caused by SHA-1 signature rejection in newer Debian/apt verification policies.
- Fixed Docker builds on ARM-based machines by explicitly setting the target platform to `linux/amd64`, preventing Azure Container Apps deployment failures.
### Changed
- Pinned the Docker base image to `mcr.microsoft.com/devcontainers/python:3.12-slim` to ensure stable package verification behavior across environments.
- Bumped `aiohttp` to `3.13.3`.
- Standardized on the container best practice of using a non-privileged port (`8080`) instead of a privileged port (`80`), reducing the risk of runtime/permission friction and improving stability of long-running ingestion workloads.

## [v2.4.0] – 2026-01-15
### Added
- End-to-end document-level security: added Microsoft Entra ID authentication in the UI and end-user access token validation in the orchestrator to establish user identity and authorization context.
  Retrieval enforcement uses Azure AI Search native ACL/RBAC trimming with end-user identity propagation via `x-ms-query-source-authorization`, plus permission-aware indexing metadata (userIds/groupIds/rbacScope), safe-by-default behavior when no valid user token is present, and optional elevated-read debugging support.

## [v2.3.0] – 2025-12-15
### Added
- Refactored Single Agent Strategy to simplify citation handling. [#161](https://github.com/Azure/gpt-rag-orchestrator/pull/161)
- Simplified MCP Strategy. [#159](https://github.com/Azure/gpt-rag-orchestrator/pull/159)
### Tested
- Compatibility with Azure direct models for inference

## [v2.2.1] – 2025-10-21
### Added
- Added more troubleshooting logs.
### Fixed
- Citations [387](https://github.com/Azure/GPT-RAG/issues/387)

## [v2.2.0] – 2025-10-16
### Added
- Added [Agentic Retrieval](https://github.com/Azure/GPT-RAG/issues/359) to the Single Agent Strategy.

## [v2.1.0] – 2025-08-31
### Added
- User Feedback Loop. [#358](https://github.com/Azure/GPT-RAG/issues/358)
### Changed
- Standardized resource group variable as `AZURE_RESOURCE_GROUP`. [#365](https://github.com/Azure/GPT-RAG/issues/365)

## [v2.0.3] – 2025-08-20
### Added
- NL2SQL docs and improved settings checks.

## [v2.0.2] – 2025-08-18
### Fixed
- Corrected v2.0.1 deployment issues.

## [v2.0.1] – 2025-08-08
### Fixed
- Corrected v2.0.0 deployment issues.

## [v2.0.0] – 2025-07-22
### Changed
- Major architecture refactor to support the vNext architecture.

## [v1.0.0]
- Original version.