"""Foundry IQ knowledge base retrieval client.

Targets the Azure AI Search *knowledge base* retrieve action (Foundry IQ), the
agentic-retrieval surface that runs query planning and parallel retrieval across
the knowledge sources bound to a knowledge base. The client mirrors the
``aiohttp`` style of :class:`connectors.search.SearchClient`: it acquires a
service bearer token for ``https://search.azure.com/.default`` and, when an OBO
user token is supplied, forwards it in the ``x-ms-query-source-authorization``
header for per-user document-level security (query-time ACL/RBAC enforcement).

Pattern A vs Pattern B is mostly a *server-side* knowledge base configuration
concern: the client always targets the configured knowledge base name. The one
query-time difference is Pattern B security-field trimming. When enabled,
Agent Landing Zone injects a ``filterAddOn`` OData filter under ``knowledgeSourceParams``.
That filter is deliberately separate from the native OBO header. The OBO header
drives Foundry IQ permission-aware sources such as ADLS Gen2 ACLs, SharePoint,
Purview, OneLake, and Fabric. The Pattern B filter narrows the registered Agent Landing Zone
Azure AI Search index that carries custom security fields.

Introduced for Azure/agent-landing-zone#526. Per-user security on the retrieve action is a
preview capability, so the API version is pinned to a single configurable
constant (see :data:`DEFAULT_FOUNDRY_IQ_API_VERSION`).
"""

import logging
import json
import math
import time
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

import aiohttp
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from dependencies import get_config
from telemetry import (
    AuditEmitter,
    AuditStatus,
    EventType,
    ReasonCode,
    current_audit_context,
)
from telemetry.audit_contract import (
    MAX_AUDIT_DURATION_MS,
    new_event_id,
    utc_now,
)
from connectors.foundry_iq_mcp import (
    McpCredentialError,
    McpRuntimeConfig,
    build_mcp_control_headers,
    redact_mcp_tool_arguments,
)

# Per-user security on the knowledge base retrieve action (both the native OBO
# path and the Pattern B filterAddOn path) requires this preview API version.
# Core retrieval is GA at 2026-04-01, but we pin the preview so the security
# features are always available. Override via the FOUNDRY_IQ_API_VERSION key.
DEFAULT_FOUNDRY_IQ_API_VERSION = "2026-05-01-preview"


class _FoundryIQRecord(dict[str, str]):
    """Normalized record with non-payload source-kind provenance."""

    source_type: str

# Config key names (kept here so the connector layer doesn't import from api/).
KNOWLEDGE_BASE_NAME_KEY = "KNOWLEDGE_BASE_NAME"
KNOWLEDGE_BASE_ENDPOINT_KEY = "KNOWLEDGE_BASE_ENDPOINT"
FOUNDRY_IQ_API_VERSION_KEY = "FOUNDRY_IQ_API_VERSION"
FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME_KEY = "FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME"
FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND_KEY = "FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND"
FOUNDRY_IQ_PATTERN_KEY = "FOUNDRY_IQ_PATTERN"
FOUNDRY_IQ_FILTER_ADD_ON_ENABLED_KEY = "FOUNDRY_IQ_FILTER_ADD_ON_ENABLED"
FOUNDRY_IQ_SECURITY_FIELD_NAME_KEY = "FOUNDRY_IQ_SECURITY_FIELD_NAME"
FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS_KEY = "FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS"
FOUNDRY_IQ_FORWARD_SOURCE_AUTH_KEY = "FOUNDRY_IQ_FORWARD_SOURCE_AUTH"
# Hybrid file-upload sidecar (Pattern A + UI upload). When enabled, and the
# primary knowledge source is the native azureBlob corpus, the retrieve action
# also queries a second searchIndex knowledge source built over the existing
# Agent Landing Zone index (SEARCH_RAG_INDEX_NAME). That second source carries the runtime
# uploads and is trimmed by a conversationId plus caller-ownership filterAddOn
# so uploaded files are only visible to their uploader inside the conversation
# that created them. Off by default.
FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED_KEY = "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED"
FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME_KEY = (
    "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME"
)
# Upper bound on the retrieve action runtime (maxRuntimeInSeconds on the
# request body). Remote knowledge source kinds such as workIQ / fabricOntology
# can take 40-60s to fan out; the default leaves headroom without penalising
# the fast local kinds. Only emitted when a remote kind is enabled so today's
# Pattern A / Pattern B request bodies remain byte-identical.
FOUNDRY_IQ_MAX_RUNTIME_SECONDS_KEY = "FOUNDRY_IQ_MAX_RUNTIME_SECONDS"
DEFAULT_FOUNDRY_IQ_MAX_RUNTIME_SECONDS = 120

# Generic MCP Server knowledge sources. The feature is preview and disabled by
# default. Server URL, tools, parsing, and auth metadata describe the
# preprovisioned knowledge source and are validated locally; retrieve requests
# reference only the registered source name.
FOUNDRY_IQ_MCP_ENABLED_KEY = "FOUNDRY_IQ_MCP_ENABLED"
FOUNDRY_IQ_MCP_SOURCES_JSON_KEY = "FOUNDRY_IQ_MCP_SOURCES_JSON"
FOUNDRY_IQ_MCP_REASONING_EFFORT_KEY = "FOUNDRY_IQ_MCP_REASONING_EFFORT"
FOUNDRY_IQ_MCP_TRUSTED_HOSTS_KEY = "FOUNDRY_IQ_MCP_TRUSTED_HOSTS"
FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS_KEY = "FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS"

# Work IQ (Microsoft 365 remote knowledge source). Opt-in; default off. When
# enabled, the retrieve request appends a workIQ knowledge source in
# knowledgeSourceParams. ACL is enforced natively by M365, so no filterAddOn
# is applied. OBO is required; MI fallback is never used for remote kinds.
WORK_IQ_ENABLED_KEY = "WORK_IQ_ENABLED"
WORK_IQ_KNOWLEDGE_SOURCE_NAME_KEY = "WORK_IQ_KNOWLEDGE_SOURCE_NAME"

# Fabric IQ (Microsoft Fabric ontology remote knowledge source). Opt-in;
# default off. When enabled, the retrieve request appends a fabricOntology
# knowledge source in knowledgeSourceParams. ACL is enforced natively by
# Fabric via the forwarded per-user OBO token (same
# x-ms-query-source-authorization header used by Work IQ), so no filterAddOn
# is applied. OBO is required; MI fallback is never used for remote kinds.
FABRIC_IQ_ENABLED_KEY = "FABRIC_IQ_ENABLED"
FABRIC_IQ_KNOWLEDGE_SOURCE_NAME_KEY = "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME"

# Fabric Data Agent (Microsoft Fabric Data Agent remote knowledge source).
# Opt-in; default off. When enabled, the retrieve request appends a
# fabricDataAgent knowledge source in knowledgeSourceParams. A Fabric Data
# Agent acts as a virtual analyst that runs queries over Fabric data and
# returns answers, tables, and charts. ACL is enforced natively by Fabric
# via the forwarded per-user OBO token (same x-ms-query-source-authorization
# header used by Work IQ / Fabric ontology), so no filterAddOn is applied.
# OBO is required; MI fallback is never used for remote kinds.
FABRIC_DATA_AGENT_ENABLED_KEY = "FABRIC_DATA_AGENT_ENABLED"
FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME_KEY = "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME"

# SharePoint remote (Microsoft 365 Copilot Retrieval API remote knowledge
# source). Opt-in; default off. When enabled, the retrieve request appends a
# remoteSharePoint knowledge source in knowledgeSourceParams. SharePoint
# item-level permissions are enforced natively by M365 via the forwarded
# per-user OBO token (same x-ms-query-source-authorization header used by
# Work IQ / Fabric ontology), so no filterAddOn is applied. OBO is required;
# MI fallback is never used for remote kinds. An optional retrieval-time KQL
# scope can be forwarded via filterExpressionAddOn.
SHAREPOINT_REMOTE_ENABLED_KEY = "SHAREPOINT_REMOTE_ENABLED"
SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME_KEY = "SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME"
SHAREPOINT_REMOTE_FILTER_EXPRESSION_ADD_ON_KEY = (
    "SHAREPOINT_REMOTE_FILTER_EXPRESSION_ADD_ON"
)

# OneLake indexed knowledge source (Microsoft Fabric OneLake, indexed variant).
# Opt-in; default off. When enabled, the retrieve request appends an
# ``indexedOneLake`` knowledge source in ``knowledgeSourceParams``. Unlike
# the remote kinds (workIQ / fabricOntology / fabricDataAgent), Foundry IQ
# manages the underlying Azure AI Search index internally: at KS registration
# time it is bound to a Fabric workspace + lakehouse and provisions the
# datasource, skillset, index, and indexer transparently. Agent Landing Zone therefore
# does not maintain a Bicep AI Search sidecar for OneLake; the KS itself
# owns the pipeline. The retrieve-time entry only needs the knowledge source
# name and reference flags; workspace / lakehouse identifiers are used only
# by platform provisioning (postProvision RBAC assignment for the Foundry
# IQ managed identity on the target Fabric workspace) and by the operator
# docs. Because content is stored in AI Search, ``indexedOneLake`` is a
# native (not remote) kind: no OBO requirement and no maxRuntimeInSeconds
# bump. Permission filters are evaluated only with a per-user OBO token;
# the service identity is never forwarded as query source authorization.
ONELAKE_KS_ENABLED_KEY = "ONELAKE_KS_ENABLED"
ONELAKE_KNOWLEDGE_SOURCE_NAME_KEY = "ONELAKE_KNOWLEDGE_SOURCE_NAME"
ONELAKE_WORKSPACE_ID_KEY = "ONELAKE_WORKSPACE_ID"
ONELAKE_LAKEHOUSE_ID_KEY = "ONELAKE_LAKEHOUSE_ID"

