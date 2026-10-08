"""
Multimodal RAG Strategy.

This strategy extends the MAF Lite approach with multimodal capabilities:
- Retrieves both text documents and related images from Azure AI Search
- Downloads images from Azure Blob Storage
- Sends multimodal content (text + images) to a vision-capable model (e.g. GPT-4o)
- Preserves ordinary history; automatic profile access/extraction is suspended
- Uses dual vector search (contentVector + captionVector)
"""

import asyncio
import functools
import logging
import re
import time
from collections.abc import Mapping
from typing import Any, Optional

# Suppress Azure SDK HTTP logging BEFORE importing azure packages
for _azure_logger in [
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.core",
    "azure",
]:
    _logger = logging.getLogger(_azure_logger)
    _logger.setLevel(logging.CRITICAL)
    _logger.propagate = False
    _logger.disabled = True
    _logger.handlers.clear()

from agent_framework import ChatAgent, ChatMessage

from .base_agent_strategy import BaseAgentStrategy
from .agent_strategies import AgentStrategies
from .composite_context_provider import CompositeContextProvider
from .multimodal_search_context_provider import MultimodalSearchContextProvider
from .foundry_iq_context_provider import FoundryIQContextProvider
from .maf_plugins import UserProfile, UserProfileMemory
from .retrieval_intent import (
    RETRIEVAL_INTENT_SYSTEM_PROMPT,
    build_retrieval_intent_messages,
    parse_retrieval_intent,
)
from connectors.foundry_iq_mcp import is_mcp_enabled
from connectors.multimodal_chat_client import MultimodalChatClient
from connectors.search import acquire_obo_search_token
from connectors.obo import resolve_retrieval_authorization
from util.retrieval_backend import get_retrieval_backend, RETRIEVAL_BACKEND_FOUNDRY_IQ
from dependencies import get_config
from openai import BadRequestError


def _resolve_conversation_owner_id(user_context) -> Optional[str]:
    """Return the server-derived principal that owns conversation-scoped chunks."""
    if not isinstance(user_context, Mapping):
        return None
    for key in ("principal_id", "oid"):
        value = user_context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _scoped_search_provider_factory(owner_id: Optional[str]):
    """Bind the conversation owner to the search provider constructor."""
    return functools.partial(MultimodalSearchContextProvider, owner_id=owner_id)


_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
_IMAGE_CLASSIFIER_PROMPT = (
    "You are filtering retrieved document images before they reach the answer model. "
    "Return exactly KEEP or SKIP. Trust the actual pixels more than the metadata. "
    "KEEP only if the image clearly shows mechanical parts, procedural diagrams, exploded views, measurements, "
    "tool usage, assembly steps, cross-sections, or other repair-relevant content for the user's request. "
    "SKIP if it is a boot, shoe, decorative art, mascot, cartoon, logo, chapter divider, header/footer, splash screen, "
    "system-output screen, unrelated scenery, or generic non-procedural illustration."
)


def _dedup_markdown_images(text: str) -> str:
    """Remove duplicate ![alt](path) references, keeping only the first occurrence of each path."""
    seen: set[str] = set()

    def _replacer(m: re.Match) -> str:
        path = m.group(2)
        if path in seen:
            return ""
        seen.add(path)
        return m.group(0)

    return _IMAGE_RE.sub(_replacer, text)


