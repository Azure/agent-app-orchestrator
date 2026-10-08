"""Registry of orchestrator settings exposed by the admin dashboard.

This module is the **single source of truth** for the curated set of App
Configuration keys the admin Configuration tab can read and write. It owns
the human-readable labels, tooltip descriptions, per-option help text, types,
and validation bounds - both the backend endpoints and the frontend tab
consume the same metadata (the frontend receives it as part of the GET
response so we never duplicate the strings on the client).

Two security primitives live here as well:

* :data:`ALLOWED_KEYS` - the orchestrator-specific allowlist. Only keys in
  this set may ever be returned by ``GET /api/dashboard/config`` or accepted
  by ``PUT /api/dashboard/config``.
* :data:`DENYLIST` - defense in depth: even if a future refactor accidentally
  adds a sensitive key to the registry, writes to anything matching the
  denylist are rejected at the API layer.

Adding a new setting is a one-line change here. Adding a new sensitive key to
deny is also a one-line change. The intent is that this file is reviewed
specifically when settings are added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional

from strategies.agent_strategies import AgentStrategies


SettingType = Literal["enum", "bool", "int", "float"]


@dataclass(frozen=True)
class SettingOption:
    """One entry in an enum dropdown, with per-value tooltip text."""
    value: str
    label: str
    description: str


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for a single exposed App Configuration key."""
    key: str
    type: SettingType
    default: Any
    label: str
    description: str
    options: Optional[List[SettingOption]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: Optional[str] = None


@dataclass(frozen=True)
class SettingSection:
    """A group of related settings rendered as one card in the UI."""
    id: str
    label: str
    description: str
    settings: List[SettingSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Enum option helpers
# ---------------------------------------------------------------------------

# The dropdown only exposes the strategies actually documented in issue #512.
# ``AgentStrategies`` also contains ``multiagent`` (legacy) which we
# deliberately omit. Each option's description explains the trade-off so a
# screen reader user gets the same context as a sighted user hovering the (i).
_AGENT_STRATEGY_OPTIONS: List[SettingOption] = [
    SettingOption(
        value=AgentStrategies.SINGLE_AGENT_RAG.value,
        label="Single agent RAG",
        description=(
            "One Semantic Kernel agent answers each turn using the configured "
            "retrieval plugins. Best for classic Retrieval Augmented Generation "
            "use cases with predictable latency and cost."
        ),
    ),
    SettingOption(
        value=AgentStrategies.MAF_LITE.value,
        label="Microsoft Agent Framework (lite)",
        description=(
            "Single MAF chat agent without the Foundry agent service. Lower "
            "latency than the full agent service and the recommended starting "
            "point for new MAF deployments."
        ),
    ),
    SettingOption(
        value=AgentStrategies.MAF_AGENT_SERVICE.value,
        label="Microsoft Agent Framework (Foundry Agent Service)",
        description=(
            "MAF agent backed by the hosted Foundry Agent Service. Adds server "
            "side tool execution and persistent threads at the cost of an extra "
            "network hop per turn."
        ),
    ),
    SettingOption(
        value=AgentStrategies.MCP.value,
        label="MCP",
        description=(
            "Routes tool calls through Model Context Protocol servers. Use when "
            "the orchestrator must expose tools defined by an external MCP host."
        ),
    ),
    SettingOption(
        value=AgentStrategies.NL2SQL.value,
        label="Natural language to SQL",
        description=(
            "Specialised strategy that translates user questions into SQL "
            "against the configured database. Disables free-form RAG behaviour."
        ),
    ),
    SettingOption(
        value=AgentStrategies.MULTIMODAL.value,
        label="Multimodal",
        description=(
            "Image-aware strategy that can classify and reason about images in "
            "the conversation. Pair with MULTIMODAL_CLASSIFY_IMAGES below."
        ),
    ),
]


_REASONING_EFFORT_OPTIONS: List[SettingOption] = [
    SettingOption("minimal", "Minimal", "Fastest and cheapest. Skip almost all reasoning steps."),
    SettingOption("low", "Low", "Some chain-of-thought. Good default for short tasks."),
    SettingOption("medium", "Medium", "Balanced reasoning depth. Recommended default."),
    SettingOption("high", "High", "Deeper deliberation. Higher latency and cost."),
]


_RETRIEVAL_BACKEND_OPTIONS: List[SettingOption] = [
    SettingOption(
        "ai_search",
        "Azure AI Search",
        "Retrieve from the Azure AI Search RAG index (default, current behavior).",
    ),
    SettingOption(
        "foundry_iq",
        "Foundry IQ",
        "Retrieve from a Foundry IQ knowledge base. Requires KNOWLEDGE_BASE_NAME "
        "and KNOWLEDGE_BASE_ENDPOINT to be provisioned.",
    ),
]

_FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND_OPTIONS: List[SettingOption] = [
    SettingOption(
        "azureBlob",
        "Azure Blob or ADLS Gen2",
        "Native Foundry IQ file ingestion from Blob Storage or ADLS Gen2. This is the default for Foundry IQ.",
    ),
    SettingOption(
        "searchIndex",
        "Existing Azure AI Search index",
        "Legacy Pattern B: register the Agent Landing Zone generated search index and optionally apply filterAddOn.",
    ),
    SettingOption(
        "workIQ",
        "Work IQ (Microsoft 365)",
        "Remote Microsoft 365 knowledge source (mail, files, chats). Requires the "
        "gated Work IQ preview and OBO (x-ms-query-source-authorization). ACL is "
        "enforced natively by M365 - no filterAddOn is applied.",
    ),
    SettingOption(
        "fabricOntology",
        "Fabric IQ (Microsoft Fabric ontology)",
        "Remote Microsoft Fabric knowledge source backed by a Fabric ontology. "
        "Requires OBO (x-ms-query-source-authorization); ACL is enforced "
        "natively by Fabric so no filterAddOn is applied.",
    ),
    SettingOption(
        "fabricDataAgent",
        "Fabric Data Agent (Microsoft Fabric)",
        "Remote Microsoft Fabric knowledge source backed by a Fabric Data "
        "Agent (virtual analyst over Fabric data). Requires OBO "
        "(x-ms-query-source-authorization); ACL is enforced natively by "
        "Fabric so no filterAddOn is applied.",
    ),
]

# ---------------------------------------------------------------------------
# Sections (renders as one card per section in the UI, top to bottom)
# ---------------------------------------------------------------------------

SECTIONS: List[SettingSection] = [
    SettingSection(
        id="agent_and_generation",
        label="Agent and generation",
        description=(
            "Which orchestration strategy answers each turn and how creative "
            "the underlying chat model is allowed to be."
        ),
        settings=[
            SettingSpec(
                key="AGENT_STRATEGY",
                type="enum",
                default=AgentStrategies.SINGLE_AGENT_RAG.value,
                label="Agent strategy",
                description=(
                    "Selects the orchestration strategy that handles each user "
                    "turn. Changing this swaps which agent class is "
                    "instantiated; new requests pick up the change after the "
                    "configuration cache is refreshed."
                ),
                options=_AGENT_STRATEGY_OPTIONS,
            ),
            SettingSpec(
                key="REASONING_EFFORT",
                type="enum",
                default="medium",
                label="Reasoning effort",
                description=(
                    "Maps to the OpenAI Responses API ``reasoning.effort`` "
                    "parameter. Lower effort is faster and cheaper; higher "
                    "effort lets the model deliberate longer before answering."
                ),
                options=_REASONING_EFFORT_OPTIONS,
            ),
            SettingSpec(
                key="CHAT_TEMPERATURE",
                type="float",
                default=1.0,
                label="Chat temperature",
                description=(
                    "Controls randomness of generated text. 0 is deterministic, "
                    "1 is the model default, values above 1 increase creativity "
                    "but can produce off-topic or hallucinated content."
                ),
                min=0.0,
                max=2.0,
                step=0.1,
            ),
            SettingSpec(
                key="CHAT_TOP_P",
                type="float",
                default=1.0,
                label="Chat top-p (nucleus sampling)",
                description=(
                    "Limits sampling to the smallest set of tokens whose "
                    "cumulative probability is at least top-p. Lower values "
                    "make the model more focused; 1.0 disables nucleus "
                    "filtering."
                ),
                min=0.0,
                max=1.0,
                step=0.05,
            ),
            SettingSpec(
                key="MAX_COMPLETION_TOKENS",
                type="int",
                default=4096,
                label="Max completion tokens",
                description=(
                    "Upper bound on the number of tokens generated per "
                    "response. Larger values allow longer answers but also "
                    "increase latency and cost; the model may still stop "
                    "earlier on its own."
                ),
                min=1,
                max=32768,
                step=1,
                unit="tokens",
            ),
        ],
    ),
    SettingSection(
        id="conversation_history",
        label="Conversation history",
        description=(
            "How much prior conversation is replayed back to the model and "
            "whether older turns are compacted to keep the window small."
        ),
        settings=[
            SettingSpec(
                key="CHAT_HISTORY_MAX_MESSAGES",
                type="int",
                default=20,
                label="Max history messages",
                description=(
                    "Maximum number of prior turns replayed to the model on "
                    "every request. Higher values give the model more context "
                    "but use more tokens per call."
                ),
                min=0,
                max=200,
                step=1,
                unit="messages",
            ),
            SettingSpec(
                key="CONVERSATION_HISTORY_COMPACTION_ENABLED",
                type="bool",
                default=False,
                label="Compact long histories",
                description=(
                    "When enabled, older turns are summarised once the history "
                    "exceeds the max-messages threshold. Reduces token usage on "
                    "long sessions but introduces an extra summarisation call."
                ),
            ),
        ],
    ),
    SettingSection(
        id="retrieval_and_search",
        label="Retrieval and search",
        description=(
            "Which retrieval sources the orchestrator consults before "
            "answering, and how many results each source returns."
        ),
        settings=[
            SettingSpec(
                key="RETRIEVAL_BACKEND",
                type="enum",
                default="ai_search",
                label="Retrieval backend",
                description=(
                    "Selects which retrieval backend serves grounding "
                    "documents. 'ai_search' uses the Azure AI Search RAG index "
                    "(default). 'foundry_iq' targets a Foundry IQ knowledge "
                    "base. The retrieved context shape and citations are "
                    "identical across backends."
                ),
                options=_RETRIEVAL_BACKEND_OPTIONS,
            ),
            SettingSpec(
                key="FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="Foundry IQ knowledge source name",
                description=(
                    "Knowledge source to target when RETRIEVAL_BACKEND=foundry_iq. "
                    "By default this is the native Blob/ADLS source created by the "
                    "deployment. For Pattern B, set it to the registered Agent Landing Zone "
                    "Azure AI Search index knowledge source."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND",
                type="enum",
                default="azureBlob",
                label="Foundry IQ knowledge source kind",
                description=(
                    "Controls the kind sent in knowledgeSourceParams. Use azureBlob "
                    "for the native Foundry IQ file-ingestion default; use searchIndex "
                    "only for the legacy Pattern B index-registration path."
                ),
                options=_FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND_OPTIONS,
            ),
            SettingSpec(
                key="FOUNDRY_IQ_FILTER_ADD_ON_ENABLED",
                type="bool",
                default=False,
                label="Enable Pattern B filterAddOn",
                description=(
                    "When enabled, Foundry IQ retrieve requests add a "
                    "filterAddOn OData expression for Agent Landing Zone custom security "
                    "fields. This is distinct from x-ms-query-source-authorization, "
                    "which is used for native Foundry IQ permission-aware sources."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_SECURITY_FIELD_NAME",
                type="string",
                default="metadata_security_id",
                label="Pattern B security field",
                description=(
                    "Collection field used by the registered Azure AI Search "
                    "index for Agent Landing Zone security trimming. Keep the default for "
                    "standard Agent Landing Zone indexes."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS",
                type="int",
                default=5,
                label="Foundry IQ max output documents",
                description=(
                    "Caps grounding documents returned by the Foundry IQ "
                    "retrieve action before the orchestrator builds prompt context."
                ),
                min=1,
                max=50,
                step=1,
            ),
            SettingSpec(
                key="FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED",
                type="bool",
                default=False,
                label="Enable conversation file upload (Pattern A)",
                description=(
                    "When RETRIEVAL_BACKEND=foundry_iq and the primary knowledge "
                    "source is the native azureBlob corpus, also query a second "
                    "searchIndex source built over the existing Agent Landing Zone index so "
                    "files uploaded in the chat UI are grounded alongside the "
                    "shared corpus. The upload source is trimmed by a "
                    "conversationId plus caller-ownership filterAddOn, so "
                    "uploads stay scoped to their uploader in the conversation "
                    "that created them. "
                    "No effect for Pattern B, which already scopes by "
                    "conversationId."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="Conversation upload knowledge source name",
                description=(
                    "Name of the searchIndex knowledge source that carries "
                    "chat-UI uploads when FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED "
                    "is on. Provisioned by the deployment; defaults to "
                    "'<SEARCH_RAG_INDEX_NAME>-conv-ks'. Leave blank to let the "
                    "deployment set it."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MAX_RUNTIME_SECONDS",
                type="int",
                default=120,
                label="Foundry IQ retrieve max runtime",
                description=(
                    "Upper bound on the Foundry IQ retrieve action runtime, sent "
                    "as maxRuntimeInSeconds on the request body. Remote knowledge "
                    "sources and generic MCP tools can take 40–60 seconds; the "
                    "default leaves headroom. Valid range is 30–600 seconds. Only "
                    "emitted when a remote, web, or MCP source is enabled, so "
                    "default Pattern A / Pattern B request bodies remain unchanged."
                ),
                min=30,
                max=600,
                step=1,
                unit="seconds",
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MCP_ENABLED",
                type="bool",
                default=False,
                label="Enable generic MCP knowledge sources (Preview)",
                description=(
                    "Opt-in Preview. Uses conversational Foundry IQ planning to "
                    "invoke preprovisioned mcpServer knowledge sources. Disabled "
                    "preserves the existing minimal-reasoning intents request."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MCP_SOURCES_JSON",
                type="string",
                default="[]",
                label="MCP knowledge source metadata (JSON)",
                description=(
                    "Validated array of preprovisioned source names, HTTPS server "
                    "metadata, tool allowlists, parsing limits, failure policy, and "
                    "query-header valueFrom metadata. Literal credentials are "
                    "rejected; use managedIdentity, obo, keyVaultSecret, or none."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MCP_REASONING_EFFORT",
                type="enum",
                default="low",
                label="MCP retrieval reasoning effort",
                description=(
                    "Planner reasoning used only when MCP sources are enabled. "
                    "Low reduces latency and cost; medium can improve tool "
                    "selection for more complex requests."
                ),
                options=[
                    SettingOption("low", "Low", "Lower latency and cost."),
                    SettingOption(
                        "medium",
                        "Medium",
                        "More planning for complex tool selection and arguments.",
                    ),
                ],
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MCP_TRUSTED_HOSTS",
                type="string",
                default="",
                label="Trusted MCP server hosts",
                description=(
                    "Required comma-separated exact host allowlist for enabled "
                    "MCP sources. Enter hostnames only, without schemes, ports, "
                    "paths, wildcards, localhost, or IP literals."
                ),
            ),
            SettingSpec(
                key="FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS",
                type="bool",
                default=False,
                label="Log MCP tool arguments",
                description=(
                    "Debug-only opt-in. Normal telemetry omits tool arguments. "
                    "When enabled, argument values under credential, token, "
                    "cookie, authorization, and header keys remain redacted."
                ),
            ),
            SettingSpec(
                key="WORK_IQ_ENABLED",
                type="bool",
                default=False,
                label="Enable Work IQ knowledge source",
                description=(
                    "When enabled and WORK_IQ_KNOWLEDGE_SOURCE_NAME is set, the "
                    "Foundry IQ retrieve request appends a workIQ knowledge source "
                    "for Microsoft 365 grounding (mail, files, chats). Requires "
                    "the gated Work IQ preview and admin consent on the Foundry "
                    "connector. OBO is mandatory - anonymous / managed-identity "
                    "fallback is never used for Work IQ."
                ),
            ),
            SettingSpec(
                key="WORK_IQ_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="Work IQ knowledge source name",
                description=(
                    "Name of the Work IQ knowledge source registered on the "
                    "knowledge base. Required when WORK_IQ_ENABLED is true. "
                    "No effect when Work IQ is disabled."
                ),
            ),
            SettingSpec(
                key="FABRIC_IQ_ENABLED",
                type="bool",
                default=False,
                label="Enable Fabric IQ knowledge source",
                description=(
                    "When enabled and FABRIC_IQ_KNOWLEDGE_SOURCE_NAME is set, "
                    "the Foundry IQ retrieve request appends a fabricOntology "
                    "knowledge source for grounding over a Microsoft Fabric "
                    "ontology (semantic model, lakehouse, warehouse, or KQL "
                    "database exposed through the ontology). ACL is enforced "
                    "natively by Fabric via the forwarded per-user OBO token; "
                    "managed-identity fallback is never used for Fabric IQ."
                ),
            ),
            SettingSpec(
                key="FABRIC_IQ_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="Fabric IQ knowledge source name",
                description=(
                    "Name of the fabricOntology knowledge source registered "
                    "on the knowledge base. Required when FABRIC_IQ_ENABLED "
                    "is true. The knowledge source itself binds a Fabric "
                    "workspace and ontology at registration time; no effect "
                    "when Fabric IQ is disabled."
                ),
            ),
            SettingSpec(
                key="FABRIC_DATA_AGENT_ENABLED",
                type="bool",
                default=False,
                label="Enable Fabric Data Agent knowledge source",
                description=(
                    "When enabled and FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME "
                    "is set, the Foundry IQ retrieve request appends a "
                    "fabricDataAgent knowledge source for grounding over a "
                    "Microsoft Fabric Data Agent (virtual analyst over Fabric "
                    "data). ACL is enforced natively by Fabric via the "
                    "forwarded per-user OBO token; managed-identity fallback "
                    "is never used for Fabric Data Agent."
                ),
            ),
            SettingSpec(
                key="FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="Fabric Data Agent knowledge source name",
                description=(
                    "Name of the fabricDataAgent knowledge source registered "
                    "on the knowledge base. Required when "
                    "FABRIC_DATA_AGENT_ENABLED is true. The knowledge source "
                    "itself binds a Fabric workspace and data agent at "
                    "registration time; no effect when Fabric Data Agent is "
                    "disabled."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_REMOTE_ENABLED",
                type="bool",
                default=False,
                label="Enable SharePoint remote (Copilot Retrieval API)",
                description=(
                    "Opt-in. When enabled, the Foundry IQ retrieve call adds "
                    "a remoteSharePoint knowledge source that queries "
                    "SharePoint at retrieval time via the Microsoft 365 "
                    "Copilot Retrieval API. Item-level SharePoint "
                    "permissions are enforced natively by M365 via the "
                    "forwarded per-user OBO token; managed-identity "
                    "fallback is never used for SharePoint remote."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="SharePoint remote knowledge source name",
                description=(
                    "Name of the remoteSharePoint knowledge source "
                    "registered on the knowledge base. Required when "
                    "SHAREPOINT_REMOTE_ENABLED is true. No effect when "
                    "SharePoint remote is disabled."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_REMOTE_FILTER_EXPRESSION_ADD_ON",
                type="string",
                default="",
                label="SharePoint remote filter expression add-on",
                description=(
                    "Optional KQL filter appended at retrieval time to the "
                    "remoteSharePoint knowledge source "
                    "(filterExpressionAddOn). Use it to scope results to a "
                    "specific site, path, or content type. Leave empty for "
                    "no additional filter."
                ),
            ),
            SettingSpec(
                key="ONELAKE_KS_ENABLED",
                type="bool",
                default=False,
                label="Enable OneLake indexed knowledge source",
                description=(
                    "When enabled and ONELAKE_KNOWLEDGE_SOURCE_NAME is set, "
                    "the Foundry IQ retrieve request appends an "
                    "indexedOneLake knowledge source for grounding over a "
                    "Microsoft Fabric OneLake lakehouse. Foundry IQ manages "
                    "the underlying Azure AI Search index internally, so "
                    "OneLake is a native (not remote) kind: it does not "
                    "require a per-user OBO token."
                ),
            ),
            SettingSpec(
                key="ONELAKE_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="OneLake knowledge source name",
                description=(
                    "Name of the indexedOneLake knowledge source registered "
                    "on the knowledge base. Required when ONELAKE_KS_ENABLED "
                    "is true. The knowledge source itself binds a Fabric "
                    "workspace and lakehouse at registration time; no effect "
                    "when OneLake is disabled."
                ),
            ),
            SettingSpec(
                key="ONELAKE_WORKSPACE_ID",
                type="string",
                default="",
                label="OneLake Fabric workspace ID",
                description=(
                    "Identifier of the Microsoft Fabric workspace that hosts "
                    "the OneLake lakehouse bound to the indexedOneLake "
                    "knowledge source. Captured for observability and used "
                    "by platform provisioning to assign Fabric RBAC to the "
                    "Foundry IQ managed identity; not sent on retrieve."
                ),
            ),
            SettingSpec(
                key="ONELAKE_LAKEHOUSE_ID",
                type="string",
                default="",
                label="OneLake Fabric lakehouse ID",
                description=(
                    "Identifier of the Microsoft Fabric lakehouse (within "
                    "ONELAKE_WORKSPACE_ID) bound to the indexedOneLake "
                    "knowledge source. Captured for observability and "
                    "documentation; not sent on retrieve."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_INDEXED_ENABLED",
                type="bool",
                default=False,
                label="Enable SharePoint Indexed knowledge source (preview)",
                description=(
                    "When enabled and SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME "
                    "is set, the Foundry IQ retrieve request appends an "
                    "indexedSharePoint knowledge source. Unlike Work IQ and "
                    "Fabric IQ, indexedSharePoint is a server-side knowledge "
                    "source on the AI Search service: it owns its ingestion "
                    "pipeline and retrieves against a pre-built local index. "
                    "Auth to SharePoint is app-only via a Federated Identity "
                    "Credential (managed identity + Graph Sites.Selected), so "
                    "no OBO token is required at retrieve time. Preview API "
                    "2026-05-01-preview; ACL / site-group trimming is out of "
                    "scope for this preview."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="SharePoint Indexed knowledge source name",
                description=(
                    "Name of the indexedSharePoint knowledge source registered "
                    "on the AI Search service. Required when "
                    "SHAREPOINT_INDEXED_ENABLED is true. The knowledge source "
                    "itself carries the SharePoint site URL, tenant, and "
                    "connectionString at registration time; no effect when "
                    "SharePoint Indexed is disabled."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_INDEXED_INDEX_NAME",
                type="string",
                default="",
                label="SharePoint Indexed target index name",
                description=(
                    "Name of the AI Search index generated by the "
                    "indexedSharePoint knowledge source. AI Search creates the "
                    "index automatically from the knowledge source definition; "
                    "this value is stored for observability and setup scripts. "
                    "Not sent on retrieve requests."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_INDEXED_SITE_URL",
                type="string",
                default="",
                label="SharePoint Indexed site URL",
                description=(
                    "Absolute URL of the SharePoint site the indexedSharePoint "
                    "knowledge source ingests from (for example "
                    "https://contoso.sharepoint.com/sites/knowledge). Stored "
                    "for observability and setup scripts; the actual ingestion "
                    "target is set on the knowledge source connectionString at "
                    "registration time."
                ),
            ),
            SettingSpec(
                key="SHAREPOINT_INDEXED_TENANT_ID",
                type="string",
                default="",
                label="SharePoint Indexed tenant ID",
                description=(
                    "Entra tenant ID that owns the SharePoint site and the "
                    "Entra app registration with the Federated Identity "
                    "Credential used by indexedSharePoint. Stored for "
                    "observability and setup scripts; not sent on retrieve "
                    "requests."
                ),
            ),
            SettingSpec(
                key="WEB_GROUNDING_ENABLED",
                type="bool",
                default=False,
                label="Enable web grounding (Grounding with Bing)",
                description=(
                    "When enabled and WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME is "
                    "set, the Foundry IQ retrieve request appends a web "
                    "knowledge source that grounds answers with Bing Search "
                    "results over the public internet. Public data, no ACL, "
                    "no OBO impersonation; the only request-time trimming is "
                    "the optional allow / block domain lists. Web calls are "
                    "billed per request by Bing and leave the Azure "
                    "compliance boundary."
                ),
            ),
            SettingSpec(
                key="WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME",
                type="string",
                default="",
                label="Web grounding knowledge source name",
                description=(
                    "Name of the web knowledge source registered on the "
                    "knowledge base. Required when WEB_GROUNDING_ENABLED is "
                    "true. Registration itself is handled by the platform's "
                    "post-provision script."
                ),
            ),
            SettingSpec(
                key="WEB_GROUNDING_ALLOWED_DOMAINS",
                type="string",
                default="",
                label="Web grounding allowed domains",
                description=(
                    "Optional comma-separated list of hostnames the web "
                    "knowledge source is allowed to return (for example "
                    "``learn.microsoft.com,azure.microsoft.com``). Applied "
                    "at retrieve time under webParameters.domains. Leave "
                    "empty to search the open web."
                ),
            ),
            SettingSpec(
                key="WEB_GROUNDING_BLOCKED_DOMAINS",
                type="string",
                default="",
                label="Web grounding blocked domains",
                description=(
                    "Optional comma-separated list of hostnames the web "
                    "knowledge source must never return. Applied at "
                    "retrieve time under webParameters.domains."
                ),
            ),
            SettingSpec(
                key="SEARCH_RETRIEVAL_ENABLED",
                type="bool",
                default=True,
                label="Enable Azure AI Search retrieval",
                description=(
                    "Turn the AI Search retrieval plugin on or off globally. "
                    "When off, the orchestrator answers purely from model "
                    "knowledge and any other enabled tools."
                ),
            ),
            SettingSpec(
                key="SEARCH_RAGINDEX_TOP_K",
                type="int",
                default=5,
                label="AI Search top-K",
                description=(
                    "How many documents to retrieve from the RAG index per "
                    "query. Higher values improve recall but increase prompt "
                    "size and latency."
                ),
                min=1,
                max=50,
                step=1,
            ),
            SettingSpec(
                key="BING_RETRIEVAL_ENABLED",
                type="bool",
                default=False,
                label="Enable Bing web grounding",
                description=(
                    "Allow the orchestrator to call the Bing grounding plugin "
                    "for fresh web results. Requires BING_CONNECTION_ID to be "
                    "configured on the Foundry project."
                ),
            ),
        ],
    ),
    SettingSection(
        id="reliability",
        label="Reliability",
        description="How aggressively the orchestrator retries transient model failures.",
        settings=[
            SettingSpec(
                key="INFERENCE_MAX_RETRIES",
                type="int",
                default=3,
                label="Inference max retries",
                description=(
                    "Number of times to retry a failed chat completion call "
                    "before giving up. Higher values improve resilience to "
                    "transient errors at the cost of longer worst-case "
                    "latency."
                ),
                min=0,
                max=10,
                step=1,
            ),
        ],
    ),
    SettingSection(
        id="multimodal",
        label="Multimodal",
        description="Image handling for multimodal strategies.",
        settings=[
            SettingSpec(
                key="MULTIMODAL_CLASSIFY_IMAGES",
                type="bool",
                default=False,
                label="Classify attached images",
                description=(
                    "When enabled, attached images are first sent to a vision "
                    "classifier so downstream agents can react to image "
                    "categories. Disable to skip the extra call when image "
                    "context is not needed."
                ),
            ),
        ],
    ),
]


# Flat allowlist derived from SECTIONS. Anything not in here is invisible to
# the dashboard, both for reads and writes.
ALLOWED_KEYS = frozenset(
    spec.key for section in SECTIONS for spec in section.settings
)


# Explicit denylist of keys that must never be exposed or written through the
# dashboard. The PUT handler enforces this in addition to the allowlist as
# defense-in-depth.
DENYLIST = frozenset(
    {
        "MCP_APP_APIKEY",
        "ORCHESTRATOR_APP_APIKEY",
        "KEY_VAULT_URI",
        "APP_API_TOKEN",
        "DAPR_API_TOKEN",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_SECRET",
        "OAUTH_AZURE_AD_CLIENT_SECRET",
        "OAUTH_AZURE_AD_CLIENT_ID",
        "OAUTH_AZURE_AD_TENANT_ID",
        "AUDIT_HMAC_KEY",
        "APP_CONFIG_ENDPOINT",
    }
)

# Suffix patterns that auto-rejects new sensitive keys without a code change.
_DENY_SUFFIXES = (
    "_APIKEY",
    "_API_KEY",
    "_SECRET",
    "_PASSWORD",
    "_CONNECTION_STRING",
    "_CONNSTRING",
    "_PRIVATE_KEY",
    "_TOKEN",
)

_DENY_PREFIXES = ("OAUTH_",)


def is_denied(key: str) -> bool:
    """Return ``True`` when ``key`` must never be exposed via the dashboard."""
    if key in DENYLIST:
        return True
    upper = key.upper()
    if any(upper.endswith(suffix) for suffix in _DENY_SUFFIXES):
        return True
    if any(upper.startswith(prefix) for prefix in _DENY_PREFIXES):
        return True
    return False


def find_spec(key: str) -> Optional[SettingSpec]:
    """Return the :class:`SettingSpec` for ``key``, or ``None`` if not exposed."""
    for section in SECTIONS:
        for spec in section.settings:
            if spec.key == key:
                return spec
    return None