# SharePoint Indexed knowledge source. Opt-in; default off. Unlike the remote
# workIQ / fabric* kinds, indexedSharePoint is a server-side knowledge source
# registered on the AI Search service. The knowledge source itself owns the
# ingestion pipeline (datasource + indexer + skillset + index generated by
# AI Search from the KS definition) and reads a pre-built local index at
# retrieve time. That means:
#
# - kind is "indexedSharePoint" (verified against MS Learn agentic-knowledge-
#   source-how-to-sharepoint-indexed; the SharePoint indexer preview API is
#   2026-05-01-preview which is already our DEFAULT_FOUNDRY_IQ_API_VERSION).
# - No OBO forwarding is required at retrieve time. Auth is baked into the
#   knowledge source connectionString via a Federated Identity Credential
#   (MI + Graph Sites.Selected app-only permission) at KS registration.
# - No maxRuntimeInSeconds bump is needed. Retrieve reads a local index and
#   is fast; only the remote kinds (workIQ, fabricOntology, fabricDataAgent)
#   need the extended runtime ceiling.
# - ACL trimming and per-user site-group enforcement are out of scope for
#   this preview; document-level security stays app-only.
SHAREPOINT_INDEXED_ENABLED_KEY = "SHAREPOINT_INDEXED_ENABLED"
SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME_KEY = "SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME"
SHAREPOINT_INDEXED_INDEX_NAME_KEY = "SHAREPOINT_INDEXED_INDEX_NAME"
SHAREPOINT_INDEXED_SITE_URL_KEY = "SHAREPOINT_INDEXED_SITE_URL"
SHAREPOINT_INDEXED_TENANT_ID_KEY = "SHAREPOINT_INDEXED_TENANT_ID"

# Web grounding (Grounding with Bing) knowledge source. Opt-in; default off.
# When enabled, the retrieve request appends a ``web`` knowledge source in
# knowledgeSourceParams. Web grounding calls Bing Search over the public
# internet: there is no per-user ACL to enforce, no OBO impersonation, and
# no filterAddOn. Optional allow / block domain lists narrow the crawl at
# request time. The knowledge source itself is registered on the knowledge
# base (kind ``web``, webParameters.domains) via the platform's post-provision
# script; the retrieve entry only names it.
WEB_GROUNDING_ENABLED_KEY = "WEB_GROUNDING_ENABLED"
WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME_KEY = "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME"
WEB_GROUNDING_ALLOWED_DOMAINS_KEY = "WEB_GROUNDING_ALLOWED_DOMAINS"
WEB_GROUNDING_BLOCKED_DOMAINS_KEY = "WEB_GROUNDING_BLOCKED_DOMAINS"

# Knowledge source kinds that must never fall back to the service managed
# identity for x-ms-query-source-authorization. These sources run against
# external systems (M365, Fabric) where impersonation, not app identity, is
# the security boundary. ``web`` is deliberately excluded: Grounding with
# Bing hits the public internet and has no per-user ACL to enforce, so no
# authorization header is added for that source.
_REMOTE_KNOWLEDGE_SOURCE_KINDS = frozenset(
    {"workIQ", "fabricOntology", "fabricDataAgent", "remoteSharePoint"}
)

# Search-audience scope used for the service token.
_SEARCH_SCOPE = "https://search.azure.com/.default"


class McpSourceError(RuntimeError):
    """Raised when a required MCP source fails during Foundry IQ retrieval."""


def _odata_escape_string(value: Optional[str]) -> str:
    """Escape a string for embedding in a single-quoted OData literal."""
    return (value or "").replace("'", "''")


_ANONYMOUS_PRINCIPAL_ID = "anonymous"
_CONVERSATION_OWNER_FIELD = "metadata_security_user_ids"


def _resolve_conversation_owner_id(user_context: Any) -> Optional[str]:
    """Return the server-derived principal that owns conversation uploads."""
    if not isinstance(user_context, Mapping):
        return None
    for key in ("principal_id", "oid"):
        value = str(user_context.get(key) or "").strip()
        if value:
            return value
    return None


def _build_conversation_owner_clause(
    conversation_id: Optional[str], owner_id: Optional[str]
) -> Optional[str]:
    """Build the clause matching conversation uploads stamped for ``owner_id``."""
    cid = (conversation_id or "").strip()
    owner = (owner_id or "").strip()
    if not cid or not owner:
        return None
    if owner == _ANONYMOUS_PRINCIPAL_ID:
        owner_clause = f"not {_CONVERSATION_OWNER_FIELD}/any()"
    else:
        owner_clause = (
            f"{_CONVERSATION_OWNER_FIELD}/any(u: u eq '{_odata_escape_string(owner)}')"
        )
    return f"(conversationId eq '{_odata_escape_string(cid)}' and {owner_clause})"


def _build_conversation_filter(
    conversation_id: Optional[str], owner_id: Optional[str]
) -> str:
    """Owner-scoped conversation uploads plus the shared corpus."""
    shared_clause = "(conversationId eq 'NaN' or conversationId eq null)"
    owned_clause = _build_conversation_owner_clause(conversation_id, owner_id)
    if owned_clause:
        return f"{owned_clause} or {shared_clause}"
    return shared_clause


def _normalize_security_ids(values: Iterable[Any]) -> List[str]:
    """Return stable, non-empty security IDs without broadening the filter."""
    normalized: List[str] = []
    seen = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text == "anonymous":
            continue
        if text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_domain_list(value: Any) -> List[str]:
    """Parse a comma- or newline-separated domain list from config.

    Accepts a list/tuple already, or a delimited string. Whitespace is
    trimmed, empties dropped, order preserved, duplicates removed.
    """
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        text = str(value)
        # Accept both commas and newlines so the same value shape works
        # in App Config, .env files, and pipeline overrides.
        for sep in ("\n", ";"):
            text = text.replace(sep, ",")
        items = text.split(",")

    seen = set()
    normalized: List[str] = []
    for item in items:
        cleaned = item.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def build_pattern_b_filter_add_on(
    *,
    conversation_id: Optional[str],
    user_context: Optional[Mapping[str, Any]],
    security_field_name: str = "metadata_security_id",
) -> str:
    """Build the Pattern B ``filterAddOn`` for Agent Landing Zone security fields.

    Pattern B registers Agent Landing Zone's existing Azure AI Search index as a Foundry IQ
    ``searchIndex`` knowledge source. The custom security fields on that index
    are not enforced by ``x-ms-query-source-authorization``. They must be
    expressed as an OData ``filterAddOn`` on the retrieve request. The filter
    preserves the direct AI Search behavior: include documents that match one of
    the caller security IDs, plus public documents where the security collection
    is empty.
    """
    safe_field = (security_field_name or "").strip() or "metadata_security_id"
    context = dict(user_context or {})

    candidate_ids: List[Any] = [
        context.get("principal_id"),
        context.get("oid"),
        context.get("user_id"),
    ]
    principal_name = context.get("principal_name") or context.get("user_name")
    if principal_name:
        candidate_ids.append(principal_name)

    for key in ("security_ids", "groups", "group_ids", "client_group_names"):
        value = context.get(key)
        if isinstance(value, (list, tuple, set)):
            candidate_ids.extend(value)
        elif value:
            candidate_ids.append(value)

    security_ids = _normalize_security_ids(candidate_ids)
    public_clause = f"not {safe_field}/any()"
    if not security_ids:
        security_clause = public_clause
    else:
        escaped_ids = ",".join(_odata_escape_string(value) for value in security_ids)
        security_clause = f"({safe_field}/any(g:search.in(g, '{escaped_ids}')) or {public_clause})"

    # Keep runtime uploads conversation- and owner-scoped while allowing the
    # shared corpus. The conversation id is client-chosen, so it must be paired
    # with the server-derived uploader identity.
    conversation_clause = None
    cid = (conversation_id or "").strip() or None
    if cid:
        conversation_clause = (
            "("
            + _build_conversation_filter(
                cid, _resolve_conversation_owner_id(context)
            )
            + ")"
        )

    if conversation_clause:
        return f"({security_clause}) and {conversation_clause}"
    return security_clause


def build_conversation_upload_filter_add_on(
    conversation_id: Optional[str], owner_id: Optional[str]
) -> Optional[str]:
    """Build the Foundry IQ sidecar filter for runtime conversation uploads.

    Returns ``None`` when the conversation id or the server-derived owner is
    missing; callers must then skip the sidecar instead of widening it. The
    clause is emitted without outer parentheses to stay as close as possible to
    the simple ``conversationId eq`` form Foundry IQ accepts for this source.
    """
    clause = _build_conversation_owner_clause(conversation_id, owner_id)
    if clause is None:
        return None
    return clause[1:-1]


