"""
Microsoft Agent Framework (MAF) + Agent Service V2 Strategy.

This strategy implements a conversational agent using Microsoft Agent Framework
with Azure AI Foundry Agent Service V2 as the backend. It provides:
- Ordinary conversation history; automatic profile access is suspended
- Optional agentic search over documents via Agent Service V2
- Extensible context providers for custom capabilities
- Server-side thread management via Agent Service V2

Hosted mode reuses the Foundry-managed conversation as its server thread and
disables profile memory until authenticated Foundry identity is available.
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
    "azure"
]:
    _logger = logging.getLogger(_azure_logger)
    _logger.setLevel(logging.CRITICAL)
    _logger.propagate = False
    _logger.disabled = True
    _logger.handlers.clear()

from agent_framework import ChatAgent

from .base_agent_strategy import BaseAgentStrategy
from .agent_strategies import AgentStrategies
from .composite_context_provider import CompositeContextProvider
from .search_context_provider import SearchContextProvider
from .foundry_iq_context_provider import FoundryIQContextProvider
from .maf_plugins import UserProfile, UserProfileMemory
from . import agent_provider_v2
from . import hosted_strategies
from connectors.foundry_iq_mcp import is_mcp_enabled
from connectors.openai_chat_client import OpenAIChatClient
from connectors.search import acquire_obo_search_token
from connectors.obo import resolve_retrieval_authorization
from orchestration.agent_events import AgentEventTranslator
from util.retrieval_backend import get_retrieval_backend, RETRIEVAL_BACKEND_FOUNDRY_IQ
from dependencies import get_config


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


# ============================================================================
# Main MAF Strategy
# ============================================================================

class MafAgentServiceStrategy(BaseAgentStrategy):
    """
    Agent strategy using Microsoft Agent Framework + Azure AI Foundry Agent Service V2.

    This strategy serves as a blank canvas for building custom agent capabilities:
    1. Maintaining ordinary conversation history without profile collection
    2. Optional agentic search over documents (via Agent Service V2)
    3. Extensible context providers for custom functionality

    Uses the Azure AI Foundry **declarative** Agent API: a single versioned
    *prompt agent* (``AIProjectClient.agents.create_version`` +
    ``PromptAgentDefinition``) is created once and reused across every request,
    with server-side thread management handled by the Microsoft Agent Framework
    ``ChatAgent`` (Responses runtime).
    """

    AGENT_INSTRUCTIONS = """You are a helpful AI assistant. Your role is to assist users with their
questions and tasks.

Your capabilities:
1. **Conversation**: Engage in helpful, informative conversations
2. **Conversation Context**: Use this conversation; automatic profile personalization is disabled
3. **Knowledge Search**: Search your knowledge base when relevant to answer questions

