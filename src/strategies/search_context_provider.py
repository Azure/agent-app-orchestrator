"""Search Context Provider with citation metadata.

Wraps Azure AI Search to return document content along with title and
filepath so the model can format proper ``[title](filepath)`` citations.
The UI resolves relative filepaths into time-limited SAS URLs.

Supports per-request OBO (On-Behalf-Of) permission trimming via the
``x_ms_query_source_authorization`` parameter natively provided by
the Azure Search SDK.
"""

import logging
import time
from collections.abc import Awaitable, Callable, MutableSequence
from typing import Any, Optional

from agent_framework import ChatMessage, Context, ContextProvider, Role
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery, QueryType, QueryCaptionType

from connectors.search import _classify_retrieval_error, build_conversation_filter
from connectors.obo import HostedRetrievalError, RetrievalAuthorizationMode, require_retrieval_token
from dependencies import get_config
from util.metadata import format_custom_metadata, parse_allowed_keys
from telemetry import AuditEmitter, ReasonCode
from .context_shaping import build_context_text, format_context_part
logger = logging.getLogger(__name__)


class SearchContextProvider(ContextProvider):
    """Azure AI Search context provider that includes citation metadata."""

    def __init__(
        self,
        *,
        endpoint: str,
        index_name: str,
        credential: AsyncTokenCredential,
        conversation_id: Optional[str] = None,
        top_k: int = 3,
        semantic_configuration_name: str | None = None,
        embed_fn: Callable[[str], Awaitable[list[float]]] | None = None,
        vector_field: str = "contentVector",
        max_content_chars: int = 1500,
        get_obo_token: Callable[[], Awaitable[Optional[str]]] | None = None,
        authorization_mode: RetrievalAuthorizationMode = RetrievalAuthorizationMode.USER_REQUIRED,
        owner_id: Optional[str] = None,
        native_permissions: bool = False,
    ) -> None:
        self._endpoint = endpoint
        self._index_name = index_name
        self._credential = credential
        self._conversation_id = (conversation_id or "").strip() or None
        self._owner_id = (owner_id or "").strip() or None
        self._top_k = top_k
        self._semantic_config = semantic_configuration_name
        self._embed_fn = embed_fn
        self._vector_field = vector_field
        self._max_content_chars = max_content_chars
        self._get_obo_token = get_obo_token
        self._authorization_mode = authorization_mode
        self._native_permissions = native_permissions

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def invoking(
        self,
        messages: ChatMessage | MutableSequence[ChatMessage],
        **kwargs: Any,
    ) -> Context:
        msgs = [messages] if isinstance(messages, ChatMessage) else list(messages)
        user_texts = [
            m.text for m in msgs
            if m and m.text and m.text.strip() and m.role == Role.USER
        ]
        if not user_texts:
            return Context()

        query = user_texts[-1]

        search_start = time.time()
        logger.info("[SearchContextProvider] Query: %r (top_k=%d, hybrid=%s)", query[:120], self._top_k, bool(self._embed_fn))

        # Custom metadata in context (default OFF). Only select the field when
        # enabled so pre-#487 indexes (which lack it) are not 400'd by Search.
        cfg = get_config()
        include_metadata = cfg.get("SEARCH_INCLUDE_METADATA_IN_CONTEXT", False, type=bool)
        metadata_max_chars = int(cfg.get("SEARCH_METADATA_MAX_CHARS", 500, type=int))
        metadata_allowed_keys = parse_allowed_keys(cfg.get("SEARCH_METADATA_ALLOWED_KEYS", "", type=str))

        select_fields = ["id", "content", "title", "filepath", "url"]
        if include_metadata:
            select_fields = select_fields + ["custom_metadata"]

        search_params: dict[str, Any] = {
            "search_text": query,
            "top": self._top_k,
            "select": select_fields,
        }

        # Conversation scoping: only this caller's chunks in this conversation
        # plus shared/global chunks.
        search_params["filter"] = build_conversation_filter(
            self._conversation_id, owner_id=self._owner_id, field_name="conversationId",
            native_permissions=self._native_permissions,
        )

        # Hybrid search: add vector query when embedding function is available
        if self._embed_fn:
            try:
                embed_start = time.time()
                vector = await self._embed_fn(query)
                search_params["vector_queries"] = [
                    VectorizedQuery(
                        vector=vector,
                        k=self._top_k,
                        fields=self._vector_field,
                    )
                ]
                logger.info("[SearchContextProvider] Embedding generated in %.2fs (dims=%d)", time.time() - embed_start, len(vector))
            except Exception as e:
                logger.warning("[SearchContextProvider] Embedding failed, falling back to keyword search: %s", type(e).__name__)

        if self._semantic_config:
            search_params["query_type"] = QueryType.SEMANTIC
            search_params["semantic_configuration_name"] = self._semantic_config
            search_params["query_caption"] = QueryCaptionType.EXTRACTIVE

        try:
            # Acquire OBO token for permission trimming if configured
            obo_token: Optional[str] = None
            obo_token = await require_retrieval_token(self._authorization_mode, self._get_obo_token)

            if obo_token:
                search_params["x_ms_query_source_authorization"] = obo_token
                logger.info("[SearchContextProvider] Using x-ms-query-source-authorization (OBO)")
            else:
                logger.info("[SearchContextProvider] Not sending x-ms-query-source-authorization")

            async with SearchClient(
                endpoint=self._endpoint,
                index_name=self._index_name,
                credential=self._credential,
            ) as client:
                results = await client.search(**search_params)

                parts: list[str] = []
                rank = 0
                async for doc in results:
                    current_rank = rank
                    rank += 1
                    title = doc.get("title") or doc.get("filepath") or doc.get("id") or "Unknown"
                    link = doc.get("filepath") or doc.get("url") or ""
                    content = doc.get("content") or ""
                    if not content:
                        AuditEmitter.default().emit_source(
                            selected=False,
                            source_type="azure_ai_search",
                            source_reference=link or str(doc.get("id") or ""),
                            source_rank=current_rank,
                            reason_code=ReasonCode.SOURCE_EMPTY,
                        )
                        continue
                    if len(content) > self._max_content_chars:
                        content = content[:self._max_content_chars] + "..."
                    if include_metadata:
                        metadata_block = format_custom_metadata(
                            doc.get("custom_metadata") or [],
                            metadata_max_chars,
                            metadata_allowed_keys,
                        )
                        if metadata_block:
                            content = f"{metadata_block}\n\n{content}"
                    parts.append(format_context_part(title, link, content))
                    AuditEmitter.default().emit_source(
                        selected=True,
                        source_type="azure_ai_search",
                        source_reference=link or str(doc.get("id") or title),
                        source_rank=current_rank,
                        source_excerpt=content,
                    )
        except Exception as e:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="azure_ai_search",
                reason_code=ReasonCode.SOURCE_REJECTED,
            )
            level, marker = _classify_retrieval_error(e)
            logger.log(
                level,
                "%s Search failed in %.2fs: %s",
                marker,
                time.time() - search_start,
                type(e).__name__,
                extra={
                    "retrieval_index": self._index_name,
                    "retrieval_credential_type": "obo" if obo_token else "managed_identity",
                },
            )
            if self._native_permissions:
                if isinstance(e, HostedRetrievalError):
                    raise
                if isinstance(e, HttpResponseError) and e.response is not None:
                    auth_error = HostedRetrievalError.from_response(
                        e.status_code, e.response.headers.items(),
                    )
                    if auth_error is not None:
                        raise auth_error from None
                raise RuntimeError("Protected Search retrieval failed") from None
            raise

        logger.info("[SearchContextProvider] Search returned %d documents in %.2fs", len(parts), time.time() - search_start)

        if not parts:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="azure_ai_search",
                reason_code=ReasonCode.SOURCE_EMPTY,
            )
            return Context()

        context_text = build_context_text(parts)
        return Context(messages=[ChatMessage(role=Role.SYSTEM, text=context_text)])