class MultimodalStrategy(BaseAgentStrategy):
    """Agent strategy with multimodal RAG (text + images).

    Based on :class:`MafLiteStrategy` but uses :class:`MultimodalChatClient`
    and :class:`MultimodalSearchContextProvider` to support vision-capable
    models analysing images retrieved alongside text documents.
    """

    _INTENT_SYSTEM_PROMPT = RETRIEVAL_INTENT_SYSTEM_PROMPT

    AGENT_INSTRUCTIONS = (
        "You are a helpful AI assistant with vision capabilities. "
        "Your role is to assist users with their questions and tasks, "
        "analysing both text and images when available.\n\n"
        "Your capabilities:\n"
        "1. **Conversation**: Engage in helpful, informative conversations\n"
        "2. **Profile Awareness**: Remember user information to provide personalized assistance\n"
        "3. **Knowledge Search**: Search your knowledge base for text and images\n"
        "4. **Image Analysis**: Analyse diagrams, charts, and figures from documents\n\n"
        "Guidelines:\n"
        "- Provide clear, helpful, and accurate responses\n"
        "- When images are available, describe relevant visual information\n"
        "- Ask clarifying questions when needed\n"
        "- Be concise but thorough in your explanations"
    )

    def __init__(self):
        super().__init__()
        logging.debug("[MultimodalStrategy] Initializing...")

        cfg = get_config()
        self.strategy_type = AgentStrategies.MULTIMODAL
        self.conversation_id: Optional[str] = None

        if not hasattr(self, "credential") or self.credential is None:
            self.credential = cfg.aiocredential
            logging.debug("[MultimodalStrategy] Using credential from AppConfigClient")

        # Sync credential for OpenAI client (get_bearer_token_provider is sync)
        self._sync_credential = cfg.credential

        # Model endpoint — needed for direct OpenAI calls
        self.model_endpoint = cfg.get("AI_FOUNDRY_ACCOUNT_ENDPOINT")
        self.openai_api_version = cfg.get("OPENAI_API_VERSION", "2025-04-01-preview")

        # Azure AI Search configuration for retrieval
        self.search_endpoint = cfg.get_value("SEARCH_SERVICE_QUERY_ENDPOINT", allow_none=True)
        self.search_index_name = cfg.get_value("SEARCH_RAG_INDEX_NAME", allow_none=True)
        self.search_top_k = int(cfg.get("SEARCH_RAGINDEX_TOP_K", 3))
        self.semantic_search_config = cfg.get_value("SEARCH_SEMANTIC_SEARCH_CONFIG", allow_none=True)

        # Embedding configuration for hybrid (keyword + vector) search
        self.embedding_deployment = cfg.get_value("EMBEDDING_DEPLOYMENT_NAME", allow_none=True)

        # Multimodal-specific: max images per request and per document
        self.max_images = int(cfg.get("MULTIMODAL_MAX_IMAGES", 10))
        self.max_images_per_doc = int(cfg.get("MULTIMODAL_MAX_IMAGES_PER_DOC", 5))
        self.max_content_chars = int(cfg.get("MULTIMODAL_MAX_CONTENT_CHARS", 4000))
        self.classify_images = cfg.get("MULTIMODAL_CLASSIFY_IMAGES", True, type=bool)
        self.image_classification_timeout_seconds = int(
            cfg.get("MULTIMODAL_IMAGE_CLASSIFICATION_TIMEOUT_SECONDS", 15)
        )
        self.image_classification_concurrency = int(
            cfg.get("MULTIMODAL_IMAGE_CLASSIFICATION_CONCURRENCY", 2)
        )
        self.validate_response_images = cfg.get("MULTIMODAL_VALIDATE_RESPONSE_IMAGES", True, type=bool)
        self.image_validation_timeout_seconds = int(
            cfg.get("MULTIMODAL_IMAGE_VALIDATION_TIMEOUT_SECONDS", 15)
        )

        # User profiles stored in the conversations container
        self.user_profile_container = cfg.get("CONVERSATIONS_DATABASE_CONTAINER", "conversations")

        # History window size (number of recent messages sent to the model)
        self.history_max_messages = int(cfg.get("CHAT_HISTORY_MAX_MESSAGES", 10))

        # Small bounded context used only by the retrieval-needed classifier.
        self.intent_history_max_messages = int(cfg.get("RETRIEVAL_INTENT_HISTORY_MESSAGES", 4))
        self.intent_history_max_chars = int(cfg.get("RETRIEVAL_INTENT_HISTORY_MAX_CHARS", 4000))
        self.no_retrieval_followup_detection_enabled = cfg.get(
            "ENABLE_NO_RETRIEVAL_FOLLOWUP_DETECTION",
            True,
            type=bool,
        )

        # Hard cap on output tokens for the main agent response
        self.max_completion_tokens = int(cfg.get("MAX_COMPLETION_TOKENS", 4096))

        # Reasoning effort for models that support it (e.g. gpt-5-mini)
        self.reasoning_effort = cfg.get("REASONING_EFFORT", "medium")

        # Runtime state
        self._chat_client: Optional[MultimodalChatClient] = None
        self._user_memory: Optional[UserProfileMemory] = None
        self._search_provider: Optional[MultimodalSearchContextProvider] = None
        self._cached_instructions: Optional[str] = None

        logging.debug("[MultimodalStrategy] Initialized")

    def set_context(self, conversation_id: Optional[str]) -> None:
        """Set conversation_id (may be None before orchestrator assigns a new id)."""
        self.conversation_id = conversation_id

    # ------------------------------------------------------------------
    # Prompt namespace — uses multimodal prompt directory
    # ------------------------------------------------------------------
    def _prompt_namespace(self) -> str:
        return "multimodal"

    # ------------------------------------------------------------------
    # Client helpers
    # ------------------------------------------------------------------
    def _get_or_create_chat_client(self) -> MultimodalChatClient:
        if self._chat_client is None:
            logging.debug(
                "[MultimodalStrategy] Creating MultimodalChatClient "
                f"endpoint={self.model_endpoint} model={self.model_name}"
            )
            self._chat_client = MultimodalChatClient(
                azure_endpoint=self.model_endpoint,
                model_deployment_name=self.model_name,
                credential=self._sync_credential,
                api_version=self.openai_api_version,
            )
        return self._chat_client

    # ------------------------------------------------------------------
    # User profile persistence
    # ------------------------------------------------------------------
    async def _load_user_profile(self, user_id: str) -> UserProfile:
        profile_key = f"user_profile_{user_id}"
        try:
            doc = await self.cosmos.get_document(self.user_profile_container, profile_key)
            if doc and "profile_data" in doc:
                return UserProfile.model_validate_json(doc["profile_data"])
        except Exception as e:
            logging.debug("[MultimodalStrategy] User profile unavailable (%s)", type(e).__name__)
        return UserProfile()

    async def _save_user_profile(self, user_id: str, profile: UserProfile):
        profile_key = f"user_profile_{user_id}"
        try:
            doc = {
                "id": profile_key,
                "profile_data": profile.model_dump_json(),
                "updated_at": time.time(),
            }
            existing = await self.cosmos.get_document(self.user_profile_container, profile_key)
            if existing:
                saved_doc = await self.cosmos.update_document(self.user_profile_container, doc)
            else:
                saved_doc = await self.cosmos.create_document(self.user_profile_container, profile_key, body=doc)
            if saved_doc is None:
                logging.warning("[MultimodalStrategy] User profile write not confirmed for %s", user_id)
            else:
                logging.info("[MultimodalStrategy] Saved user profile for %s", user_id)
        except Exception as e:
            logging.error("[MultimodalStrategy] Failed to save user profile (%s)", type(e).__name__)

    # ------------------------------------------------------------------
    # Search provider (multimodal retrieval)
    # ------------------------------------------------------------------
    async def _create_search_provider(self) -> Optional[MultimodalSearchContextProvider]:
        if not self.search_endpoint:
            logging.debug("[MultimodalStrategy] No search endpoint configured, skipping search")
            return None
        if not self.search_index_name:
            logging.warning("[MultimodalStrategy] No search index name configured, skipping search")
            return None

        MultimodalSearchContextProvider = _scoped_search_provider_factory(
            _resolve_conversation_owner_id(self.user_context)
        )
        try:
            allow_anonymous = self.cfg.get(
                "ALLOW_ANONYMOUS", default=True, type=bool
            )
            retrieval_backend = get_retrieval_backend()
            token = getattr(self, "request_access_token", None)
            authorization_mode = resolve_retrieval_authorization(token, allow_anonymous)
            mcp_enabled = (
                retrieval_backend == RETRIEVAL_BACKEND_FOUNDRY_IQ
                and is_mcp_enabled(
                    self.cfg.get(
                        "FOUNDRY_IQ_MCP_ENABLED", default=False, type=bool
                    )
                )
            )
            # Build async embed function for hybrid search
            embed_fn = None
            if self.embedding_deployment:
                chat_client = self._get_or_create_chat_client()
                async def _embed(text: str) -> list[float]:
                    resp = await chat_client._client.embeddings.create(
                        input=text, model=self.embedding_deployment
                    )
                    return resp.data[0].embedding
                embed_fn = _embed

            async def _get_obo_token() -> str | None:
                return (
                    await acquire_obo_search_token(
                        token,
                        allow_anonymous=False,
                    )
                    if token
                    else None
                )

            # Decision #5 (Azure/agent-landing-zone#526): multimodal stays on Azure AI Search
            # for v3.0.0 — Pattern A captioning/image parity for Foundry IQ is
            # deferred. The selector is still honored for consistency: when
            # foundry_iq is configured, retrieval is text-only via
            # FoundryIQContextProvider (no image grounding).
            if retrieval_backend == RETRIEVAL_BACKEND_FOUNDRY_IQ:
                provider = FoundryIQContextProvider(
                    conversation_id=self.conversation_id,
                    top_k=self.search_top_k,
                    max_content_chars=self.max_content_chars,
                    get_obo_token=_get_obo_token,
                    authorization_mode=authorization_mode,
                    request_access_token=(
                        getattr(self, "request_access_token", None)
                        if mcp_enabled
                        else None
                    ),
                    allow_anonymous=allow_anonymous,
                    mcp_enabled=mcp_enabled,
                    user_context=self.user_context,
                )
                logging.info(
                    "[MultimodalStrategy] FoundryIQContextProvider created "
                    "(top_k=%d, text-only; Pattern A image parity deferred)",
                    self.search_top_k,
                )
                return provider

            provider = MultimodalSearchContextProvider(
                endpoint=self.search_endpoint,
                credential=self.credential,
                blob_credential=self.credential,
                conversation_id=self.conversation_id,
                index_name=self.search_index_name,
                top_k=self.search_top_k,
                max_images=self.max_images,
                max_images_per_doc=self.max_images_per_doc,
                max_content_chars=self.max_content_chars,
                semantic_configuration_name=self.semantic_search_config,
                embed_fn=embed_fn,
                get_obo_token=_get_obo_token,
                authorization_mode=authorization_mode,
                classify_images_fn=self._classify_image_relevance if self.classify_images else None,
                classify_images_concurrency=self.image_classification_concurrency,
            )
            logging.info(
                "[MultimodalStrategy] MultimodalSearchContextProvider created "
                "(index=%s, top_k=%d, max_images=%d)",
                self.search_index_name, self.search_top_k, self.max_images,
            )
            return provider
        except Exception as e:
            logging.error("[MultimodalStrategy] Failed to create search provider (%s)", type(e).__name__)
            raise

    async def _classify_image_relevance(self, candidate: dict[str, Any]) -> bool:
        client = self._get_or_create_chat_client()
        figure_path = candidate.get("fig_path", "")
        caption = candidate.get("caption") or "(none)"
        local_text = candidate.get("local_text") or "(none)"
        query = candidate.get("query") or ""
        image_base64 = candidate.get("image_base64") or ""
        image_url = image_base64
        if image_base64 and not image_base64.startswith("data:"):
            image_url = f"data:image/png;base64,{image_base64}"

        _kwargs = dict(
            model=self.model_name,
            messages=[
                {"role": "system", "content": _IMAGE_CLASSIFIER_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"User request: {query}\n"
                                f"Figure path: {figure_path}\n"
                                f"Caption: {caption}\n"
                                f"Nearby text: {local_text}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url,
                                "detail": "auto",
                            },
                        },
                    ],
                },
            ],
            max_completion_tokens=200,
        )
        try:
            response = await asyncio.wait_for(
                client._client.chat.completions.create(**_kwargs),
                timeout=self.image_classification_timeout_seconds,
            )
        except BadRequestError:
            _kwargs.pop("max_completion_tokens", None)
            _kwargs["max_tokens"] = 200
            response = await asyncio.wait_for(
                client._client.chat.completions.create(**_kwargs),
                timeout=self.image_classification_timeout_seconds,
            )
        result = (response.choices[0].message.content or "").strip().upper()
        finish = getattr(response.choices[0], "finish_reason", "?")
        tokens = getattr(getattr(response, "usage", None), "completion_tokens", "?")
        logging.info(
            "[MultimodalStrategy] image_classifier fig=%s result=%r finish=%s tokens=%s",
            figure_path, result, finish, tokens,
        )
        # Fail-closed: skip images whose classification is empty or ambiguous
        if not result:
            return False
        return result.startswith("KEEP")

    # ------------------------------------------------------------------
    # Post-response image validation guardrail
    # ------------------------------------------------------------------
    _IMAGE_VALIDATION_PROMPT = (
        "You are a post-response image guardrail. Return exactly VALID or INVALID. "
        "Do not explain, describe, or answer the user's question. "
        "VALID only if the image clearly shows technical content such as mechanical parts, "
        "procedural diagrams, exploded views, measurements, tool usage, cross-sections, "
        "wiring, specifications, or assembly steps. "
        "INVALID if it is a cartoon, decorative artwork, book filler, icon, logo, "
        "chapter divider, header/footer art, mascot, boot/shoe, generic scenery, "
        "or any non-technical decoration."
    )

    async def _validate_response_images(self, response_text: str, query: str) -> str:
        """Strip images the model embedded that are decorative or irrelevant."""
        matches = list(_IMAGE_RE.finditer(response_text))
        if not matches:
            return response_text

        image_data = getattr(self._search_provider, "image_data", {}) or {}
        client = self._get_or_create_chat_client()
        semaphore = asyncio.Semaphore(self.image_classification_concurrency)
        invalid_paths: set[str] = set()

        async def _validate_one(path: str) -> None:
            b64 = image_data.get(path)
            if not b64:
                # No base64 available — cannot validate, strip to be safe
                invalid_paths.add(path)
                logging.warning(
                    "[MultimodalStrategy] image_validation result=NO_DATA (stripping)",
                )
                return

            image_url = b64 if b64.startswith("data:") else f"data:image/png;base64,{b64}"
            async with semaphore:
                try:
                    _kwargs = dict(
                        model=self.model_name,
                        messages=[
                            {"role": "system", "content": self._IMAGE_VALIDATION_PROMPT},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": f"Answer topic: {query}\nClassify this image: VALID or INVALID."},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": image_url, "detail": "auto"},
                                    },
                                ],
                            },
                        ],
                        max_completion_tokens=10,
                    )
                    try:
                        resp = await asyncio.wait_for(
                            client._client.chat.completions.create(**_kwargs),
                            timeout=self.image_validation_timeout_seconds,
                        )
                    except BadRequestError:
                        _kwargs.pop("max_completion_tokens", None)
                        _kwargs["max_tokens"] = 10
                        resp = await asyncio.wait_for(
                            client._client.chat.completions.create(**_kwargs),
                            timeout=self.image_validation_timeout_seconds,
                        )
                    result = (resp.choices[0].message.content or "").strip().upper()
                    logging.info(
                        "[MultimodalStrategy] image_validation valid=%s",
                        bool(result and result.startswith("VALID")),
                    )
                    # Fail-closed: anything other than explicit VALID is stripped
                    if not result or not result.startswith("VALID"):
                        invalid_paths.add(path)
                except Exception as e:
                    logging.warning(
                        "[MultimodalStrategy] image_validation failed (%s); stripping",
                        type(e).__name__,
                    )
                    invalid_paths.add(path)

        unique_paths = {m.group(2) for m in matches}
        await asyncio.gather(*(_validate_one(p) for p in unique_paths))

        if invalid_paths:
            def _strip_invalid(m: re.Match) -> str:
                return "" if m.group(2) in invalid_paths else m.group(0)
            response_text = _IMAGE_RE.sub(_strip_invalid, response_text)

        logging.info(
            "[MultimodalStrategy] image_validation_summary: validated=%d stripped=%d",
            len(unique_paths), len(invalid_paths),
        )
        return response_text

    # ------------------------------------------------------------------
    # Session summary
    # ------------------------------------------------------------------
    def _build_session_summary(self) -> str:
        return "Automatic profile personalization is disabled."

    # ------------------------------------------------------------------
    # Intent classification (LLM-based)
    # ------------------------------------------------------------------
    async def _classify_intent(self, user_message: str, history: list[dict] | None = None) -> str:
        """Classify whether the turn needs retrieval using a lightweight LLM call."""
        try:
            client = self._get_or_create_chat_client()
            messages = build_retrieval_intent_messages(
                user_message=user_message,
                history=history,
                max_history_messages=self.intent_history_max_messages,
                max_history_chars=self.intent_history_max_chars,
            )
            _kwargs = dict(
                model=self.model_name,
                messages=messages,
                max_completion_tokens=200,
            )
            try:
                resp = await client._client.chat.completions.create(**_kwargs)
            except BadRequestError:
                _kwargs.pop("max_completion_tokens", None)
                _kwargs["max_tokens"] = 200
                resp = await client._client.chat.completions.create(**_kwargs)
            result = (resp.choices[0].message.content or "").strip().upper()
            intent = parse_retrieval_intent(
                result,
                enable_no_retrieval=self.no_retrieval_followup_detection_enabled,
            )
            logging.info("[MultimodalStrategy] intent=%s (raw=%r)", intent, result)
            return intent
        except Exception as e:
            logging.warning("[MultimodalStrategy] Intent classification failed (%s); defaulting to question", type(e).__name__)
            return "question"

    # ------------------------------------------------------------------
    # Main agent flow
    # ------------------------------------------------------------------
    async def initiate_agent_flow(self, user_message: str):
        """Run the multimodal conversational agent with vision support."""
        flow_start = time.time()
        logging.info("[MultimodalStrategy] === Flow start === question=%r", user_message[:120])

        conv = self.conversation
        is_new_session = not conv.get("session_initialized", False)
        self._eligible_profile_user_id()

        chat_client = self._get_or_create_chat_client()

        # No legacy-key binding is trusted for automatic personalization.
        self._user_memory = None

        history = conv.get("messages", [])

        # Classify intent — skip search when retrieval is not needed.
        t0 = time.time()
        intent = await self._classify_intent(user_message, history=history)
        logging.info("[MultimodalStrategy] intent_classification: %.2fs", time.time() - t0)

        # Only initialize retrieval for turns that need it.
        if intent == "question" and self._search_provider is None:
            t0 = time.time()
            self._search_provider = await self._create_search_provider()
            logging.info(
                "[MultimodalStrategy] search_provider_init: %.2fs (hybrid=%s)",
                time.time() - t0, bool(self.embedding_deployment),
            )

        # Build context providers
        context_providers = [self._user_memory] if self._user_memory is not None else []
        if intent == "question" and self._search_provider:
            context_providers.append(self._search_provider)
        elif intent == "greeting":
            logging.info("[MultimodalStrategy] Greeting detected — skipping search")
        elif intent == "no_retrieval":
            logging.info("[MultimodalStrategy] No-retrieval follow-up detected — skipping search")
        else:
            logging.warning("[MultimodalStrategy] No search provider — agent will answer without grounding")
        logging.info("[MultimodalStrategy] context_providers: %d", len(context_providers))

        # Read base instructions (cached after first read)
        if self._cached_instructions is None:
            base_instructions = await self._read_prompt("main")
            self._cached_instructions = base_instructions if base_instructions else self.AGENT_INSTRUCTIONS
        instructions = self._cached_instructions

        # Create agent and stream
        async with ChatAgent(
            chat_client=chat_client,
            instructions=instructions,
            context_provider=CompositeContextProvider(
                context_providers,
                required_providers=[self._search_provider] if intent == "question" and self._search_provider else [],
            ),
        ) as agent:

            thread = agent.get_new_thread()

            # Session welcome with existing profile
            if (
                is_new_session
                and self._user_memory is not None
                and self._user_memory.has_minimum_context()
            ):
                conv["session_initialized"] = True
                session_summary = self._build_session_summary()
                yield f"Welcome back! Here's what I remember:\n\n{session_summary}\n\n---\n\n"
            elif is_new_session:
                conv["session_initialized"] = True

            # Build message list with conversation history
            input_messages: list[ChatMessage] = []
            for msg in history[-self.history_max_messages:]:
                role = msg.get("role", "user")
                text = msg.get("text") or msg.get("content") or ""
                if text:
                    # Strip image markdown from assistant history so old
                    # ![...](path) references don't act as few-shot examples
                    # that teach the model to mention figures by name only
                    # (without embedding them), and to prevent stale image
                    # paths that aren't in the current search context.
                    if role == "assistant":
                        text = _IMAGE_RE.sub("", text)
                        text = re.sub(r'\n{3,}', '\n\n', text).strip()
                    if text:
                        input_messages.append(ChatMessage(role=role, text=text))
            input_messages.append(ChatMessage(role="user", text=user_message))
            logging.info(
                "[MultimodalStrategy] history_messages: %d (total input: %d)",
                len(history), len(input_messages),
            )

            # Buffer the full response so we can post-process before yielding.
            # This ensures duplicate image references are removed regardless
            # of model instruction-following reliability.
            stream_start = time.time()
            full_response = ""
            async for chunk in agent.run_stream(
                input_messages,
                thread=thread,
                options={"max_completion_tokens": self.max_completion_tokens, "reasoning_effort": self.reasoning_effort},
            ):
                if chunk.text:
                    full_response += chunk.text
            logging.info(
                "[MultimodalStrategy] agent_stream: %.2fs (response_len=%d)",
                time.time() - stream_start, len(full_response),
            )

            # Post-process: remove duplicate ![...]() image references (keep first)
            full_response = _dedup_markdown_images(full_response)

            # Post-response guardrail: validate each embedded image
            if self.validate_response_images and "![" in full_response:
                t0 = time.time()
                full_response = await self._validate_response_images(full_response, user_message)
                logging.info(
                    "[MultimodalStrategy] image_validation: %.2fs",
                    time.time() - t0,
                )

            yield full_response

            # Persist conversation history locally
            if "messages" not in conv:
                conv["messages"] = []
            conv["messages"].append({"role": "user", "text": user_message})
            conv["messages"].append({"role": "assistant", "text": full_response})

        logging.info("[MultimodalStrategy] === Flow done === total: %.2fs", time.time() - flow_start)

    # ------------------------------------------------------------------
    # Post-flow cleanup (runs as background task)
    # ------------------------------------------------------------------
    async def _post_flow_cleanup(self, user_id: str) -> None:
        """Compatibility no-op while automatic profile access is suspended."""
        self._eligible_profile_user_id()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------
    async def clear_session(self):
        conv = self.conversation
        conv["session_initialized"] = False
        conv["messages"] = []
        self._user_memory = None
        logging.info("[MultimodalStrategy] Session cleared")
