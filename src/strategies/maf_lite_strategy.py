"""
Microsoft Agent Framework (MAF) Lite Strategy.

This strategy uses Microsoft Agent Framework with a direct Azure OpenAI model
connection — no Azure AI Foundry Agent Service V2 dependency.  It provides:
- Ordinary conversation history; automatic profile access is suspended
- Optional agentic search over documents
- Extensible context providers for custom capabilities
- Local conversation history (no server-side threads)

Hosted mode consumes Foundry-managed conversation history and disables profile
memory until an authenticated Foundry identity contract is available.
"""

import functools
import logging
import time
from collections.abc import Mapping
from typing import Optional

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
from .search_context_provider import SearchContextProvider
from .foundry_iq_context_provider import FoundryIQContextProvider
from .maf_plugins import UserProfile, UserProfileMemory
from .retrieval_intent import (
    RETRIEVAL_INTENT_SYSTEM_PROMPT,
    build_retrieval_intent_messages,
    parse_retrieval_intent,
)
from connectors.foundry_iq_mcp import is_mcp_enabled
from connectors.openai_chat_client import OpenAIChatClient
from connectors.search import acquire_obo_search_token
from connectors.obo import resolve_retrieval_authorization
from orchestration.agent_events import AgentEventTranslator
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
    return functools.partial(SearchContextProvider, owner_id=owner_id)


