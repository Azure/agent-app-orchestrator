"""Foundry IQ context provider with citation metadata.

The Foundry IQ counterpart to :class:`SearchContextProvider`. It retrieves
documents from a Foundry IQ knowledge base (via :class:`FoundryIQClient`) and
emits them into the prompt using the shared context-shaping helpers so the
context is byte-identical to the Azure AI Search path. Downstream citation
behavior therefore does not change with the selected ``RETRIEVAL_BACKEND``.

Per-request OBO permission trimming is forwarded by :class:`FoundryIQClient` in
the ``x-ms-query-source-authorization`` header. That OBO header is a distinct
mechanism from the Pattern B ``filterAddOn`` security filter, which narrows a
registered Agent Landing Zone Azure AI Search index using its custom security fields.

Introduced for Azure/agent-landing-zone#526.
"""

import logging
import time
from collections.abc import Awaitable, Callable, MutableSequence
from typing import Any, Mapping, Optional

from agent_framework import ChatMessage, Context, ContextProvider, Role

from connectors.foundry_iq import get_foundry_iq_client
from connectors.obo import RetrievalAuthorizationMode, require_retrieval_token, resolve_retrieval_authorization
from connectors.search import _classify_retrieval_error
from telemetry import AuditEmitter, ReasonCode
from util.blob_sas import sign_blob_url
from .context_shaping import build_context_text, format_context_part

logger = logging.getLogger(__name__)


class FoundryIQContextProvider(ContextProvider):
    """Foundry IQ knowledge base context provider with citation metadata."""

    def __init__(
        self,
        *,
        conversation_id: Optional[str] = None,
        top_k: int = 3,
        max_content_chars: int = 1500,
        get_obo_token: Callable[[], Awaitable[Optional[str]]] | None = None,
        authorization_mode: RetrievalAuthorizationMode = RetrievalAuthorizationMode.USER_REQUIRED,
        request_access_token: Optional[str] = None,
        allow_anonymous: bool = True,
        mcp_enabled: bool = False,
        user_context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._conversation_id = (conversation_id or "").strip() or None
        self._top_k = top_k
        self._max_content_chars = max_content_chars
        self._get_obo_token = get_obo_token
        self._authorization_mode = authorization_mode
        self._request_access_token = request_access_token
        self._allow_anonymous = allow_anonymous
        self._mcp_enabled = mcp_enabled
        self._user_context = dict(user_context or {})

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
        logger.info("[FoundryIQContextProvider] Query: %r (top_k=%d)", query[:120], self._top_k)

        obo_token: Optional[str] = None
        mcp_enabled = self._mcp_enabled
        try:
            client = get_foundry_iq_client()
            client_mcp_config = getattr(client, "mcp_config", None)
            if client_mcp_config is not None:
                mcp_enabled = bool(
                    getattr(client_mcp_config, "enabled", self._mcp_enabled)
                )

            # Remote and mixed sources must not silently drop user-only members.
            source_requires_user = any(
                getattr(client, name, False) is True
                for name in (
                    "work_iq_enabled", "fabric_iq_enabled",
                    "fabric_data_agent_enabled", "sharepoint_remote_enabled",
                )
            )
            if mcp_enabled and client_mcp_config is not None:
                source_requires_user = source_requires_user or any(
                    header.value_from.kind == "obo"
                    for source in client_mcp_config.sources
                    for header in source.query_headers
                )
            required_mode = resolve_retrieval_authorization(
                self._request_access_token, self._allow_anonymous, source_requires_user,
            )
            if not isinstance(self._authorization_mode, RetrievalAuthorizationMode):
                raise ValueError("Invalid retrieval authorization mode")
            mode = (
                RetrievalAuthorizationMode.USER_REQUIRED
                if required_mode is RetrievalAuthorizationMode.USER_REQUIRED
                else self._authorization_mode
            )
            obo_token = await require_retrieval_token(mode, self._get_obo_token)

            retrieve_kwargs: dict[str, Any] = {
                "obo_token": obo_token,
                "conversation_id": self._conversation_id,
                "user_context": self._user_context,
            }
            if mcp_enabled:
                retrieve_kwargs["incoming_token"] = self._request_access_token
            records = await client.retrieve(query, **retrieve_kwargs)

            parts: list[str] = []
            for rank, record in enumerate(records[: self._top_k]):
                title = record.get("title") or "reference"
                link = record.get("link") or ""
                content = record.get("content") or ""
                if not content:
                    continue
                if len(content) > self._max_content_chars:
                    content = content[: self._max_content_chars] + "..."
                # The model copies this href verbatim into its citation, and
                # surfaces such as the Foundry Playground render that markdown
                # with no opportunity to rewrite it. Sign the link now so the
                # citation resolves without public blob access. The audit trail
                # below keeps the unsigned link so no SAS token reaches
                # telemetry.
                citation_link = await sign_blob_url(link) if link else link
                parts.append(format_context_part(title, citation_link, content))
                AuditEmitter.default().emit_source(
                    selected=True,
                    source_type=getattr(
                        record,
                        "source_type",
                        "foundry_iq_mcp" if mcp_enabled else "foundry_iq",
                    ),
                    source_reference=link or title,
                    source_rank=rank,
                    source_excerpt=content,
                )
        except Exception as e:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="foundry_iq_mcp" if mcp_enabled else "foundry_iq",
                reason_code=ReasonCode.SOURCE_REJECTED,
            )
            level, marker = _classify_retrieval_error(e)
            logger.log(
                level,
                "%s Foundry IQ retrieval failed in %.2fs: %s",
                marker,
                time.time() - search_start,
                type(e).__name__,
                extra={
                    "retrieval_credential_type": "obo" if obo_token else "managed_identity",
                },
            )
            raise

        logger.info(
            "[FoundryIQContextProvider] Retrieval returned %d documents in %.2fs",
            len(parts),
            time.time() - search_start,
        )

        if not parts:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="foundry_iq_mcp" if mcp_enabled else "foundry_iq",
                reason_code=ReasonCode.SOURCE_EMPTY,
            )
            return Context()

        context_text = build_context_text(parts)
        return Context(messages=[ChatMessage(role=Role.SYSTEM, text=context_text)])
