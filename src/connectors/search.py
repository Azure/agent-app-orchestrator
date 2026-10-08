import aiohttp
import logging
import json
import time
import hashlib
import functools
from collections.abc import Mapping
from typing import Optional, Any, Dict
from pydantic import BaseModel, Field
from azure.core.exceptions import AzureError

from dependencies import get_config
from util.metadata import format_custom_metadata, parse_allowed_keys
from telemetry import AuditEmitter, ReasonCode
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)
from util.retrieval_backend import get_retrieval_backend, RETRIEVAL_BACKEND_FOUNDRY_IQ
from connectors.foundry_iq import McpSourceError, get_foundry_iq_client
from connectors.foundry_iq_mcp import McpConfigurationError, McpCredentialError
from connectors.obo import (
    acquire_obo_token as _acquire_obo_token,
    classify_retrieval_error as _classify_retrieval_error,
)

# Standardized log markers for retrieval/auth failure paths. Operators can grep
# these to spot swallowed errors that would otherwise return empty results
# silently. See issue Azure/agent-landing-zone#508.
_RETRIEVAL_AUTH_FAILURE_MARKER = "[Retrieval][AUTH_FAILURE]"
_RETRIEVAL_ERROR_MARKER = "[Retrieval][ERROR]"


_global_index_empty_cache: Dict[str, Dict[str, Any]] = {}

_SEARCH_OBO_SCOPE = "https://search.azure.com/user_impersonation"


async def acquire_obo_token(
    api_access_token: Optional[str],
    scope: str,
    allow_anonymous: bool = False,
) -> Optional[str]:
    """Acquire an OBO token for an explicit downstream scope.

    Reusable helper so any strategy can obtain a search-audience delegated
    token without duplicating the OBO exchange logic.

    Returns the Bearer token string (without 'Bearer ' prefix) or None.
    """
    return await _acquire_obo_token(api_access_token, scope, allow_anonymous)


async def acquire_obo_search_token(
    api_access_token: Optional[str], allow_anonymous: bool = True
) -> Optional[str]:
    """Compatibility wrapper for Azure AI Search delegated tokens."""
    return await acquire_obo_token(
        api_access_token,
        _SEARCH_OBO_SCOPE,
        allow_anonymous=allow_anonymous,
    )


class SearchResult(BaseModel):
    """Represents a single search result from AI Search."""
    title: str
    link: str
    content: str
    # Raw custom_metadata carried for in-process consumers/tests only. It is
    # excluded from serialization on purpose: the formatted metadata block is
    # prepended into ``content`` (the text the LLM reads), so emitting the raw
    # value as a separate JSON key would duplicate it and waste tokens. When the
    # SEARCH_INCLUDE_METADATA_IN_CONTEXT flag is off this stays None and the
    # serialized output is byte-for-byte unchanged.
    custom_metadata: Optional[Any] = Field(default=None, exclude=True)


ANONYMOUS_PRINCIPAL_ID = "anonymous"
CONVERSATION_OWNER_FIELD = "metadata_security_user_ids"


def _odata_escape_string(value: Optional[str]) -> str:
    """Escape a string for embedding in single-quoted OData literals."""
    return (value or "").replace("'", "''")


def resolve_conversation_owner_id(user_context: Any) -> Optional[str]:
    """Return the server-derived principal that owns conversation uploads."""
    if not isinstance(user_context, Mapping):
        return None
    for key in ("principal_id", "oid"):
        value = str(user_context.get(key) or "").strip()
        if value:
            return value
    return None


def build_conversation_owner_clause(
    conversation_id: Optional[str],
    owner_id: Optional[str],
    *,
    field_name: str = "conversationId",
    owner_field: str = CONVERSATION_OWNER_FIELD,
) -> Optional[str]:
    """Build the clause matching conversation uploads stamped for ``owner_id``."""
    cid = (conversation_id or "").strip()
    owner = (owner_id or "").strip()
    if not cid or not owner:
        return None
    safe_field = (field_name or "").strip() or "conversationId"
    safe_owner_field = (owner_field or "").strip() or CONVERSATION_OWNER_FIELD
    if owner == ANONYMOUS_PRINCIPAL_ID:
        owner_clause = f"not {safe_owner_field}/any()"
    else:
        owner_clause = (
            f"{safe_owner_field}/any(u: u eq '{_odata_escape_string(owner)}')"
        )
    return f"({safe_field} eq '{_odata_escape_string(cid)}' and {owner_clause})"