Guidelines:
- Provide clear, helpful, and accurate responses
- Ask clarifying questions when needed
- Be concise but thorough in your explanations"""

    def __init__(self):
        """Initialize the MAF + Agent Service V2 strategy."""
        super().__init__()

        logging.debug("[MafAgentServiceStrategy] Initializing...")

        cfg = get_config()
        self.strategy_type = AgentStrategies.MAF_AGENT_SERVICE
        self.conversation_id: Optional[str] = None

        # Ensure credential is set (use config's async credential as fallback)
        if not hasattr(self, 'credential') or self.credential is None:
            self.credential = cfg.aiocredential
            logging.debug("[MafAgentServiceStrategy] Using credential from AppConfigClient")

        # Direct-model client config used only for the (background) user-profile
        # extraction performed by UserProfileMemory. The main agent runs through
        # the shared Foundry prompt-agent provider, not this client.
        self._sync_credential = cfg.credential
        self.model_endpoint = cfg.get("AI_FOUNDRY_ACCOUNT_ENDPOINT")
        self.openai_api_version = cfg.get("OPENAI_API_VERSION", "2025-04-01-preview")
        self._memory_chat_client: Optional[OpenAIChatClient] = None

        # Azure AI Search configuration for retrieval
        self.search_endpoint = cfg.get_value("SEARCH_SERVICE_QUERY_ENDPOINT", allow_none=True)
        self.search_index_name = cfg.get_value("SEARCH_RAG_INDEX_NAME", allow_none=True)
        self.search_top_k = int(cfg.get("SEARCH_RAGINDEX_TOP_K", 3))
        self.semantic_search_config = cfg.get_value("SEARCH_SEMANTIC_SEARCH_CONFIG", allow_none=True)

        # User profiles stored in the conversations container (no extra container needed)
        self.user_profile_container = cfg.get("CONVERSATIONS_DATABASE_CONTAINER", "conversations")

        # Hard cap on output tokens for the main agent response
        self.max_completion_tokens = int(cfg.get("MAX_COMPLETION_TOKENS", 4096))

        # Reasoning effort for models that support it (e.g. gpt-5-mini)
        self.reasoning_effort = cfg.get("REASONING_EFFORT", "medium")

        # Runtime state
        self._agent: Optional[ChatAgent] = None
        self._search_provider: Optional[SearchContextProvider] = None

        logging.debug("[MafAgentServiceStrategy] Initialized")
    
    def set_context(self, conversation_id: Optional[str]) -> None:
        """Set conversation_id (may be None before orchestrator assigns a new id)."""
        self.conversation_id = conversation_id

    def _prompt_namespace(self) -> str:
        """Share prompts directory with MafLiteStrategy."""
        return "maf"

    def _get_or_create_memory_chat_client(self) -> OpenAIChatClient:
        """Lazily build the direct-model client used by UserProfileMemory for
        background profile extraction. Reused for the lifetime of the worker."""
        if self._memory_chat_client is None:
            logging.debug(
                "[MafAgentServiceStrategy] Creating OpenAIChatClient for memory "
                f"endpoint={self.model_endpoint} model={self.model_name}"
            )
            self._memory_chat_client = OpenAIChatClient(
                azure_endpoint=self.model_endpoint,
                model_deployment_name=self.model_name,
                credential=self._sync_credential,
                api_version=self.openai_api_version,
            )
        return self._memory_chat_client

    async def _load_user_profile(self, user_id: str) -> UserProfile:
        """Load user profile from CosmosDB or return empty profile."""
        profile_key = f"user_profile_{user_id}"
        try:
            doc = await self.cosmos.get_document(self.user_profile_container, profile_key)
            if doc and "profile_data" in doc:
                return UserProfile.model_validate_json(doc["profile_data"])
        except Exception as e:
            logging.debug("[MafAgentServiceStrategy] User profile unavailable (%s)", type(e).__name__)
        return UserProfile()

    async def _save_user_profile(self, user_id: str, profile: UserProfile):
        """Save user profile to CosmosDB."""
        profile_key = f"user_profile_{user_id}"
        try:
            doc = {
                "id": profile_key,
                "profile_data": profile.model_dump_json(),
                "updated_at": time.time()
            }
            existing = await self.cosmos.get_document(self.user_profile_container, profile_key)
            if existing:
                saved_doc = await self.cosmos.update_document(self.user_profile_container, doc)
            else:
                saved_doc = await self.cosmos.create_document(self.user_profile_container, profile_key, body=doc)
            if saved_doc is None:
                logging.warning("[MafAgentServiceStrategy] User profile write not confirmed for %s", user_id)
            else:
                logging.info("[MafAgentServiceStrategy] Saved user profile for %s", user_id)
        except Exception as e:
            logging.error("[MafAgentServiceStrategy] Failed to save user profile (%s)", type(e).__name__)

    async def _create_user_memory(
        self,
        user_id: Optional[str],
    ) -> Optional[UserProfileMemory]:
        """Automatic profile access is suspended pending trusted key binding."""
        self._eligible_profile_user_id()
        return None

    async def _persist_user_memory(
        self,
        user_id: Optional[str],
        user_memory: Optional[UserProfileMemory],
    ) -> None:
        """Automatic profile writes are suspended, including cached profiles."""
        self._eligible_profile_user_id()

    async def _create_search_provider(self) -> Optional[SearchContextProvider]:
        """Create the search context provider for retrieval."""
        if not self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool):
            return None
        if not self.search_endpoint:
            logging.debug("[MafAgentServiceStrategy] No search endpoint configured, skipping search")
            return None
        if not self.search_index_name:
            logging.warning("[MafAgentServiceStrategy] No search index name configured, skipping search")
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
                    "[MafAgentServiceStrategy] FoundryIQContextProvider created (top_k=%d)",
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
                get_obo_token=_get_obo_token,
                authorization_mode=authorization_mode,
                **({"native_permissions": True} if delegated is not None else {}),
            )
            logging.info(
                "[MafAgentServiceStrategy] SearchContextProvider created (index=%s)",
                self.search_index_name,
            )
            return provider

        except Exception as e:
            logging.error("[MafAgentServiceStrategy] Failed to create search provider (%s)", type(e).__name__)
            raise

    def _build_session_summary(self, user_memory: UserProfileMemory) -> str:
        """Do not imply that inaccessible legacy profiles were loaded or saved."""
        return "Automatic profile personalization is disabled."

    async def initiate_agent_flow(self, user_message: str):
        """
        Initiate the agent flow for a conversational interaction.

        Steps:
        1. Clear automatic profile context (unverified legacy-key binding)
        2. Create agent with context providers
        3. Preserve ordinary conversation context
        4. Process user message and stream response
        5. Preserve ordinary conversation history; profile writes remain disabled
        """
        flow_start = time.time()
        logging.debug(f"[MafAgentServiceStrategy] initiate_agent_flow called with: {user_message!r}")

        conv = self.conversation
        is_new_session = not conv.get("session_initialized", False)

        # Get user ID from conversation context
        user_id = self._eligible_profile_user_id()

        user_memory = await self._create_user_memory(user_id)

        # Initialize search provider if not done
        if self._search_provider is None:
            self._search_provider = await self._create_search_provider()

        # Read base instructions
        base_instructions = await self._read_prompt("main")
        instructions = base_instructions if base_instructions else self.AGENT_INSTRUCTIONS

        # Resolve the shared, reusable Foundry prompt agent (created once via
        # create_version, then reused on every request and across restarts).
        provider = await agent_provider_v2.get_provider(self.project_endpoint, self.credential)
        agent_name = agent_provider_v2.compute_agent_name(
            "gptrag-maf-agent-service",
            model=self.model_name,
            instructions=instructions,
            tool_names=[],
            extra={"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else None,
        )
        details = await agent_provider_v2.get_or_create_agent_details(
            provider=provider,
            name=agent_name,
            model=self.model_name,
            instructions=instructions,
            tools=None,
            reasoning_effort=self.reasoning_effort,
        )

        # Legacy Assistants thread ids are not valid Responses conversation
        # ids; drop them before reusing the server thread. Not applicable to
        # the stateless hosted runtime, which never binds to a
        # service-managed thread at all.
        if not self.hosted_runtime:
            agent_provider_v2.reset_legacy_thread(conv)

        context_providers = [user_memory] if user_memory is not None else []
        if self._search_provider:
            context_providers.append(self._search_provider)

        # Wrap the cached agent version into a ChatAgent (no HTTP call). The
        # underlying client reuses the shared project client and does NOT
        # close it on exit, so the singleton survives across requests.
        async with provider.as_agent(
            details,
            context_provider=(
                CompositeContextProvider(
                    context_providers,
                    required_providers=[self._search_provider] if self._search_provider else [],
                )
                if context_providers
                else None
            ),
        ) as agent:

            if self.hosted_runtime:
                # Stateless hosted path: zero managed-Conversations
                # data-plane operations. No service-managed thread is
                # created, read, or resumed -- there is no service
                # identity a caller-selected conversation id could
                # redirect. The complete ordered history already supplied
                # by the authenticated caller is replayed as plain input
                # messages on a purely local, ephemeral thread, and
                # ``store: False`` guarantees the turn is never persisted
                # server-side either.
                thread = agent.get_new_thread()
                run_input = hosted_strategies.build_stateless_messages(
                    conv.get("messages", []), user_message,
                )
                run_options = {
                    "max_tokens": self.max_completion_tokens,
                    "store": False,
                }
            else:
                # Get or create thread
                thread_id = conv.get("thread_id")
                if thread_id:
                    # Resume existing thread
                    thread = agent.get_new_thread(service_thread_id=thread_id)
                else:
                    # Create new thread
                    thread = agent.get_new_thread()
                    # service_thread_id may be None until first run; we'll update after
                    if thread.service_thread_id:
                        conv["thread_id"] = thread.service_thread_id
                run_input = user_message
                run_options = {"max_tokens": self.max_completion_tokens}

            # If new session with existing profile, provide summary
            if (
                is_new_session
                and user_memory is not None
                and user_memory.has_minimum_context()
            ):
                conv["session_initialized"] = True
                session_summary = self._build_session_summary(user_memory)
                yield f"Welcome back! Here's what I remember:\n\n{session_summary}\n\n---\n\n"
            elif is_new_session:
                conv["session_initialized"] = True

            # Stream the agent response. ``reasoning`` is baked into the agent
            # definition (definition-level setting, rejected as a per-run
            # option); only ``max_tokens`` is passed per run, with a one-shot
            # fallback to no options if the service ever rejects it too.
            full_response = ""
            event_translator = AgentEventTranslator()
            async for chunk in agent_provider_v2.stream_agent_run(
                agent,
                run_input,
                thread=thread,
                options=run_options,
            ):
                for event in event_translator.translate(chunk):
                    yield event
                if chunk.text:
                    full_response += chunk.text
                    yield chunk.text

            # Capture thread_id if it was set during the run (classic
            # runtime only; the hosted runtime never persists one).
            if (
                not self.hosted_runtime
                and not conv.get("thread_id")
                and thread.service_thread_id
            ):
                conv["thread_id"] = thread.service_thread_id

            # Store in conversation history
            if "messages" not in conv:
                conv["messages"] = []
            conv["messages"].append({"role": "user", "text": user_message})
            conv["messages"].append({"role": "assistant", "text": full_response})

        # Flush any pending background profile extraction before persisting so
        # the saved profile reflects this turn (parity with MafLiteStrategy).
        await self._persist_user_memory(user_id, user_memory)

        logging.info(f"[MafAgentServiceStrategy] Flow completed in {round(time.time() - flow_start, 2)}s")

    async def clear_session(self):
        """Clear the current session state (but preserve persisted profile)."""
        conv = self.conversation
        conv["session_initialized"] = False
        conv["thread_id"] = None
        conv["messages"] = []

        self._agent = None

        logging.info("[MafAgentServiceStrategy] Session cleared")