class FoundryIQClient:
    """Client for the Foundry IQ knowledge base retrieve action."""

    def __init__(self) -> None:
        self.cfg = get_config()
        # The knowledge base lives on the search service, so the endpoint
        # defaults to the search query endpoint when KNOWLEDGE_BASE_ENDPOINT is
        # not explicitly stamped.
        self.endpoint = (
            self.cfg.get("KNOWLEDGE_BASE_ENDPOINT")
            or self.cfg.get("SEARCH_SERVICE_QUERY_ENDPOINT")
        )
        self.knowledge_base_name = self.cfg.get(KNOWLEDGE_BASE_NAME_KEY)
        self.api_version = self.cfg.get(
            FOUNDRY_IQ_API_VERSION_KEY, DEFAULT_FOUNDRY_IQ_API_VERSION
        )
        self.knowledge_source_name = (
            self.cfg.get(FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        self.knowledge_source_kind = (
            self.cfg.get(FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND_KEY, "")
            or self.cfg.get(FOUNDRY_IQ_PATTERN_KEY, "")
            or ""
        ).strip()
        self.filter_add_on_enabled = _as_bool(
            self.cfg.get(FOUNDRY_IQ_FILTER_ADD_ON_ENABLED_KEY, False, type=bool)
        )
        if not self.knowledge_source_kind:
            self.knowledge_source_kind = "searchIndex" if self.filter_add_on_enabled else "azureBlob"
        if self.knowledge_source_kind == "managed":
            self.knowledge_source_kind = "azureBlob"
        self.security_field_name = self.cfg.get(
            FOUNDRY_IQ_SECURITY_FIELD_NAME_KEY, "metadata_security_id", type=str
        )
        self.max_output_documents = _as_optional_int(
            self.cfg.get(FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS_KEY, None)
        )
        # Deprecated and ignored: the service managed-identity token is never
        # forwarded as ``x-ms-query-source-authorization``. Only a per-user OBO
        # token is forwarded, so permission-trimmed sources fail closed for
        # anonymous requests instead of returning service-identity results.
        self.forward_source_auth = False
        if _as_bool(self.cfg.get(FOUNDRY_IQ_FORWARD_SOURCE_AUTH_KEY, False, type=bool)):
            logging.warning(
                "[FoundryIQClient] FOUNDRY_IQ_FORWARD_SOURCE_AUTH is deprecated "
                "and ignored; the service identity is never forwarded as query "
                "source authorization"
            )
        # Hybrid file-upload sidecar. Only meaningful for Pattern A (azureBlob
        # primary); Pattern B already carries a conversationId filterAddOn on its
        # single searchIndex source, so it needs no second source.
        self.conversation_upload_enabled = _as_bool(
            self.cfg.get(FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED_KEY, False, type=bool)
        )
        self.conversation_knowledge_source_name = (
            self.cfg.get(FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        # Retrieve-runtime ceiling for remote knowledge source kinds.
        raw_max_runtime_seconds = self.cfg.get(
            FOUNDRY_IQ_MAX_RUNTIME_SECONDS_KEY,
            DEFAULT_FOUNDRY_IQ_MAX_RUNTIME_SECONDS,
        )
        self.max_runtime_seconds = (
            _as_optional_int(raw_max_runtime_seconds)
            or DEFAULT_FOUNDRY_IQ_MAX_RUNTIME_SECONDS
        )
        mcp_enabled = _as_bool(
            self.cfg.get(FOUNDRY_IQ_MCP_ENABLED_KEY, False, type=bool)
        )
        self.mcp_config = McpRuntimeConfig.parse(
            enabled=mcp_enabled,
            sources_json=self.cfg.get(FOUNDRY_IQ_MCP_SOURCES_JSON_KEY, "[]"),
            reasoning_effort=self.cfg.get(
                FOUNDRY_IQ_MCP_REASONING_EFFORT_KEY, "low"
            ),
            trusted_hosts=self.cfg.get(FOUNDRY_IQ_MCP_TRUSTED_HOSTS_KEY, ""),
            log_tool_arguments=_as_bool(
                self.cfg.get(
                    FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS_KEY, False, type=bool
                )
            ),
            api_version=self.api_version,
            max_runtime_seconds=raw_max_runtime_seconds,
        )
        # Work IQ (Microsoft 365) remote knowledge source. Opt-in; requires
        # a per-user OBO token - anonymous / MI fallback is never used.
        self.work_iq_enabled = _as_bool(
            self.cfg.get(WORK_IQ_ENABLED_KEY, False, type=bool)
        )
        self.work_iq_knowledge_source_name = (
            self.cfg.get(WORK_IQ_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        # Fabric IQ (Microsoft Fabric ontology) remote knowledge source.
        # Opt-in; requires a per-user OBO token (same
        # x-ms-query-source-authorization header used by Work IQ), so
        # anonymous / MI fallback is never used.
        self.fabric_iq_enabled = _as_bool(
            self.cfg.get(FABRIC_IQ_ENABLED_KEY, False, type=bool)
        )
        self.fabric_iq_knowledge_source_name = (
            self.cfg.get(FABRIC_IQ_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        # Fabric Data Agent remote knowledge source. Opt-in; requires a
        # per-user OBO token (same x-ms-query-source-authorization header
        # used by Work IQ / Fabric ontology), so anonymous / MI fallback is
        # never used.
        self.fabric_data_agent_enabled = _as_bool(
            self.cfg.get(FABRIC_DATA_AGENT_ENABLED_KEY, False, type=bool)
        )
        self.fabric_data_agent_knowledge_source_name = (
            self.cfg.get(FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        # SharePoint remote knowledge source (Copilot Retrieval API). Opt-in;
        # requires a per-user OBO token (same x-ms-query-source-authorization
        # header used by Work IQ / Fabric ontology), so anonymous / MI
        # fallback is never used.
        self.sharepoint_remote_enabled = _as_bool(
            self.cfg.get(SHAREPOINT_REMOTE_ENABLED_KEY, False, type=bool)
        )
        self.sharepoint_remote_knowledge_source_name = (
            self.cfg.get(SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        self.sharepoint_remote_filter_expression_add_on = (
            self.cfg.get(SHAREPOINT_REMOTE_FILTER_EXPRESSION_ADD_ON_KEY, "") or ""
        ).strip()
        # OneLake indexed knowledge source. Native kind (Foundry IQ owns the
        # underlying AI Search index), so no OBO is required. Workspace and
        # lakehouse identifiers are read here for observability / logging
        # only; the retrieve-time entry carries just the KS name.
        self.onelake_ks_enabled = _as_bool(
            self.cfg.get(ONELAKE_KS_ENABLED_KEY, False, type=bool)
        )
        self.onelake_knowledge_source_name = (
            self.cfg.get(ONELAKE_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        self.onelake_workspace_id = (
            self.cfg.get(ONELAKE_WORKSPACE_ID_KEY, "") or ""
        ).strip()
        self.onelake_lakehouse_id = (
            self.cfg.get(ONELAKE_LAKEHOUSE_ID_KEY, "") or ""
        ).strip()
        # SharePoint Indexed knowledge source. Opt-in; server-side KS on the
        # AI Search service, so no OBO is required at retrieve time. The five
        # config keys are read here so downstream helpers can inspect them
        # without re-fetching from config. The index/site/tenant hints are
        # captured for observability and setup scripts; the retrieve call only
        # needs the KS name (auth is baked into the KS connectionString).
        self.sharepoint_indexed_enabled = _as_bool(
            self.cfg.get(SHAREPOINT_INDEXED_ENABLED_KEY, False, type=bool)
        )
        self.sharepoint_indexed_knowledge_source_name = (
            self.cfg.get(SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        self.sharepoint_indexed_index_name = (
            self.cfg.get(SHAREPOINT_INDEXED_INDEX_NAME_KEY, "") or ""
        ).strip()
        self.sharepoint_indexed_site_url = (
            self.cfg.get(SHAREPOINT_INDEXED_SITE_URL_KEY, "") or ""
        ).strip()
        self.sharepoint_indexed_tenant_id = (
            self.cfg.get(SHAREPOINT_INDEXED_TENANT_ID_KEY, "") or ""
        ).strip()
        # Web grounding (Grounding with Bing) knowledge source. Opt-in.
        # Public data, no ACL, no OBO - domains lists are the only trimming.
        self.web_grounding_enabled = _as_bool(
            self.cfg.get(WEB_GROUNDING_ENABLED_KEY, False, type=bool)
        )
        self.web_grounding_knowledge_source_name = (
            self.cfg.get(WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME_KEY, "") or ""
        ).strip()
        self.web_grounding_allowed_domains = _parse_domain_list(
            self.cfg.get(WEB_GROUNDING_ALLOWED_DOMAINS_KEY, "")
        )
        self.web_grounding_blocked_domains = _parse_domain_list(
            self.cfg.get(WEB_GROUNDING_BLOCKED_DOMAINS_KEY, "")
        )
        self.credential = self.cfg.aiocredential

        # Shared aiohttp session - reuses TCP connections across calls.
        self._session: Optional[aiohttp.ClientSession] = None

        if not self.endpoint:
            raise ValueError(
                "Neither KNOWLEDGE_BASE_ENDPOINT nor SEARCH_SERVICE_QUERY_ENDPOINT "
                "is set in config; cannot reach the Foundry IQ knowledge base."
            )
        if not self.knowledge_base_name:
            raise ValueError(
                "KNOWLEDGE_BASE_NAME is not set in config; cannot target a "
                "Foundry IQ knowledge base."
            )

        logging.info(
            "[FoundryIQClient] ✅ Initialized (knowledge_base=%s, api_version=%s)",
            self.knowledge_base_name,
            self.api_version,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return a shared aiohttp session, creating one lazily if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _retrieve_url(self) -> str:
        return (
            f"{self.endpoint}"
            f"/knowledgebases/{self.knowledge_base_name}/retrieve"
            f"?api-version={self.api_version}"
        )

    @staticmethod
    def _normalize_work_iq_reference(source: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Map a Work IQ ``sourceData`` payload into ``{title, link, content}``.

        Work IQ references do not use the flat ``snippet`` / ``blob_url`` shape.
        Instead they carry:

        - ``attributions[]`` - one or more entries per reference. Each carries
          a ``seeMoreWebUrl`` (Outlook / SharePoint / Teams deep link) and,
          when available, a subject / filename we can use as the citation
          title.
        - ``extracts[]`` - extracted chunks with a ``text`` field. Concatenating
          the extracts gives the grounding content the LLM sees.

        Returns ``None`` when the payload has neither extracts nor attributions
        we can use, so the caller can skip empty Work IQ hits without falling
        through to the generic azureBlob / searchIndex path (which would
        otherwise emit a ``reference`` title and no link).
        """
        attributions = source.get("attributions") or []
        extracts = source.get("extracts") or []
        if not attributions and not extracts:
            return None

        extract_texts: List[str] = []
        for extract in extracts:
            if isinstance(extract, Mapping):
                text = extract.get("text")
                if text:
                    extract_texts.append(str(text))
        content = "\n\n".join(extract_texts).strip()
        if not content:
            return None

        link = ""
        title: Optional[str] = None
        for attribution in attributions:
            if not isinstance(attribution, Mapping):
                continue
            if not link:
                link = str(attribution.get("seeMoreWebUrl") or "")
            if not title:
                # Prefer human-friendly labels, fall back to filename.
                for key in ("subject", "name", "title", "filename", "fileName"):
                    value = attribution.get(key)
                    if value:
                        title = str(value)
                        break
            if link and title:
                break

        if not title and link:
            tail = link.rsplit("/", 1)[-1]
            title = tail.split("?", 1)[0] or None
        if not title:
            title = "reference"

        return {"title": title, "link": link, "content": content}

    @staticmethod
    def _normalize_fabric_iq_reference(source: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Map a Fabric IQ ``sourceData`` payload into ``{title, link, content}``.

        Fabric IQ references carry a different shape from Work IQ:

        - ``fabricAnswer`` (optional) is a natural-language summary produced by
          the Fabric ontology retriever.
        - ``fabricRawData`` (optional) is the raw grounding evidence, typically
          CSV rows the LLM should be able to quote from verbatim.
        - ``workspaceId`` / ``ontologyId`` identify the Fabric artifact used.

        Both ``fabricAnswer`` and ``fabricRawData`` may be present; when both
        exist we concatenate them so the LLM sees the summary plus the raw
        rows. Returns ``None`` when neither field is present so the caller can
        skip empty hits rather than emit a bare ``reference`` title.

        Fabric IQ does not currently return a per-reference deep link, so
        ``link`` is left empty and the citation title falls back to the
        workspace / ontology identifiers.
        """
        answer = source.get("fabricAnswer")
        raw = source.get("fabricRawData")
        parts: List[str] = []
        if isinstance(answer, str) and answer.strip():
            parts.append(answer.strip())
        if isinstance(raw, str) and raw.strip():
            parts.append(raw.strip())
        if not parts:
            return None
        content = "\n\n".join(parts)

        workspace_id = str(source.get("workspaceId") or "").strip()
        ontology_id = str(source.get("ontologyId") or "").strip()
        if workspace_id and ontology_id:
            title = f"Fabric ontology {ontology_id} (workspace {workspace_id})"
        elif ontology_id:
            title = f"Fabric ontology {ontology_id}"
        elif workspace_id:
            title = f"Fabric workspace {workspace_id}"
        else:
            title = "Fabric ontology"

        return {"title": title, "link": "", "content": content}

    @staticmethod
    def _normalize_fabric_data_agent_reference(
        source: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        """Map a Fabric Data Agent ``sourceData`` payload into ``{title, link, content}``.

        A Fabric Data Agent acts as a virtual analyst: it runs queries over
        Fabric data and returns answers, and (optionally) tabular / chart
        artifacts. The retrieve response typically exposes:

        - ``dataAgentAnswer`` (optional) - natural-language answer.
        - ``dataAgentRawData`` (optional) - raw grounding evidence, usually
          CSV / JSON rows the LLM should be able to quote from verbatim.
        - ``workspaceId`` / ``dataAgentId`` - identify the Fabric artifacts
          the agent ran against.

        Both ``dataAgentAnswer`` and ``dataAgentRawData`` may be present;
        when both exist we concatenate them so the LLM sees the summary plus
        the raw rows. Returns ``None`` when neither field is present so the
        caller can skip empty hits rather than emit a bare ``reference``
        title. Fabric Data Agent does not currently return a per-reference
        deep link, so ``link`` is left empty and the citation title falls
        back to the workspace / data agent identifiers.
        """
        answer = source.get("dataAgentAnswer")
        raw = source.get("dataAgentRawData")
        parts: List[str] = []
        if isinstance(answer, str) and answer.strip():
            parts.append(answer.strip())
        if isinstance(raw, str) and raw.strip():
            parts.append(raw.strip())
        if not parts:
            return None
        content = "\n\n".join(parts)

        workspace_id = str(source.get("workspaceId") or "").strip()
        data_agent_id = str(source.get("dataAgentId") or "").strip()
        if workspace_id and data_agent_id:
            title = f"Fabric data agent {data_agent_id} (workspace {workspace_id})"
        elif data_agent_id:
            title = f"Fabric data agent {data_agent_id}"
        elif workspace_id:
            title = f"Fabric workspace {workspace_id}"
        else:
            title = "Fabric data agent"

        return {"title": title, "link": "", "content": content}

    @staticmethod
    def _normalize_sharepoint_remote_reference(
        source: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        """Map a remoteSharePoint ``sourceData`` payload into ``{title, link, content}``.

        SharePoint remote references come from the Copilot Retrieval API. A
        reference typically carries:

        - ``webUrl`` - deep link to the SharePoint item (file, page, list
          item), the natural value for the citation ``link``.
        - ``resourceMetadata`` - object with SharePoint properties such as
          ``Title`` / ``Name`` / ``FileName`` / ``LastModifiedTime``.
        - ``extracts`` - list of ``{text}`` snippets (same shape used by
          Work IQ) and/or a flat ``text`` / ``content`` / ``snippet`` field
          when the API returns pre-joined text.

        Returns ``None`` when neither snippet content nor extracts can be
        recovered so the caller can skip empty hits rather than emit a bare
        ``reference`` title.
        """
        parts: List[str] = []
        for extract in source.get("extracts") or []:
            if not isinstance(extract, Mapping):
                continue
            text = extract.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if not parts:
            for key in ("text", "content", "snippet"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                    break
        if not parts:
            return None
        content = "\n\n".join(parts)

        link = ""
        web_url = source.get("webUrl")
        if isinstance(web_url, str):
            link = web_url.strip()

        title = ""
        metadata = source.get("resourceMetadata")
        if isinstance(metadata, Mapping):
            for key in ("Title", "title", "Name", "name", "FileName", "fileName"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break
        if not title and link:
            tail = link.rsplit("/", 1)[-1]
            title = tail.split("?", 1)[0]
        if not title:
            title = "SharePoint item"

        return {"title": title, "link": link, "content": content}

    @staticmethod
    def _normalize_onelake_reference(source: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Map an ``indexedOneLake`` ``sourceData`` payload into ``{title, link, content}``.

        Because Foundry IQ manages the underlying Azure AI Search index for
        OneLake internally, the reference shape is projection-driven: it
        looks like a native ``azureBlob`` chunk (a snippet plus a file
        pointer) rather than the ``attributions`` / ``extracts`` shape of
        remote M365 / Fabric kinds. The exact field names are not fully
        pinned down in the preview API surface, so the normalizer accepts
        the plausible variants (``snippet`` / ``content`` / ``text`` for
        the chunk; ``oneLakeFilePath`` / ``filePath`` / ``path`` /
        ``abfssPath`` / ``webUrl`` for the pointer) and annotates the
        citation title with the workspace / lakehouse identifiers when
        they are present. Returns ``None`` when no textual content can be
        found so the caller can skip empty hits.
        """
        content = (
            source.get("snippet")
            or source.get("content")
            or source.get("text")
            or ""
        )
        if not content:
            return None

        link = (
            source.get("webUrl")
            or source.get("oneLakeFilePath")
            or source.get("filePath")
            or source.get("path")
            or source.get("abfssPath")
            or ""
        )

        title = source.get("title") or source.get("fileName") or source.get("name")
        if not title and link:
            tail = link.rsplit("/", 1)[-1]
            title = tail.split("?", 1)[0] or None
        if not title:
            title = "OneLake reference"

        workspace_id = str(source.get("workspaceId") or "").strip()
        lakehouse_id = str(source.get("lakehouseId") or "").strip()
        if workspace_id and lakehouse_id:
            title = f"{title} (workspace {workspace_id}, lakehouse {lakehouse_id})"
        elif workspace_id:
            title = f"{title} (workspace {workspace_id})"
        elif lakehouse_id:
            title = f"{title} (lakehouse {lakehouse_id})"

        return {"title": title, "link": link, "content": content}

    @staticmethod
    def _normalize_sharepoint_indexed_reference(
        source: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        """Map an ``indexedSharePoint`` ``sourceData`` payload into ``{title, link, content}``.

        ``indexedSharePoint`` is a server-side knowledge source: AI Search
        auto-generates a datasource / indexer / skillset / index from the KS
        definition and retrieves against that local index. The exact
        ``sourceData`` shape is not documented publicly for this preview
        (2026-05-01-preview), so we extract defensively:

        - ``content`` prefers the standard AI Search text projections
          (``content``, ``snippet``, ``text``) but also accepts a
          SharePoint-oriented body/summary field when the KS pipeline
          projects one.
        - ``link`` prefers a SharePoint deep link (``webUrl`` / ``webPath`` /
          ``siteUrl``) so citations open in SharePoint, and only falls back
          to the blob/index url when no SharePoint URL is present.
        - ``title`` prefers a human-readable label (``title`` / ``name`` /
          ``fileName``) and falls back to the SharePoint link tail so
          citations don't surface an opaque ``driveItemId``.

        Returns ``None`` when the payload has no usable content so the caller
        can skip empty hits rather than emit a bare ``reference`` title. The
        detection in :meth:`_normalize_references` only routes here when at
        least one SharePoint-oriented field is present, so falling through
        to the generic path remains an option for genuinely generic shapes.
        """
        content = (
            source.get("content")
            or source.get("snippet")
            or source.get("text")
            or source.get("body")
            or source.get("summary")
            or ""
        )
        if not content:
            return None

        # Prefer SharePoint-shaped deep links so citations open in SharePoint
        # instead of surfacing the underlying AI Search blob URL. Both the
        # camelCase and lowerCase spellings are accepted defensively since
        # the exact projection is not documented.
        link = (
            source.get("webUrl")
            or source.get("web_url")
            or source.get("webPath")
            or source.get("siteUrl")
            or source.get("site_url")
            or source.get("blob_url")
            or source.get("url")
            or source.get("filepath")
            or source.get("path")
            or ""
        )

        title = (
            source.get("title")
            or source.get("name")
            or source.get("fileName")
            or source.get("filename")
            or source.get("driveItemId")
            or source.get("drive_item_id")
            or source.get("id")
        )
        if not title and link:
            tail = str(link).rsplit("/", 1)[-1]
            title = tail.split("?", 1)[0] or None
        if not title:
            title = "SharePoint reference"

        return {"title": str(title), "link": str(link), "content": str(content)}

    @staticmethod
    def _normalize_web_reference(source: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Map a web (Grounding with Bing) ``sourceData`` payload into
        ``{title, link, content}``.

        The public API preview for kind ``web`` does not document a fixed
        reference shape end-to-end, so this normalizer follows the generic
        Bing-style contract used across Microsoft grounding surfaces:

        - ``title`` (page title) - preferred.
        - ``url`` (canonical page URL) - preferred link.
        - ``snippet`` (extract shown to the user) - preferred content.

        Common alternates such as ``name`` (Bing web results) and
        ``description`` are accepted as graceful fallbacks so a small server
        rename does not silently drop citations. Returns ``None`` when
        neither a snippet nor a URL is available, so the caller can skip
        empty hits rather than emit a bare ``reference`` title.
        """
        snippet = (
            source.get("snippet")
            or source.get("description")
            or source.get("content")
            or ""
        )
        url = (
            source.get("url")
            or source.get("link")
            or source.get("displayUrl")
            or ""
        )
        title = (
            source.get("title")
            or source.get("name")
            or ""
        )

        snippet_text = snippet.strip() if isinstance(snippet, str) else ""
        url_text = url.strip() if isinstance(url, str) else ""
        title_text = title.strip() if isinstance(title, str) else ""

        if not snippet_text and not url_text:
            return None

        if not title_text and url_text:
            # Derive a readable title from the URL host + path tail so the
            # citation surface is not blank when the server omits ``title``.
            tail = url_text.rsplit("/", 1)[-1].split("?", 1)[0]
            title_text = tail or url_text
        if not title_text:
            title_text = "web result"

        return {"title": title_text, "link": url_text, "content": snippet_text}

    @staticmethod
    def _normalize_mcp_reference(ref: Mapping[str, Any]) -> Optional[Dict[str, str]]:
        """Map an MCP reference into the shared citation contract."""
        source_data = ref.get("sourceData")
        source = source_data if isinstance(source_data, Mapping) else {}

        title = source.get("title") or source.get("name") or ref.get("title")
        if not title:
            title = ref.get("toolName") or "MCP reference"
        link = (
            source.get("url")
            or source.get("uri")
            or source.get("link")
            or source.get("webUrl")
            or ""
        )
        content_value = (
            source.get("content")
            or source.get("text")
            or source.get("snippet")
            or source.get("description")
        )
        if content_value is None and source_data not in (None, "", {}, []):
            content_value = source_data

        if isinstance(content_value, str):
            content = content_value.strip()
        elif content_value is None:
            content = ""
        else:
            try:
                content = json.dumps(
                    content_value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    default=str,
                )
            except (TypeError, ValueError):
                content = str(content_value)
        content = content[:4000]

        title_text = str(title).strip() if title is not None else ""
        link_text = str(link).strip() if link is not None else ""
        if not content and not link_text:
            return None
        return {
            "title": title_text or "MCP reference",
            "link": link_text,
            "content": content,
        }

    @classmethod
    def _normalize_references(cls, payload: Dict[str, Any]) -> List[Dict[str, str]]:
        """Map a retrieve response into ``[{title, link, content}]`` records.

        The knowledge base retrieve response carries a ``references`` array;
        each reference exposes a ``sourceData`` object with the configured
        source fields. Field names vary by knowledge source kind and content
        extraction mode:

        - ``azureBlob`` native sources (both ``minimal`` and ``standard``
          content extraction) return ``sourceData.snippet`` and
          ``sourceData.blob_url`` and do not include ``title``.
        - ``searchIndex`` Pattern B sources return whatever the index
          projects (typically ``content``, ``filepath``/``url``, ``title``).
        - ``workIQ`` (Microsoft 365) sources return ``sourceData.attributions``
          and ``sourceData.extracts`` instead of a flat snippet field. See
          :meth:`_normalize_work_iq_reference`.
        - ``fabricOntology`` (Microsoft Fabric) sources return
          ``sourceData.fabricAnswer`` and/or ``sourceData.fabricRawData``
          alongside ``workspaceId`` / ``ontologyId``. See
          :meth:`_normalize_fabric_iq_reference`.
        - ``fabricDataAgent`` (Microsoft Fabric Data Agent) sources return
          ``sourceData.dataAgentAnswer`` and/or ``sourceData.dataAgentRawData``
          alongside ``workspaceId`` / ``dataAgentId``. See
          :meth:`_normalize_fabric_data_agent_reference`.
        - ``remoteSharePoint`` (Microsoft 365 Copilot Retrieval API) sources
          return ``sourceData.webUrl`` and ``sourceData.resourceMetadata``
          with SharePoint properties (Title, LastModifiedTime, ...) plus
          ``extracts`` snippets. See :meth:`_normalize_sharepoint_remote_reference`.
        - ``indexedOneLake`` (Microsoft Fabric OneLake, indexed) sources
          return an azureBlob-shaped projection: a chunk snippet plus a
          OneLake file pointer, optionally annotated with
          ``workspaceId`` / ``lakehouseId``. See
          :meth:`_normalize_onelake_reference`.
        - ``web`` (Grounding with Bing) sources return generic
          ``sourceData.title`` / ``sourceData.url`` / ``sourceData.snippet``
          fields matching the public Bing-style contract. See
          :meth:`_normalize_web_reference`.

        We accept the union with explicit priority so the downstream
        ``{title, link, content}`` contract is identical across backends,
        and so a future small rename does not silently drop references.
        Title falls back to the link's filename when the source does not
        carry one (true for ``azureBlob`` references).
        """
        records: List[Dict[str, str]] = []

        def append_record(
            record: Optional[Dict[str, str]],
            source_type: str,
        ) -> None:
            if record is not None:
                tagged_record = _FoundryIQRecord(record)
                tagged_record.source_type = source_type
                records.append(tagged_record)

        for ref in payload.get("references", []) or []:
            source = ref.get("sourceData") or {}
            ref_type = str(ref.get("type") or "").strip().lower()

            if ref_type == "mcpserver":
                mcp_record = cls._normalize_mcp_reference(ref)
                append_record(mcp_record, "mcpServer")
                continue

            if not isinstance(source, Mapping):
                continue

            # Web grounding shape (kind ``web``): dispatched by the top-level
            # reference ``type`` marker since sourceData.url / snippet overlap
            # with the generic azureBlob / searchIndex path below.
            if ref_type == "web":
                web_record = cls._normalize_web_reference(source)
                append_record(web_record, "web")
                continue

            # Fabric Data Agent shape (fabricDataAgent): distinct fields,
            # no overlap with fabricOntology / workIQ.
            if source.get("dataAgentAnswer") or source.get("dataAgentRawData"):
                data_agent_record = cls._normalize_fabric_data_agent_reference(source)
                append_record(data_agent_record, "fabricDataAgent")
                continue

            # SharePoint remote shape (remoteSharePoint / Copilot Retrieval
            # API): keyed on ``resourceMetadata``, the marker unique to the
            # Copilot Retrieval API response envelope (Work IQ shares
            # ``extracts`` but never carries ``resourceMetadata``, and a
            # bare ``webUrl`` alone is ambiguous with SharePoint Indexed).
            if source.get("resourceMetadata"):
                sp_record = cls._normalize_sharepoint_remote_reference(source)
                append_record(sp_record, "remoteSharePoint")
                continue

            # Fabric IQ shape (fabricOntology): distinct fields, no
            # attributions/extracts overlap with Work IQ.
            if source.get("fabricAnswer") or source.get("fabricRawData"):
                fabric_record = cls._normalize_fabric_iq_reference(source)
                append_record(fabric_record, "fabricOntology")
                continue

            # Work IQ shape (workIQ): attributions + extracts.
            if source.get("attributions") or source.get("extracts"):
                record = cls._normalize_work_iq_reference(source)
                append_record(record, "workIQ")
                continue

            # indexedOneLake shape: azureBlob-like projection with a
            # OneLake-flavored file pointer or lakehouse identifier.
            # Checked before the generic azureBlob fallback so the
            # citation carries workspace / lakehouse context.
            if (
                source.get("oneLakeFilePath")
                or source.get("abfssPath")
                or source.get("lakehouseId")
            ):
                onelake_record = cls._normalize_onelake_reference(source)
                append_record(onelake_record, "indexedOneLake")
                continue

            # SharePoint Indexed shape (indexedSharePoint): SharePoint-oriented
            # link fields on top of an otherwise flat sourceData. Only route
            # here when a SharePoint-index-specific field is present so
            # genuinely generic searchIndex hits still fall through to the
            # flat path below. webUrl arrives here (rather than being eaten
            # by the SharePoint remote branch above) because SharePoint
            # remote now requires Copilot-Retrieval-API-specific markers
            # (resourceMetadata / extracts). SharePoint Indexed is
            # identified by webUrl / webPath / siteUrl / driveItemId.
            if (
                source.get("webUrl")
                or source.get("webPath")
                or source.get("siteUrl")
                or source.get("driveItemId")
            ):
                sharepoint_record = cls._normalize_sharepoint_indexed_reference(source)
                if sharepoint_record is not None:
                    append_record(sharepoint_record, "indexedSharePoint")
                    continue
                # Fall through only if the SharePoint-oriented extractor
                # could not produce content; the generic path may still
                # salvage something usable.

            content = (
                source.get("snippet")
                or source.get("content")
                or source.get("text")
                or ""
            )
            if not content:
                continue

            link = (
                source.get("blob_url")
                or source.get("url")
                or source.get("filepath")
                or source.get("path")
                or ref.get("blobUrl")
                or ""
            )

            title = source.get("title") or ref.get("docKey") or source.get("id")
            if not title and link:
                # Derive a human-friendly title from the blob/file name so the
                # citation surface is not an opaque uid.
                tail = link.rsplit("/", 1)[-1]
                title = tail.split("?", 1)[0] or None
            if not title:
                title = "reference"

            append_record(
                {"title": title, "link": link, "content": content},
                {
                    "searchindex": "searchIndex",
                    "azureblob": "azureBlob",
                }.get(ref_type, "foundryIQ"),
            )
        return records

    def _build_conversation_source_params(
        self,
        *,
        conversation_id: Optional[str],
        user_context: Optional[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build the sidecar searchIndex source that carries runtime uploads.

        Returns ``None`` when the hybrid file-upload sidecar does not apply:

        - the feature is disabled, or
        - the primary source is not the native ``azureBlob`` corpus (Pattern B
          already trims its single searchIndex source by conversationId), or
        - no conversation knowledge source name was provisioned.

        When it does apply, the source is a ``searchIndex`` knowledge source over
        the existing Agent Landing Zone index, always trimmed by a conversationId plus
        caller-ownership ``filterAddOn``; it is skipped when the caller
        principal is unknown. ``failOnError`` is ``false`` so
        an empty or missing upload index degrades gracefully to the shared corpus
        instead of failing the whole retrieve.
        """
        if not self.conversation_upload_enabled:
            return None
        if self.knowledge_source_kind != "azureBlob":
            # Pattern B (searchIndex primary) already applies a conversationId
            # filterAddOn on its own source; a second source would double-count.
            return None
        if not self.conversation_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED=true but "
                "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME is empty; skipping the "
                "file-upload sidecar source."
            )
            return None
        cid = (conversation_id or "").strip()
        if not cid:
            logging.warning(
                "[FoundryIQClient] FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED=true but "
                "conversation_id is empty; skipping the file-upload sidecar source."
            )
            return None
        if self.api_version != DEFAULT_FOUNDRY_IQ_API_VERSION:
            raise ValueError(
                "Foundry IQ conversation-upload filterAddOn requires "
                f"{DEFAULT_FOUNDRY_IQ_API_VERSION}; current "
                f"FOUNDRY_IQ_API_VERSION={self.api_version}."
            )
        filter_add_on = build_conversation_upload_filter_add_on(
            cid, _resolve_conversation_owner_id(user_context)
        )
        if not filter_add_on:
            logging.warning(
                "[FoundryIQClient] FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED=true but "
                "the caller principal is unknown; skipping the file-upload sidecar source."
            )
            return None
        params: Dict[str, Any] = {
            "knowledgeSourceName": self.conversation_knowledge_source_name,
            "kind": "searchIndex",
            "includeReferences": True,
            "includeReferenceSourceData": True,
            "failOnError": False,
            "filterAddOn": filter_add_on,
        }
        logging.info(
            "[FoundryIQClient][Upload] Adding conversation-upload source %s "
            "(conversation_owner_scoped filterAddOn)",
            self.conversation_knowledge_source_name,
        )
        return params

    def _build_work_iq_source_params(
        self, *, obo_token: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Build the Work IQ (Microsoft 365) knowledge source entry.

        Returns ``None`` when Work IQ does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned, or
        - no per-user OBO token is available (remote kinds strictly require
          impersonation - MI fallback is never used).

        Work IQ enforces ACL natively via the M365 user token, so no
        ``filterAddOn`` is emitted.
        """
        if not self.work_iq_enabled:
            return None
        if not self.work_iq_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] WORK_IQ_ENABLED=true but "
                "WORK_IQ_KNOWLEDGE_SOURCE_NAME is empty; skipping Work IQ source."
            )
            return None
        if not obo_token:
            logging.warning(
                "[FoundryIQClient] WORK_IQ_ENABLED=true but no OBO token "
                "(x-ms-query-source-authorization) available; skipping Work IQ "
                "source. Managed-identity fallback is never used for remote "
                "knowledge source kinds."
            )
            return None

        logging.info(
            "[FoundryIQClient][WorkIQ] Adding workIQ source %s (ACL native, no filterAddOn)",
            self.work_iq_knowledge_source_name,
        )
        return {
            "knowledgeSourceName": self.work_iq_knowledge_source_name,
            "kind": "workIQ",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }

    def _build_fabric_iq_source_params(
        self, *, obo_token: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Build the Fabric IQ (Microsoft Fabric ontology) knowledge source entry.

        Returns ``None`` when Fabric IQ does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned, or
        - no per-user OBO token is available (remote kinds strictly require
          impersonation; MI fallback is never used).

        Fabric IQ enforces ACL natively via the forwarded per-user token
        (workspace / ontology / underlying-item permissions), so no
        ``filterAddOn`` is emitted. The fabricOntology KS itself carries the
        workspaceId and ontologyId at registration time; the retrieve-time
        entry only needs the knowledge source name and reference flags.
        """
        if not self.fabric_iq_enabled:
            return None
        if not self.fabric_iq_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] FABRIC_IQ_ENABLED=true but "
                "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME is empty; skipping Fabric IQ source."
            )
            return None
        if not obo_token:
            logging.warning(
                "[FoundryIQClient] FABRIC_IQ_ENABLED=true but no OBO token "
                "(x-ms-query-source-authorization) available; skipping Fabric IQ "
                "source. Managed-identity fallback is never used for remote "
                "knowledge source kinds."
            )
            return None

        logging.info(
            "[FoundryIQClient][FabricIQ] Adding fabricOntology source %s (ACL native, no filterAddOn)",
            self.fabric_iq_knowledge_source_name,
        )
        return {
            "knowledgeSourceName": self.fabric_iq_knowledge_source_name,
            "kind": "fabricOntology",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }

    def _build_fabric_data_agent_source_params(
        self, *, obo_token: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Build the Fabric Data Agent knowledge source entry.

        Returns ``None`` when Fabric Data Agent does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned, or
        - no per-user OBO token is available (remote kinds strictly require
          impersonation; MI fallback is never used).

        Fabric Data Agent enforces ACL natively via the forwarded per-user
        token (workspace / data agent / underlying-item permissions), so no
        ``filterAddOn`` is emitted. The fabricDataAgent KS itself carries
        the workspaceId and dataAgentId at registration time; the
        retrieve-time entry only needs the knowledge source name and
        reference flags.
        """
        if not self.fabric_data_agent_enabled:
            return None
        if not self.fabric_data_agent_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] FABRIC_DATA_AGENT_ENABLED=true but "
                "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME is empty; skipping "
                "Fabric Data Agent source."
            )
            return None
        if not obo_token:
            logging.warning(
                "[FoundryIQClient] FABRIC_DATA_AGENT_ENABLED=true but no OBO "
                "token (x-ms-query-source-authorization) available; skipping "
                "Fabric Data Agent source. Managed-identity fallback is never "
                "used for remote knowledge source kinds."
            )
            return None

        logging.info(
            "[FoundryIQClient][FabricDataAgent] Adding fabricDataAgent source "
            "%s (ACL native, no filterAddOn)",
            self.fabric_data_agent_knowledge_source_name,
        )
        return {
            "knowledgeSourceName": self.fabric_data_agent_knowledge_source_name,
            "kind": "fabricDataAgent",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }

    def _build_sharepoint_remote_source_params(
        self, *, obo_token: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Build the SharePoint remote (Copilot Retrieval API) KS entry.

        Returns ``None`` when SharePoint remote does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned, or
        - no per-user OBO token is available (remote kinds strictly require
          impersonation; MI fallback is never used).

        SharePoint remote enforces ACL natively via the forwarded per-user
        token (item-level M365 permissions), so no ``filterAddOn`` is
        emitted. An optional retrieval-time KQL scope can be forwarded via
        ``filterExpressionAddOn`` when configured.
        """
        if not self.sharepoint_remote_enabled:
            return None
        if not self.sharepoint_remote_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] SHAREPOINT_REMOTE_ENABLED=true but "
                "SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME is empty; skipping "
                "SharePoint remote source."
            )
            return None
        if not obo_token:
            logging.warning(
                "[FoundryIQClient] SHAREPOINT_REMOTE_ENABLED=true but no OBO "
                "token (x-ms-query-source-authorization) available; skipping "
                "SharePoint remote source. Managed-identity fallback is "
                "never used for remote knowledge source kinds."
            )
            return None

        logging.info(
            "[FoundryIQClient][SharePointRemote] Adding remoteSharePoint "
            "source %s (ACL native, no filterAddOn)",
            self.sharepoint_remote_knowledge_source_name,
        )
        params: Dict[str, Any] = {
            "knowledgeSourceName": self.sharepoint_remote_knowledge_source_name,
            "kind": "remoteSharePoint",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }
        if self.sharepoint_remote_filter_expression_add_on:
            params["filterExpressionAddOn"] = (
                self.sharepoint_remote_filter_expression_add_on
            )
        return params

    def _build_onelake_source_params(self) -> Optional[Dict[str, Any]]:
        """Build the ``indexedOneLake`` knowledge source entry.

        Returns ``None`` when OneLake does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned.

        Because Foundry IQ owns the underlying AI Search index for the
        OneLake KS, this is a native (not remote) kind: it does not
        require an OBO token and does not trigger the
        ``maxRuntimeInSeconds`` bump. Workspace / lakehouse identifiers
        are bound at KS registration time; the retrieve-time entry only
        needs the knowledge source name and reference flags.
        """
        if not self.onelake_ks_enabled:
            return None
        if not self.onelake_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] ONELAKE_KS_ENABLED=true but "
                "ONELAKE_KNOWLEDGE_SOURCE_NAME is empty; skipping "
                "OneLake source."
            )
            return None

        logging.info(
            "[FoundryIQClient][OneLake] Adding indexedOneLake source "
            "%s (workspace=%s, lakehouse=%s)",
            self.onelake_knowledge_source_name,
            self.onelake_workspace_id or "<unset>",
            self.onelake_lakehouse_id or "<unset>",
        )
        return {
            "knowledgeSourceName": self.onelake_knowledge_source_name,
            "kind": "indexedOneLake",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }

    def _build_sharepoint_indexed_source_params(self) -> Optional[Dict[str, Any]]:
        """Build the ``indexedSharePoint`` knowledge source entry.

        Returns ``None`` when SharePoint Indexed does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned.

        Unlike the remote workIQ / fabric* kinds, ``indexedSharePoint`` is a
        server-side knowledge source registered on the AI Search service.
        Auth to SharePoint is baked into the KS ``connectionString`` at
        registration time via a Federated Identity Credential (managed
        identity + Graph ``Sites.Selected`` app-only permission), so no
        per-request OBO token is required. Retrieve reads a pre-built local
        index and is fast, so this KS does not participate in
        :meth:`_remote_kinds_enabled` and does not lift
        ``maxRuntimeInSeconds``.
        """
        if not self.sharepoint_indexed_enabled:
            return None
        if not self.sharepoint_indexed_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] SHAREPOINT_INDEXED_ENABLED=true but "
                "SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME is empty; skipping "
                "SharePoint Indexed source."
            )
            return None

        logging.info(
            "[FoundryIQClient][SharePointIndexed] Adding indexedSharePoint "
            "source %s (server-side KS, app-only auth via FIC)",
            self.sharepoint_indexed_knowledge_source_name,
        )
        return {
            "knowledgeSourceName": self.sharepoint_indexed_knowledge_source_name,
            "kind": "indexedSharePoint",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }

    def _build_web_source_params(self) -> Optional[Dict[str, Any]]:
        """Build the web (Grounding with Bing) knowledge source entry.

        Returns ``None`` when web grounding does not apply:

        - the feature is disabled, or
        - no knowledge source name was provisioned.

        Web grounding hits the public internet: there is no per-user ACL to
        enforce, no OBO impersonation, and no ``filterAddOn``. The only
        request-time trimming is the optional ``webParameters.domains``
        allow / block lists. When both lists are empty the ``webParameters``
        block is omitted so the request body stays minimal.
        """
        if not self.web_grounding_enabled:
            return None
        if not self.web_grounding_knowledge_source_name:
            logging.warning(
                "[FoundryIQClient] WEB_GROUNDING_ENABLED=true but "
                "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME is empty; skipping web "
                "grounding source."
            )
            return None

        params: Dict[str, Any] = {
            "knowledgeSourceName": self.web_grounding_knowledge_source_name,
            "kind": "web",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }

        allowed = list(self.web_grounding_allowed_domains)
        blocked = list(self.web_grounding_blocked_domains)
        if allowed or blocked:
            domains: Dict[str, List[str]] = {}
            if allowed:
                domains["allowedDomains"] = allowed
            if blocked:
                domains["blockedDomains"] = blocked
            params["webParameters"] = {"domains": domains}

        logging.info(
            "[FoundryIQClient][Web] Adding web source %s (allowed=%d, blocked=%d)",
            self.web_grounding_knowledge_source_name,
            len(allowed),
            len(blocked),
        )
        return params

    def _remote_kinds_enabled(self) -> bool:
        """Return True when any remote knowledge source is on.

        Controls emission of the ``maxRuntimeInSeconds`` runtime ceiling. Web
        grounding calls the public Bing endpoint and can add several seconds
        of latency, so it counts toward the ceiling even though it is not in
        the OBO-required ``_REMOTE_KNOWLEDGE_SOURCE_KINDS`` frozenset.
        """
        return (
            self.work_iq_enabled
            or self.fabric_iq_enabled
            or self.fabric_data_agent_enabled
            or self.sharepoint_remote_enabled
            or self.web_grounding_enabled
        )

    def _handle_mcp_activity(
        self, payload: Mapping[str, Any], *, http_status: int
    ) -> None:
        """Log safe MCP activity fields and enforce configured failure policy."""
        sources = self.mcp_config.source_by_name()
        failures: list[tuple[str, str]] = []

        for raw_activity in payload.get("activity", []) or []:
            if not isinstance(raw_activity, Mapping):
                continue
            source_name = str(raw_activity.get("knowledgeSourceName") or "")
            if source_name not in sources:
                continue
            arguments = raw_activity.get("mcpServerArguments")
            arguments = arguments if isinstance(arguments, Mapping) else {}
            tool_name = str(
                arguments.get("toolName") or raw_activity.get("toolName") or ""
            )
            error = raw_activity.get("error") or raw_activity.get("errors")
            status_value = raw_activity.get("status")
            status_text = str(status_value or "").strip().lower()
            status_code = raw_activity.get("statusCode")
            failed = bool(error) or status_text in {
                "error",
                "failed",
                "failure",
                "timeout",
            }
            if isinstance(status_code, int) and status_code >= 400:
                failed = True

            emitter = AuditEmitter.default()
            if emitter.enabled:
                audit_context = current_audit_context()
                if audit_context is not None:
                    elapsed_ms_value = raw_activity.get("elapsedMs")
                    try:
                        if isinstance(elapsed_ms_value, bool):
                            raise ValueError
                        elapsed_ms = float(elapsed_ms_value)
                        if (
                            not math.isfinite(elapsed_ms)
                            or elapsed_ms < 0
                            or elapsed_ms > MAX_AUDIT_DURATION_MS
                        ):
                            raise ValueError
                    except (TypeError, ValueError, OverflowError):
                        emitter.emit_failure(ReasonCode.VALIDATION_FAILED)
                    else:
                        if emitter.reserve_tool_invocation():
                            count_value = raw_activity.get("count")
                            if not isinstance(count_value, int) or isinstance(
                                count_value, bool
                            ):
                                count_value = None
                            observed_at = utc_now()
                            started_at = observed_at - timedelta(
                                milliseconds=elapsed_ms
                            )
                            tool_id = emitter.pseudonymize(
                                "tool", f"{source_name}:{tool_name}"
                            )
                            invocation_id = emitter.pseudonymize(
                                "tool-invocation",
                                f"{source_name}:{tool_name}:"
                                f"{raw_activity.get('id') or new_event_id()}",
                            )
                            started_event_id = emitter.emit(
                                EventType.TOOL_STARTED,
                                operation="foundry_iq.mcp_tool",
                                status=AuditStatus.STARTED,
                                reason_code=ReasonCode.TOOL_INVOKED,
                                parent_event_id=(
                                    audit_context.request_started_event_id
                                ),
                                event_time=started_at,
                                metadata={
                                    "tool_name": "foundry_iq_mcp_tool",
                                    "tool_id": tool_id,
                                    "tool_invocation_id": invocation_id,
                                    "transport": "foundry_iq",
                                    "timing_source": "reconstructed",
                                },
                                _reserved_tool_event=True,
                                _omission_kind="tool",
                            )
                            emitter.emit(
                                (
                                    EventType.TOOL_FAILED
                                    if failed
                                    else EventType.TOOL_COMPLETED
                                ),
                                operation="foundry_iq.mcp_tool",
                                status=(
                                    AuditStatus.FAILED
                                    if failed
                                    else AuditStatus.COMPLETED
                                ),
                                reason_code=(
                                    ReasonCode.TIMEOUT
                                    if status_text == "timeout"
                                    else (
                                        ReasonCode.TOOL_FAILED
                                        if failed
                                        else ReasonCode.TOOL_COMPLETED
                                    )
                                ),
                                parent_event_id=started_event_id,
                                started_at=started_at,
                                duration_ms=elapsed_ms,
                                metadata={
                                    "tool_name": "foundry_iq_mcp_tool",
                                    "tool_id": tool_id,
                                    "tool_invocation_id": invocation_id,
                                    "failure_type": (
                                        "timeout"
                                        if status_text == "timeout"
                                        else ("remote_error" if failed else None)
                                    ),
                                    "output_count": count_value,
                                    "partial_output": http_status == 206,
                                    "timing_source": "reconstructed",
                                },
                                _reserved_tool_event=True,
                                _omission_kind="tool",
                            )

            error_code = ""
            if isinstance(error, Mapping):
                error_code = str(
                    error.get("code") or error.get("type") or "activity_error"
                )
            elif error:
                error_code = "activity_error"
            elif failed:
                error_code = status_text or str(status_code or "activity_error")

            logging.log(
                logging.WARNING if failed else logging.INFO,
                "[FoundryIQClient][MCP] source=%s tool=%s elapsed_ms=%s "
                "count=%s status=%s error=%s partial=%s reasoning=%s runtime=%s",
                source_name,
                tool_name or "<none>",
                raw_activity.get("elapsedMs"),
                raw_activity.get("count"),
                status_text or status_code or "success",
                error_code or "<none>",
                http_status == 206,
                self.mcp_config.reasoning_effort,
                self.max_runtime_seconds,
                extra={
                    "mcp_source": source_name,
                    "mcp_tool": tool_name,
                    "mcp_elapsed_ms": raw_activity.get("elapsedMs"),
                    "mcp_count": raw_activity.get("count"),
                    "mcp_status": status_text or status_code or "success",
                    "mcp_error": error_code,
                    "mcp_partial": http_status == 206,
                },
            )
            if self.mcp_config.log_tool_arguments and arguments.get(
                "toolArguments"
            ) is not None:
                safe_arguments = redact_mcp_tool_arguments(
                    arguments["toolArguments"]
                )
                logging.debug(
                    "[FoundryIQClient][MCP] source=%s tool=%s arguments=%s",
                    source_name,
                    tool_name or "<none>",
                    safe_arguments,
                )
            if failed:
                failures.append((source_name, error_code or "activity_error"))

        required_failures = [
            (source_name, error_code)
            for source_name, error_code in failures
            if sources[source_name].fail_on_error
        ]
        if required_failures:
            source_name, error_code = required_failures[0]
            raise McpSourceError(
                f"MCP source {source_name!r} failed with {error_code}"
            )

    async def retrieve(
        self,
        query: str,
        *,
        obo_token: Optional[str] = None,
        incoming_token: Optional[str] = None,
        conversation_id: Optional[str] = None,
        user_context: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """Run the knowledge base retrieve action and return normalized records.

        :param query: Natural-language user query.
        :param obo_token: Optional delegated Azure AI Search user token. When
            present it is forwarded in ``x-ms-query-source-authorization`` for
            per-user document-level security. This is the OBO mechanism, kept
            distinct from the Pattern B ``filterAddOn`` security filter.
        :param incoming_token: Original user token accepted by the orchestrator.
            MCP OBO headers exchange this assertion for each configured explicit
            scope and never reuse the Search-audience ``obo_token``.
        :param conversation_id: Optional conversation scope. When Pattern B
            ``filterAddOn`` is enabled, this narrows runtime-upload documents to
            the current conversation plus shared/global chunks.
        :param user_context: Optional user/security context used only for Pattern
            B security-field filters. Native permission enforcement continues to
            use ``obo_token`` and the ``x-ms-query-source-authorization`` header.
        :return: A list of ``{title, link, content}`` records.
        """
        url = self._retrieve_url()
        start = time.time()

        # Service bearer token (the same search audience used by SearchClient).
        try:
            token = (await self.credential.get_token(_SEARCH_SCOPE)).token
        except Exception as exc:
            logging.error("[FoundryIQClient] failed to acquire service token (%s)", type(exc).__name__)
            if self.mcp_config.enabled:
                raise McpCredentialError(
                    "Failed to acquire the Foundry IQ service token"
                ) from None
            raise

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        TraceContextTextMapPropagator().inject(headers)
        if obo_token:
            # Per-user document-level security (query-time ACL/RBAC enforcement).
            headers["x-ms-query-source-authorization"] = obo_token
            logging.info(
                "[FoundryIQClient][Trimming] x-ms-query-source-authorization=present "
                "token_audience=%s source=obo",
                _SEARCH_SCOPE,
            )
        else:
            # Never substitute the service managed identity for the caller's
            # identity: permission-trimmed sources must fail closed without a
            # user token. Callers gate anonymous retrieval via ALLOW_ANONYMOUS.
            logging.info(
                "[FoundryIQClient][Trimming] x-ms-query-source-authorization=absent "
                "reason=no OBO token"
            )

        if self.mcp_config.enabled:
            # Local imports avoid the existing search -> foundry_iq dependency
            # becoming a module-import cycle.
            from connectors.keyvault import get_secret
            from connectors.obo import acquire_obo_token

            try:
                mcp_headers, credential_modes = await build_mcp_control_headers(
                    self.mcp_config,
                    credential=self.credential,
                    incoming_token=incoming_token,
                    acquire_obo_token=acquire_obo_token,
                    get_secret=get_secret,
                )
            except McpCredentialError:
                raise
            except Exception as exc:
                raise McpCredentialError(
                    "Failed to resolve an MCP query credential"
                ) from exc
            headers.update(mcp_headers)
            for source_name, modes in credential_modes.items():
                logging.info(
                    "[FoundryIQClient][MCP] source=%s credential_modes=%s",
                    source_name,
                    ",".join(modes),
                )

        if conversation_id:
            logging.debug("[FoundryIQClient] conversation_id=%s", conversation_id)

        # Agent Landing Zone configures the knowledge base with minimal reasoning, which
        # requires explicit intents rather than chat messages.
        if self.mcp_config.enabled:
            body: Dict[str, Any] = {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": query}],
                    }
                ],
                "retrievalReasoningEffort": {
                    "kind": self.mcp_config.reasoning_effort
                },
                "outputMode": "extractiveData",
                "includeActivity": True,
            }
        else:
            body = {
                "intents": [
                    {
                        "search": query,
                        "type": "semantic",
                    }
                ]
            }
        if self.max_output_documents:
            body["maxOutputDocuments"] = self.max_output_documents

        knowledge_source_params: List[Dict[str, Any]] = []
        if self.knowledge_source_name:
            source_params: Dict[str, Any] = {
                "knowledgeSourceName": self.knowledge_source_name,
                "kind": self.knowledge_source_kind,
                "includeReferences": True,
                "includeReferenceSourceData": True,
            }
            if self.filter_add_on_enabled:
                if self.knowledge_source_kind != "searchIndex":
                    raise ValueError(
                        "Foundry IQ filterAddOn is only valid for searchIndex knowledge sources; "
                        f"current kind={self.knowledge_source_kind}."
                    )
                if self.api_version != DEFAULT_FOUNDRY_IQ_API_VERSION:
                    raise ValueError(
                        "Foundry IQ Pattern B filterAddOn requires "
                        f"{DEFAULT_FOUNDRY_IQ_API_VERSION}; current "
                        f"FOUNDRY_IQ_API_VERSION={self.api_version}."
                    )
                source_params["filterAddOn"] = build_pattern_b_filter_add_on(
                    conversation_id=conversation_id,
                    user_context=user_context,
                    security_field_name=self.security_field_name,
                )
                logging.info(
                    "[FoundryIQClient][PatternB] Applying filterAddOn to knowledge source %s",
                    self.knowledge_source_name,
                )
            knowledge_source_params.append(source_params)

        conversation_source_params = self._build_conversation_source_params(
            conversation_id=conversation_id,
            user_context=user_context,
        )
        if conversation_source_params:
            knowledge_source_params.append(conversation_source_params)

        work_iq_source_params = self._build_work_iq_source_params(obo_token=obo_token)
        if work_iq_source_params:
            knowledge_source_params.append(work_iq_source_params)

        fabric_iq_source_params = self._build_fabric_iq_source_params(obo_token=obo_token)
        if fabric_iq_source_params:
            knowledge_source_params.append(fabric_iq_source_params)

        fabric_data_agent_source_params = self._build_fabric_data_agent_source_params(
            obo_token=obo_token
        )
        if fabric_data_agent_source_params:
            knowledge_source_params.append(fabric_data_agent_source_params)

        sharepoint_remote_source_params = self._build_sharepoint_remote_source_params(
            obo_token=obo_token
        )
        if sharepoint_remote_source_params:
            knowledge_source_params.append(sharepoint_remote_source_params)

        onelake_source_params = self._build_onelake_source_params()
        if onelake_source_params:
            knowledge_source_params.append(onelake_source_params)

        # SharePoint Indexed is a server-side KS on AI Search: no OBO required.
        sharepoint_indexed_source_params = self._build_sharepoint_indexed_source_params()
        if sharepoint_indexed_source_params:
            knowledge_source_params.append(sharepoint_indexed_source_params)

        web_source_params = self._build_web_source_params()
        if web_source_params:
            knowledge_source_params.append(web_source_params)

        if self.mcp_config.enabled:
            knowledge_source_params.extend(
                self.mcp_config.knowledge_source_params()
            )

        if knowledge_source_params:
            body["knowledgeSourceParams"] = knowledge_source_params

        # Remote kinds (workIQ, future fabric*) can take 40-60s to fan out.
        # Emit the runtime ceiling only when a remote kind is enabled so the
        # default Pattern A / Pattern B request body stays byte-identical.
        if self._remote_kinds_enabled() or self.mcp_config.enabled:
            body["maxRuntimeInSeconds"] = self.max_runtime_seconds

        session = await self._get_session()
        response_status = 0
        try:
            async with session.post(url, headers=headers, json=body) as resp:
                response_status = resp.status
                await resp.text()
                if resp.status >= 400:
                    if self.mcp_config.enabled:
                        logging.error(
                            "[FoundryIQClient][MCP] retrieve failed status=%s",
                            resp.status,
                        )
                        raise McpSourceError(
                            "Foundry IQ MCP retrieve failed: "
                            f"status={resp.status}"
                        )
                    logging.error("[FoundryIQClient] retrieve failed status=%s", resp.status)
                    raise RuntimeError(
                        f"Foundry IQ retrieve failed: {resp.status}"
                    )
                payload = await resp.json()
        except McpSourceError:
            raise
        except Exception as exc:
            if self.mcp_config.enabled:
                raise McpSourceError(
                    "Foundry IQ MCP retrieve request failed"
                ) from exc
            raise

        if self.mcp_config.enabled:
            self._handle_mcp_activity(payload, http_status=response_status)
        records = self._normalize_references(payload)
        logging.info(
            "[FoundryIQClient] Retrieved %d references in %.2fs "
            "(knowledge_base=%s, partial=%s)",
            len(records),
            time.time() - start,
            self.knowledge_base_name,
            response_status == 206,
        )
        return records


_foundry_iq_client_instance: Optional[FoundryIQClient] = None


def get_foundry_iq_client() -> FoundryIQClient:
    """Return a singleton :class:`FoundryIQClient` to reuse connections/config."""
    global _foundry_iq_client_instance
    if _foundry_iq_client_instance is None:
        _foundry_iq_client_instance = FoundryIQClient()
    return _foundry_iq_client_instance