def build_conversation_filter(
    conversation_id: Optional[str],
    *,
    field_name: str = "conversationId",
    owner_id: Optional[str] = None,
    owner_field: str = CONVERSATION_OWNER_FIELD,
) -> str:
    """Build OData filter for conversation-scoped retrieval.

    Includes:
    - conversation-specific chunks (conversationId == <cid>) only when they
      are stamped for ``owner_id``; without an owner they are excluded so a
      reused conversation id cannot expose another principal's uploads
    - shared/global chunks always. A chunk is treated as shared when its
      conversationId is the 'NaN' sentinel OR null/unset. Ingestion has used
      both representations for global corpora, so both must match here,
      otherwise globally-ingested documents become invisible to retrieval.
    """
    safe_field = (field_name or "").strip() or "conversationId"
    shared_clause = f"({safe_field} eq 'NaN' or {safe_field} eq null)"
    owned_clause = build_conversation_owner_clause(
        conversation_id,
        owner_id,
        field_name=safe_field,
        owner_field=owner_field,
    )
    if owned_clause:
        return f"{owned_clause} or {shared_clause}"
    return shared_clause


_module_build_conversation_filter = build_conversation_filter


class SearchClient:
    """
    Azure Cognitive Search client with hybrid search support.

    Handles:
    - Basic search operations (term, vector, hybrid)
    - Document retrieval by ID
    - Token acquisition and authentication
    - Embeddings generation for vector search
    """
    def __init__(self):
        """
        Initialize SearchClient with configuration.
        """
        # ==== Load all config parameters in one place ====
        self.cfg = get_config()
        self.endpoint = self.cfg.get("SEARCH_SERVICE_QUERY_ENDPOINT")
        self.api_version = self.cfg.get("SEARCH_API_VERSION", "2024-07-01")
        self.credential = self.cfg.aiocredential

        # Hybrid search configuration
        self.search_top_k = int(self.cfg.get('SEARCH_RAGINDEX_TOP_K', 3))
        self.search_approach = self.cfg.get('SEARCH_APPROACH', 'hybrid')
        self.semantic_search_config = self.cfg.get('SEARCH_SEMANTIC_SEARCH_CONFIG', 'my-semantic-config')
        self.search_service = self.cfg.get('SEARCH_SERVICE_NAME')
        self.use_semantic = self.cfg.get('SEARCH_USE_SEMANTIC', 'false').lower() == 'true'
        self.index_name = self.cfg.get("SEARCH_RAG_INDEX_NAME", "ragindex")
        self.index_empty_cache_ttl_seconds = int(self.cfg.get("SEARCH_EMPTY_CACHE_TTL_SECONDS", 60, type=int))

        # Custom metadata in LLM context (default OFF). When OFF the field is not
        # selected, keeping the query byte-for-byte unchanged. This guard matters
        # because pre-#487 indexes lack the custom_metadata field and selecting a
        # missing field makes Azure AI Search reject the whole query with 400.
        self.include_metadata_in_context = self.cfg.get("SEARCH_INCLUDE_METADATA_IN_CONTEXT", False, type=bool)
        self.metadata_max_chars = int(self.cfg.get("SEARCH_METADATA_MAX_CHARS", 500, type=int))
        self.metadata_allowed_keys = parse_allowed_keys(self.cfg.get("SEARCH_METADATA_ALLOWED_KEYS", "", type=str))

        # Per-request context (kept in memory only)
        self._request_api_access_token: Optional[str] = None
        self._allow_anonymous: bool = True

        # Cached delegated Search token (OBO) for the current request
        self._cached_search_user_token: Optional[str] = None
        self._cached_search_user_token_expires_at: float = 0.0

        # Last OBO error summary (for clear logs / strict-mode failures)
        self._last_obo_error: Optional[str] = None

        # Conversation ID scope for filtering search results
        self._conversation_id: Optional[str] = None

        # Shared aiohttp session — reuses TCP connections across all HTTP calls
        self._session: Optional[aiohttp.ClientSession] = None

        # Initialize GenAIModelClient for embeddings (only if needed for vector/hybrid search)
        self.aoai_client = None
        if self.search_approach in ["vector", "hybrid"]:
            try:
                from connectors.aifoundry import get_genai_client
                self.aoai_client = get_genai_client()
                logging.info("[SearchClient] ✅ GenAIModelClient initialized for embeddings")
            except Exception as e:
                logging.warning("[SearchClient] Could not initialize embeddings (%s)", type(e).__name__)
                logging.warning("[SearchClient] ⚠️ Falling back to term search only")
                self.search_approach = "term"

        # Cache is now maintained in the _global_index_empty_cache module variable
        # ==== End config block ====

        if not self.endpoint:
            raise ValueError("SEARCH_SERVICE_QUERY_ENDPOINT not set in config")

        logging.info("[SearchClient] ✅ Initialized with hybrid search support")
        logging.info("[SearchClient]    Index: %s", self.index_name)
        logging.info("[SearchClient]    Approach: %s", self.search_approach)
        logging.info("[SearchClient]    Top K: %s", self.search_top_k)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Returns a shared aiohttp session, creating one lazily if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def set_request_context(
        self,
        *,
        api_access_token: Optional[str],
        allow_anonymous: bool,
        conversation_id: Optional[str] = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Sets per-request context used for permission trimming.

        api_access_token is the incoming user token sent to the orchestrator (audience: this API).
        It is used as the OBO assertion to acquire a Search-audience user token.
        """
        api_access_token = (api_access_token or "").strip() or None
        if api_access_token != self._request_api_access_token:
            # Token changed (new request); drop cached OBO token.
            self._cached_search_user_token = None
            self._cached_search_user_token_expires_at = 0.0

        self._request_api_access_token = api_access_token
        self._allow_anonymous = bool(allow_anonymous)
        self._conversation_id = (conversation_id or "").strip() or None
        self._user_context = dict(user_context or {})


    def _token_fingerprint(self, token: Optional[str]) -> str:
        if not token:
            return "<none>"
        try:
            return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        except UnicodeError:
            return "<unknown>"

    async def _acquire_search_user_token_via_obo(self, api_access_token: str) -> Optional[str]:
        """Acquire a delegated Azure AI Search token using the OBO flow.

        This exchanges the incoming API token (user assertion) for a Search-audience token.
        """
        tenant_id = (self.cfg.get_value("OAUTH_AZURE_AD_TENANT_ID", default=None, allow_none=True) or "").strip() or None
        client_id = (self.cfg.get_value("OAUTH_AZURE_AD_CLIENT_ID", default=None, allow_none=True) or "").strip() or None
        client_secret = (self.cfg.get_value("OAUTH_AZURE_AD_CLIENT_SECRET", default=None, allow_none=True) or "").strip() or None

        if not tenant_id or not client_id or not client_secret:
            logging.warning(
                "[Retrieval][OBO] Missing Entra configuration for OBO. tenant_id=%s client_id=%s client_secret=%s",
                "set" if tenant_id else "missing",
                "set" if client_id else "missing",
                "set" if client_secret else "missing",
            )
            return None

        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

        form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "requested_token_use": "on_behalf_of",
            "scope": "https://search.azure.com/user_impersonation",
            "assertion": api_access_token,
        }

        # Important: never log the assertion.
        fp = self._token_fingerprint(api_access_token)
        logging.debug("[Retrieval][OBO] Requesting Search delegated token via OBO (assertion_fp=%s)", fp)

        session = await self._get_session()
        async with session.post(token_url, data=form) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    # Token endpoint errors often include trace_id/correlation_id.
                    level, marker = _classify_retrieval_error(resp.status)
                    try:
                        err = json.loads(raw)
                    except (ValueError, RecursionError):
                        err = None
                    if isinstance(err, dict):
                        error = err.get("error")
                        desc = err.get("error_description")
                        trace_id = err.get("trace_id")
                        correlation_id = err.get("correlation_id")
                        error_codes = err.get("error_codes")
                        self._last_obo_error = (
                            f"status={resp.status} error={error} codes={error_codes} trace_id={trace_id} correlation_id={correlation_id}"
                        )
                        logging.log(
                            level,
                            "%s OBO failed (status=%d error=%s codes=%s trace_id=%s correlation_id=%s desc=%s)",
                            marker,
                            resp.status,
                            error,
                            error_codes,
                            trace_id,
                            correlation_id,
                            (str(desc)[:240] + "…") if desc and len(str(desc)) > 240 else desc,
                            extra={
                                "retrieval_status": resp.status,
                                "retrieval_index": self.index_name,
                                "retrieval_credential_type": "obo",
                            },
                        )
                    else:
                        self._last_obo_error = f"status={resp.status} body={raw[:200]}"
                        logging.log(
                            level,
                            "%s OBO failed (status=%d body=%s)",
                            marker,
                            resp.status,
                            raw[:400],
                            extra={
                                "retrieval_status": resp.status,
                                "retrieval_index": self.index_name,
                                "retrieval_credential_type": "obo",
                            },
                        )
                    return None

                data = {}
                try:
                    data = json.loads(raw)
                except (ValueError, RecursionError):
                    logging.error("[Retrieval][OBO] Token endpoint returned non-JSON response")
                    return None

                token = data.get("access_token")
                expires_in = data.get("expires_in")
                if not token:
                    self._last_obo_error = "token endpoint response missing access_token"
                    logging.error("[Retrieval][OBO] Token endpoint response missing access_token")
                    return None

                # Cache for the remainder of the request.
                try:
                    ttl = int(expires_in) if expires_in is not None else 0
                except (TypeError, ValueError, OverflowError):
                    ttl = 0
                self._cached_search_user_token = token
                self._cached_search_user_token_expires_at = time.time() + max(0, ttl - 30)

                self._last_obo_error = None

                logging.info("[Retrieval][OBO] ✅ Acquired Search delegated token via OBO")
                return token

    async def _get_search_user_token_for_trimming(self) -> Optional[str]:
        # Use cached token if still valid.
        if self._cached_search_user_token and time.time() < self._cached_search_user_token_expires_at:
            return self._cached_search_user_token

        # No incoming user token -> cannot do OBO.
        if not self._request_api_access_token:
            if self._allow_anonymous:
                logging.info(
                    "[Retrieval][Trimming] No incoming user token; running without x-ms-query-source-authorization because ALLOW_ANONYMOUS=true"
                )
                return None

            logging.error(
                "[Retrieval][Trimming] Missing incoming user token. Permission trimming is required but ALLOW_ANONYMOUS=false. "
                "Refusing to call Search without x-ms-query-source-authorization."
            )
            raise RuntimeError(
                "Permission trimming requires an incoming user access token. "
                "No Authorization header was available and ALLOW_ANONYMOUS=false."
            )

        token = await self._acquire_search_user_token_via_obo(self._request_api_access_token)
        if token:
            return token

        if self._allow_anonymous:
            level, marker = _classify_retrieval_error(self._last_obo_error)
            logging.log(
                level,
                "%s OBO failed; running without x-ms-query-source-authorization because ALLOW_ANONYMOUS=true (details=%s)",
                marker,
                self._last_obo_error or "<no-details>",
                extra={
                    "retrieval_index": self.index_name,
                    "retrieval_credential_type": "obo",
                },
            )
            return None

        logging.error(
            "[Retrieval][Trimming] OBO failed and ALLOW_ANONYMOUS=false. Refusing to call Search without x-ms-query-source-authorization. Details: %s",
            self._last_obo_error or "<no-details>",
        )
        raise RuntimeError(
            "Failed to acquire Azure AI Search delegated token via OBO. "
            "Ensure API permissions include Azure Cognitive Search delegated user_impersonation and admin consent is granted."
        )

    async def search(self, index_name: str, body: dict, *, search_user_token: Optional[str] = None) -> dict:
        """
        Executes a search POST against /indexes/{index_name}/docs/search.
        """
        url = (
            f"{self.endpoint}"
            f"/indexes/{index_name}/docs/search"
            f"?api-version={self.api_version}"
        )

        # get bearer token
        try:
            token = (await self.credential.get_token("https://search.azure.com/.default")).token
        except AzureError as exc:
            logging.error("[search] failed to acquire token (%s)", type(exc).__name__)
            raise

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        # Optional: user context token for permission trimming.
        if search_user_token:
            headers["x-ms-query-source-authorization"] = search_user_token
        TraceContextTextMapPropagator().inject(headers)

        session = await self._get_session()
        async with session.post(url, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logging.error(f"[search] {resp.status} {text}")
                    raise RuntimeError(f"Search failed: {resp.status} {text}")
                return await resp.json()

    async def get_document(self, index_name: str, document_id: str, select_fields: list = None) -> dict:
        """
        Retrieves a single document by ID from the index.
        GET /indexes/{index_name}/docs/{document_id}

        Args:
            index_name: Name of the search index
            document_id: Document key/ID
            select_fields: Optional list of fields to retrieve (e.g., ['filepath', 'title'])

        Returns:
            Document dictionary with requested fields
        """
        # Build URL with optional $select parameter
        url = (
            f"{self.endpoint}"
            f"/indexes/{index_name}/docs('{document_id}')"
            f"?api-version={self.api_version}"
        )

        if select_fields:
            fields_str = ",".join(select_fields)
            url += f"&$select={fields_str}"

        # Get bearer token
        try:
            token = (await self.credential.get_token("https://search.azure.com/.default")).token
        except AzureError as exc:
            logging.error(
                "[search] failed to acquire token for get_document (%s)",
                type(exc).__name__,
            )
            raise

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    logging.warning(f"[search] Document not found: {document_id}")
                    return None
                if resp.status >= 400:
                    logging.error("[search] Get document failed (status=%s)", resp.status)
                    raise RuntimeError(f"Get document failed: {resp.status}")
                return await resp.json()

    async def is_index_empty(self):
        """
        Fast check to see if the search index is completely empty, caching the result.
        Returns True if empty, False if it has documents.
        """
        global _global_index_empty_cache
        cached_entry = _global_index_empty_cache.get(self.index_name)
        if cached_entry:
            # Backward compatibility: older cache shape used raw bool values.
            if isinstance(cached_entry, bool):
                logging.info(f"[Retrieval] Index '{self.index_name}' empty cache hit (legacy); bypassing index probe")
                return cached_entry

            cached_is_empty = bool(cached_entry.get("is_empty", False))
            cached_at = float(cached_entry.get("checked_at", 0.0))
            cache_age_seconds = max(0.0, time.time() - cached_at)

            if cache_age_seconds < self.index_empty_cache_ttl_seconds:
                logging.info(
                    "[Retrieval] Index '%s' empty cache hit (age=%.1fs, ttl=%ss); bypassing index probe",
                    self.index_name,
                    cache_age_seconds,
                    self.index_empty_cache_ttl_seconds,
                )
                return cached_is_empty

            logging.info(
                "[Retrieval] Index '%s' empty cache expired (age=%.1fs >= ttl=%ss); re-probing",
                self.index_name,
                cache_age_seconds,
                self.index_empty_cache_ttl_seconds,
            )

        try:
            logging.info(f"[Retrieval] Probing if index '{self.index_name}' is empty...")
            # Simple query requesting 1 document with no vector/hybrid overhead
            search_body: Dict[str, Any] = {
                "search": "*",
                "select": "id",
                "top": 1
            }

            search_user_token = await self._get_search_user_token_for_trimming()

            results = await self.search(
                index_name=self.index_name,
                body=search_body,
                search_user_token=search_user_token,
            )

            # If no 'value' or empty list, it's empty
            has_results = len(results.get('value', [])) > 0
            is_empty_result = not has_results
            _global_index_empty_cache[self.index_name] = {
                "is_empty": is_empty_result,
                "checked_at": time.time(),
            }

            if is_empty_result:
                logging.info(f"[Retrieval] Probe confirmed: Index '{self.index_name}' is EMPTY.")
            else:
                logging.info(f"[Retrieval] Probe confirmed: Index '{self.index_name}' HAS DOCUMENTS.")

            return is_empty_result

        except Exception as e:
            logging.error("[Retrieval] Failed to check if index is empty (%s)", type(e).__name__)
            # Default to not empty if we can't tell, to avoid false bypasses
            return False

    async def search_knowledge_base(self, query: str) -> str:
        """
        Searches the knowledge base for relevant documents using hybrid search.

        :param query: The search query to find relevant documents.
        :return: Search results as a JSON string containing a list of documents with title, link and content.
        """
        # Backend selector (Azure/agent-landing-zone#526). The default 'ai_search' keeps the
        # exact path below; 'foundry_iq' delegates to the knowledge base retrieve
        # action and maps into the identical return contract.
        if get_retrieval_backend() == RETRIEVAL_BACKEND_FOUNDRY_IQ:
            return await self._search_knowledge_base_foundry_iq(query)

        logging.info(f"[Retrieval] AI Search index: {self.index_name}")
        logging.info(f"[Retrieval] Search approach: {self.search_approach}")
        logging.info(f"[Retrieval] Executing search for query: {query}")

        # Bind the server-derived owner so conversation uploads stay principal-scoped.
        build_conversation_filter = functools.partial(
            _module_build_conversation_filter,
            owner_id=resolve_conversation_owner_id(getattr(self, "_user_context", None)),
        )
        search_user_token = None
        try:
            logging.info("[Retrieval] Using Azure AI Search for document retrieval")

            # Build search body according to search approach. custom_metadata is
            # only added to the select when the flag is on; otherwise the select
            # string stays exactly as before so pre-#487 indexes keep working.
            select_fields = "title,content,url,filepath,chunk_id"
            if self.include_metadata_in_context:
                select_fields += ",custom_metadata"
            search_body: Dict[str, Any] = {
                "select": select_fields,
                "top": self.search_top_k
            }

            # Filter by conversation scope: this chat + shared corpora (general/global).
            # Never query without a filter: an unset id would return every chunk in the index.
            search_body["filter"] = build_conversation_filter(self._conversation_id, field_name="conversationId")

            # Generate embeddings for vector/hybrid search
            if self.search_approach in ["vector", "hybrid"] and self.aoai_client:
                start_time = time.time()
                logging.info("[Retrieval] Generating embeddings for query")
                embeddings_query = await self.aoai_client.get_embeddings(query)
                logging.info(f"[Retrieval] Embeddings generated in {round(time.time() - start_time, 2)} seconds")

                if self.search_approach == "vector":
                    search_body["vectorQueries"] = [{
                        "kind": "vector",
                        "vector": embeddings_query,
                        "fields": "contentVector",
                        "k": self.search_top_k
                    }]
                elif self.search_approach == "hybrid":
                    search_body["search"] = query
                    search_body["vectorQueries"] = [{
                        "kind": "vector",
                        "vector": embeddings_query,
                        "fields": "contentVector",
                        "k": self.search_top_k
                    }]
            else:
                # Term search only
                search_body["search"] = query

            # Execute search
            search_user_token = await self._get_search_user_token_for_trimming()
            if search_user_token:
                logging.info("[Retrieval][Trimming] Using x-ms-query-source-authorization (OBO token acquired)")
            else:
                logging.info("[Retrieval][Trimming] Not sending x-ms-query-source-authorization")

            search_results = await self.search(
                index_name=self.index_name,
                body=search_body,
                search_user_token=search_user_token,
            )

            # Process search results
            results_list = []
            for rank, result in enumerate(search_results.get('value', [])):
                title = result.get('title', 'reference') or 'reference'
                link = result.get('filepath') or result.get('url', '') or ''
                content = result.get('content', '')
                source_selected = bool(content)
                if not source_selected:
                    AuditEmitter.default().emit_source(
                        selected=False,
                        source_type="azure_ai_search",
                        source_reference=link or str(result.get("chunk_id") or ""),
                        source_rank=rank,
                        reason_code=ReasonCode.SOURCE_EMPTY,
                    )

                # Prepend the formatted metadata block before content when enabled.
                # Read defensively: the field may be absent or null on a result.
                raw_metadata = None
                if self.include_metadata_in_context:
                    raw_metadata = result.get("custom_metadata") or []
                    metadata_block = format_custom_metadata(
                        raw_metadata,
                        self.metadata_max_chars,
                        self.metadata_allowed_keys,
                    )
                    if metadata_block:
                        content = f"{metadata_block}\n\n{content}"

                # Debug log each document with formatted output (remove line breaks)
                content_preview = content[:200] if len(content) > 200 else content
                content_preview = ' '.join(content_preview.split())  # Replace all whitespace/newlines with single space
                logging.debug(f"[Retrieval] Document: [{title}]({link}): {content_preview}")

                search_result = SearchResult(
                    title=title,
                    link=link,
                    content=content,
                    custom_metadata=raw_metadata,
                )
                results_list.append(search_result.model_dump())
                if source_selected:
                    AuditEmitter.default().emit_source(
                        selected=True,
                        source_type="azure_ai_search",
                        source_reference=link or str(result.get("chunk_id") or title),
                        source_rank=rank,
                        source_excerpt=content,
                    )

            # If we found results, force cache to non-empty so routing can recover
            # immediately from any stale empty-cache state.
            if results_list:
                _global_index_empty_cache[self.index_name] = {
                    "is_empty": False,
                    "checked_at": time.time(),
                }

            logging.info(f"[Retrieval] Found {len(results_list)} results from Azure AI Search")
            if not results_list:
                AuditEmitter.default().emit_source(
                    selected=False,
                    source_type="azure_ai_search",
                    reason_code=ReasonCode.SOURCE_EMPTY,
                )
            return json.dumps({"results": results_list, "query": query})

        except Exception as e:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="azure_ai_search",
                reason_code=ReasonCode.SOURCE_REJECTED,
            )
            level, marker = _classify_retrieval_error(e)
            logging.log(
                level,
                "%s Azure AI Search failed: %s",
                marker,
                type(e).__name__,
                extra={
                    "retrieval_index": self.index_name,
                    "retrieval_credential_type": "obo" if search_user_token else "managed_identity",
                },
            )

            # In strict mode (ALLOW_ANONYMOUS=false), do not silently degrade.
            # Raise so the request fails early and the logs make the root cause obvious.
            if not self._allow_anonymous:
                raise

            logging.warning("[Retrieval] Falling back to empty results (ALLOW_ANONYMOUS=true)")
            return json.dumps({"results": [], "query": query, "error": "search_failed"})


    async def _search_knowledge_base_foundry_iq(self, query: str) -> str:
        """Foundry IQ knowledge base retrieval, mapped to the AI Search contract.

        Delegates to :class:`FoundryIQClient` and maps the normalized
        ``{title, link, content}`` records into the exact same JSON contract
        ``single_agent_rag`` consumes from the Azure AI Search path. The OBO
        token (per-user document-level security) is acquired the same way as the
        AI Search path and forwarded in ``x-ms-query-source-authorization`` by
        the client. Conversation/security-field scoping via ``filterAddOn`` is a
        server-side knowledge source concern deferred to a later PR.
        """
        logging.info("[Retrieval] Using Foundry IQ knowledge base for document retrieval")
        search_user_token = None
        incoming_token = self._request_api_access_token
        allow_anonymous = self._allow_anonymous
        conversation_id = self._conversation_id
        user_context = dict(getattr(self, "_user_context", {}))
        mcp_enabled = False
        try:
            client = get_foundry_iq_client()
            mcp_enabled = bool(
                getattr(getattr(client, "mcp_config", None), "enabled", False)
            )
            if mcp_enabled:
                search_user_token = await acquire_obo_search_token(
                    incoming_token,
                    allow_anonymous=allow_anonymous,
                )
                records = await client.retrieve(
                    query,
                    obo_token=search_user_token,
                    incoming_token=incoming_token,
                    conversation_id=conversation_id,
                    user_context=user_context,
                )
            else:
                search_user_token = await self._get_search_user_token_for_trimming()
                records = await client.retrieve(
                    query,
                    obo_token=search_user_token,
                    conversation_id=conversation_id,
                    user_context=user_context,
                )

            results_list = []
            source_type = "foundry_iq_mcp" if mcp_enabled else "foundry_iq"
            for rank, record in enumerate(records[: self.search_top_k]):
                content = record.get("content") or ""
                if not content:
                    AuditEmitter.default().emit_source(
                        selected=False,
                        source_type=getattr(record, "source_type", source_type),
                        source_reference=record.get("link")
                        or record.get("title"),
                        source_rank=rank,
                        reason_code=ReasonCode.SOURCE_EMPTY,
                    )
                search_result = SearchResult(
                    title=record.get("title") or "reference",
                    link=record.get("link") or "",
                    content=content,
                )
                results_list.append(search_result.model_dump())
                if content:
                    AuditEmitter.default().emit_source(
                        selected=True,
                        source_type=getattr(record, "source_type", source_type),
                        source_reference=record.get("link")
                        or record.get("title"),
                        source_rank=rank,
                        source_excerpt=content,
                    )

            logging.info(f"[Retrieval] Found {len(results_list)} results from Foundry IQ")
            if not results_list:
                AuditEmitter.default().emit_source(
                    selected=False,
                    source_type=source_type,
                    reason_code=ReasonCode.SOURCE_EMPTY,
                )
            return json.dumps({"results": results_list, "query": query})

        except Exception as e:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="foundry_iq_mcp" if mcp_enabled else "foundry_iq",
                reason_code=ReasonCode.SOURCE_REJECTED,
            )
            level, marker = _classify_retrieval_error(e)
            logging.log(
                level,
                "%s Foundry IQ retrieval failed (%s)",
                marker,
                type(e).__name__,
                extra={
                    "retrieval_credential_type": "obo" if search_user_token else "managed_identity",
                },
            )
            if isinstance(e, McpConfigurationError):
                raise
            if mcp_enabled and isinstance(
                e, (McpConfigurationError, McpCredentialError, McpSourceError)
            ):
                raise
            if not allow_anonymous:
                raise
            logging.warning("[Retrieval] Falling back to empty results (ALLOW_ANONYMOUS=true)")
            return json.dumps({"results": [], "query": query, "error": "search_failed"})


    async def fetch_filepath_from_index(self, document_id: str) -> Optional[str]:
        """
        Fetch filepath directly from Azure AI Search index using document ID.

        Args:
            document_id: Document ID from Azure Search

        Returns:
            Filepath string from the index, or None if not found
        """
        try:
            logging.info("[Citations] 🔍 Fetching filepath from index for document_id: %s", document_id)

            document = await self.get_document(
                index_name=self.index_name,
                document_id=document_id,
                select_fields=['filepath', 'title']
            )

            if document:
                filepath = document.get('filepath')
                if filepath:
                    logging.info("[Citations] ✅ Found filepath in index: %s", filepath)
                    return filepath
                else:
                    logging.warning("[Citations] ⚠️ Document found but 'filepath' field is empty")
            else:
                logging.warning("[Citations] ⚠️ Document not found with ID: %s", document_id)

        except Exception as e:
            logging.error("[Citations] Error fetching document from index (%s)", type(e).__name__)

        return None


_search_client_instance = None

def get_search_client() -> SearchClient:
    """Returns a singleton SearchClient to reuse connections and config."""
    global _search_client_instance
    if _search_client_instance is None:
        _search_client_instance = SearchClient()
    return _search_client_instance