class MafLiteStrategy(BaseAgentStrategy):
    """Agent strategy using Microsoft Agent Framework with direct Azure OpenAI.

    Unlike ``MafAgentServiceStrategy`` this strategy does NOT require Azure AI
    Foundry Agent Service V2.  The ``ChatAgent`` talks to the model deployment
    directly via :class:`OpenAIChatClient`, while still supporting user-profile
    memory and optional agentic search.
    """

    _INTENT_SYSTEM_PROMPT = RETRIEVAL_INTENT_SYSTEM_PROMPT

    AGENT_INSTRUCTIONS = (
        "You are a helpful AI assistant. Your role is to assist users with their "
        "questions and tasks.\n\n"
        "Your capabilities:\n"
        "1. **Conversation**: Engage in helpful, informative conversations\n"
        "2. **Conversation Context**: Use this conversation; automatic profile personalization is disabled\n"
        "3. **Knowledge Search**: Search your knowledge base when relevant to answer questions\n\n"
        "Guidelines:\n"
        "- Provide clear, helpful, and accurate responses\n"
        "- Ask clarifying questions when needed\n"
        "- Be concise but thorough in your explanations"
    )

    def __init__(self):
        super().__init__()
        logging.debug("[MafLiteStrategy] Initializing...")

        cfg = get_config()
        self.strategy_type = AgentStrategies.MAF_LITE
        self.conversation_id: Optional[str] = None

        if not hasattr(self, "credential") or self.credential is None:
            self.credential = cfg.aiocredential
            logging.debug("[MafLiteStrategy] Using credential from AppConfigClient")

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

        # Max content characters per document chunk sent to the model
        self.max_content_chars = int(cfg.get("MAX_CONTENT_CHARS", 4000))

        # User profiles stored in the conversations container (no extra container needed)
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
        self._chat_client: Optional[OpenAIChatClient] = None
        self._user_memory: Optional[UserProfileMemory] = None
        self._search_provider: Optional[SearchContextProvider] = None
        self._cached_instructions: Optional[str] = None

        logging.debug("[MafLiteStrategy] Initialized")

    def set_context(self, conversation_id: Optional[str]) -> None:
        """Set conversation_id (may be None before orchestrator assigns a new id)."""
        self.conversation_id = conversation_id

    # ------------------------------------------------------------------
    # Prompt namespace override — share prompts with MafAgentServiceStrategy
    # ------------------------------------------------------------------
    def _prompt_namespace(self) -> str:
        return "maf"

    # ------------------------------------------------------------------
    # Client helpers
    # ------------------------------------------------------------------
    def _get_or_create_chat_client(self) -> OpenAIChatClient:
        if self._chat_client is None:
            logging.debug(
                "[MafLiteStrategy] Creating OpenAIChatClient "
                f"endpoint={self.model_endpoint} model={self.model_name}"
            )
            self._chat_client = OpenAIChatClient(
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
            logging.debug("[MafLiteStrategy] User profile unavailable (%s)", type(e).__name__)
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
                logging.warning("[MafLiteStrategy] User profile write not confirmed for %s", user_id)
            else:
                logging.info("[MafLiteStrategy] Saved user profile for %s", user_id)
        except Exception as e:
            logging.error("[MafLiteStrategy] Failed to save user profile (%s)", type(e).__name__)

    async def _ensure_user_memory(
        self,
        user_id: Optional[str],
        chat_client: OpenAIChatClient,
    ) -> None:
        """Automatic profile access is suspended pending trusted key binding."""
        self._eligible_profile_user_id()

    # ------------------------------------------------------------------
    # Search provider (optional agentic retrieval)
    # ------------------------------------------------------------------
    async def _create_search_provider(self) -> Optional[SearchContextProvider]:
        if not self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool):
            return None
        if not self.search_endpoint:
            logging.debug("[MafLiteStrategy] No search endpoint configured, skipping search")
            return None
        if not self.search_index_name:
            logging.warning("[MafLiteStrategy] No search index name configured, skipping search")
            return None

        SearchContextProvider = _scoped_search_provider_factory(
            _resolve_conversation_owner_id(self.user_context)
        )
        try:
            allow_anonymous = self.cfg.get(
                "ALLOW_ANONYMOUS", default=True, type=bool
            )
            retrieval_backend = get_retrieval_backend()
            token = getattr(self, "request_access_token", None)
            delegated = getattr(self, "delegated_retrieval", None)
            if delegated is not None:
                allow_anonymous = False
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
                if delegated is not None:
                    return delegated.search_token
                return (
                    await acquire_obo_search_token(
                        token,
                        allow_anonymous=False,
                    )
                    if token
                    else None
                )

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
                    delegated_retrieval=delegated,
                )
                logging.info(
                    "[MafLiteStrategy] FoundryIQContextProvider created (top_k=%d)",
                    self.search_top_k,
                )
                return provider

            provider = SearchContextProvider(
                endpoint=self.search_endpoint,
                credential=self.credential,
                conversation_id=self.conversation_id,
                index_name=self.search_index_name,
                top_k=self.search_top_k,
                semantic_configuration_name=self.semantic_search_config,
                embed_fn=embed_fn,
                get_obo_token=_get_obo_token,
                authorization_mode=authorization_mode,
                max_content_chars=self.max_content_chars,
                **({"native_permissions": True} if delegated is not None else {}),
            )
            logging.info(
                "[MafLiteStrategy] SearchContextProvider created (index=%s, top_k=%d)",
                self.search_index_name, self.search_top_k,
            )
            return provider
        except Exception as e:
            logging.error("[MafLiteStrategy] Failed to create search provider (%s)", type(e).__name__)
            raise

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
                reasoning_effort=self.reasoning_effort,
            )
            try:
                resp = await client._client.chat.completions.create(**_kwargs)
            except BadRequestError:
                _kwargs.pop("reasoning_effort", None)
                resp = await client._client.chat.completions.create(**_kwargs)
            result = (resp.choices[0].message.content or "").strip().upper()
            intent = parse_retrieval_intent(
                result,
                enable_no_retrieval=self.no_retrieval_followup_detection_enabled,
            )
            logging.info("[MafLiteStrategy] intent=%s (raw=%r)", intent, result)
            return intent
        except Exception as e:
            logging.warning("[MafLiteStrategy] Intent classification failed (%s); defaulting to question", type(e).__name__)
            return "question"

    # ------------------------------------------------------------------
    # Main agent flow
    # ------------------------------------------------------------------
    async def initiate_agent_flow(self, user_message: str):
        """Run the conversational agent with direct Azure OpenAI model access."""
        flow_start = time.time()
        logging.info("[MafLiteStrategy] === Flow start === question=%r", user_message[:120])

        conv = self.conversation
        is_new_session = not conv.get("session_initialized", False)
        user_id = self._eligible_profile_user_id()

        chat_client = self._get_or_create_chat_client()

        # Clear any cached profile; automatic access remains suspended.
        await self._ensure_user_memory(user_id, chat_client)

        history = conv.get("messages", [])

        # Classify intent — skip search when retrieval is not needed.
        t0 = time.time()
        intent = await self._classify_intent(user_message, history=history)
        logging.info("[MafLiteStrategy] intent_classification: %.2fs", time.time() - t0)

        # Only initialize retrieval for turns that need it.
        if intent == "question" and self._search_provider is None:
            t0 = time.time()
            self._search_provider = await self._create_search_provider()
            logging.info("[MafLiteStrategy] search_provider_init: %.2fs (hybrid=%s)", time.time() - t0, bool(self.embedding_deployment))

        # Build context providers
        context_providers = (
            [self._user_memory]
            if self._user_memory is not None
            else []
        )
        if intent == "question" and self._search_provider:
            context_providers.append(self._search_provider)
        elif intent == "greeting":
            logging.info("[MafLiteStrategy] Greeting detected — skipping search")
        elif intent == "no_retrieval":
            logging.info("[MafLiteStrategy] No-retrieval follow-up detected — skipping search")
        else:
            logging.warning("[MafLiteStrategy] No search provider — agent will answer without grounding")
        logging.info("[MafLiteStrategy] context_providers: %d", len(context_providers))

        # Read base instructions (cached after first read)
        if self._cached_instructions is None:
            base_instructions = await self._read_prompt("main")
            self._cached_instructions = base_instructions if base_instructions else self.AGENT_INSTRUCTIONS
        instructions = self._cached_instructions

        # Create agent and stream — no server-side thread needed
        async with ChatAgent(
            chat_client=chat_client,
            instructions=instructions,
            context_provider=(
                CompositeContextProvider(
                    context_providers,
                    required_providers=[self._search_provider] if intent == "question" and self._search_provider else [],
                )
                if context_providers
                else None
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
                    input_messages.append(ChatMessage(role=role, text=text))
            input_messages.append(ChatMessage(role="user", text=user_message))
            logging.info("[MafLiteStrategy] history_messages: %d (total input: %d)", len(history), len(input_messages))

            # Stream the agent response
            stream_start = time.time()
            full_response = ""
            event_translator = AgentEventTranslator()
            async for chunk in agent.run_stream(
                input_messages,
                thread=thread,
                options={"max_completion_tokens": self.max_completion_tokens, "reasoning_effort": self.reasoning_effort},
            ):
                for event in event_translator.translate(chunk):
                    yield event
                if chunk.text:
                    full_response += chunk.text
                    yield chunk.text
            logging.info("[MafLiteStrategy] agent_stream: %.2fs (response_len=%d)", time.time() - stream_start, len(full_response))

            # Persist conversation history locally
            if "messages" not in conv:
                conv["messages"] = []
            conv["messages"].append({"role": "user", "text": user_message})
            conv["messages"].append({"role": "assistant", "text": full_response})

        logging.info("[MafLiteStrategy] === Flow done === total: %.2fs", time.time() - flow_start)

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
        logging.info("[MafLiteStrategy] Session cleared")
