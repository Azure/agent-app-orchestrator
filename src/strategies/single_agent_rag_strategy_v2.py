import logging
import json
import time
from typing import Optional

# Suppress Azure SDK HTTP logging BEFORE importing azure packages
for _azure_logger in [
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.core",
    "azure"
]:
    logger = logging.getLogger(_azure_logger)
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    logger.disabled = True
    logger.handlers.clear()

from .base_agent_strategy import BaseAgentStrategy
from .agent_strategies import AgentStrategies
from . import agent_provider_v2
from . import hosted_strategies

from dependencies import get_config
from connectors.search import get_search_client
from connectors.aifoundry import get_genai_client
from orchestration.agent_events import AgentEventTranslator
from telemetry import (
    AuditEmitter,
    AuditStatus,
    EventType,
    ReasonCode,
    current_audit_context,
    invoke_audited_tool,
)
from openai import BadRequestError

# Stable name for the single, reusable Foundry prompt agent.
DEFAULT_REUSABLE_AGENT_NAME = "gptrag-single-agent-rag"


async def search_knowledge_base(query: str) -> str:
    """Search the knowledge base for documents relevant to the user's question.

    Use this whenever answering requires information from the indexed documents.

    Args:
        query: A natural-language search query describing the information needed.

    Returns:
        Markdown-formatted excerpts of the most relevant documents, each with a
        citable ``### [title](filepath)`` header.
    """
    # Schema-only reference. This callable defines the function-tool contract
    # recorded on the prompt-agent definition at create time. Actual execution
    # at request time is performed by a per-request bound callable (which carries
    # the same tool name) supplied through ``provider.as_agent(tools=[...])``.
    return ""


def _render_agent_instructions(cfg, *, aisearch_enabled: bool) -> str:
    """Render the single_agent_rag system prompt for the agent definition.

    ``user_context`` is intentionally empty so the agent definition stays stable
    and is reused across all users (per-user context is supplied at request time
    by the framework, not baked into the persistent agent)."""
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    src_folder = Path(__file__).resolve().parent.parent
    prompt_dir = src_folder / "prompts" / "single_agent_rag"
    env = Environment(
        loader=FileSystemLoader(str(prompt_dir)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("main.jinja2")
    return template.render({
        "strategy": "single_agent_rag",
        "user_context": {},
        "bing_grounding_enabled": False,
        "aisearch_enabled": aisearch_enabled,
    }).strip()


def _resolve_agent_name(cfg) -> str:
    configured_agent = (cfg.get("AGENT_ID", "") or "").strip()
    return configured_agent or DEFAULT_REUSABLE_AGENT_NAME


async def prewarm_agents_client(*, create_reusable_agent: bool = True) -> None:
    """Pre-warm the Foundry provider and (optionally) pre-create the reusable
    prompt agent.

    1. Initialises the process-wide ``AIProjectClient`` + provider so the first
       real request doesn't pay the cold-start penalty.
    2. When ``create_reusable_agent`` is set, materialises the versioned prompt
       agent exactly once via ``create_version`` so every request reuses it.
    """
    cfg = get_config()
    endpoint = cfg.get("AI_FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        logging.warning("[Startup] AI_FOUNDRY_PROJECT_ENDPOINT not set; skipping provider pre-warm")
        return

    from connectors.identity_manager import get_identity_manager
    credential = get_identity_manager().get_aio_credential()

    provider = await agent_provider_v2.get_provider(endpoint, credential)
    logging.info("[Startup] ✅ Foundry provider pre-warmed")

    if not create_reusable_agent:
        logging.info("[Startup] Skipping reusable prompt-agent creation for active strategy")
        return

    try:
        aisearch_enabled = cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
        instructions = _render_agent_instructions(cfg, aisearch_enabled=aisearch_enabled)
        name = _resolve_agent_name(cfg)
        tools = [search_knowledge_base] if aisearch_enabled else None
        await agent_provider_v2.get_or_create_agent_details(
            provider=provider,
            name=name,
            model=cfg.get("CHAT_DEPLOYMENT_NAME"),
            instructions=instructions,
            tools=tools,
            reasoning_effort=cfg.get("REASONING_EFFORT", "low"),
        )
        logging.info("[Startup] ✅ Reusable prompt agent ready (name=%s)", name)
    except Exception:
        logging.warning("[Startup] Could not pre-create prompt agent (will create on first request)")


class SingleAgentRAGStrategyV2(BaseAgentStrategy):
    """
    Implements a latency-optimized V2 Retrieve-Augmented Generation strategy.

    This strategy prioritizes bypassing heavy cloud Agent services when possible (e.g. empty indexes),
    instead relying on local Execution via `GenAIModelClient`.

    When an index has data or complex tools are needed, this uses Azure AI Agents SDK
    with explicit Event Handlers for streaming and local orchestration, avoiding polling loops.
    """

    @classmethod
    async def create(cls):
        """
        Factory method to create an instance of SingleAgentRAGStrategyV2.
        """
        logging.debug("[Agent Flow V2] Creating SingleAgentRAGStrategyV2 instance...")
        return cls()

    def __init__(self):
        super().__init__()

        logging.debug("[Init] Initializing SingleAgentRAGStrategyV2...")
        self.cfg = get_config()
        self.strategy_type = AgentStrategies.SINGLE_AGENT_RAG

        # Conversation scope for retrieval. Set by the orchestrator via set_context()
        # and/or read from self.conversation["id"] at stream time. Without it, the
        # AI Search filter falls back to shared ('NaN') chunks only and uploaded
        # per-conversation documents are never retrieved (see issue #478).
        self.conversation_id: Optional[str] = None

        aisearch_enabled = self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
        if not aisearch_enabled:
            logging.warning("[Init V2] SEARCH_RETRIEVAL_ENABLED set to false. SearchClient will not be available.")
            self.search_client = None
        else:
            self.search_client = get_search_client()
            logging.info("[Init V2] ✅ SearchClient initialized (singleton)")

        # Hard cap on output tokens for the main agent response.
        # Default 8000: reasoning models (e.g. gpt-5-nano) spend part of this
        # budget on reasoning tokens, so a higher cap leaves room for the answer.
        self.max_completion_tokens = int(self.cfg.get("MAX_COMPLETION_TOKENS", 8000))

        # Reasoning effort for models that support it (e.g. gpt-5-mini/nano).
        # Default "low": keeps reasoning token spend in check so the response
        # is not truncated with finish_reason=length on heavy turns.
        self.reasoning_effort = self.cfg.get("REASONING_EFFORT", "low")

        # Initialize Direct LLM Client for Bypass scenario (singleton)
        self.llm_client = get_genai_client()

    def set_context(self, conversation_id: Optional[str]) -> None:
        """Receive the active conversation id from the orchestrator.

        The orchestrator calls this (guarded by hasattr) before the agent flow
        runs. Storing it lets _stream_agent scope the AI Search retrieval to the
        current conversation so uploaded documents tagged with this id are
        returned alongside the shared corpus (issue #478).
        """
        if conversation_id:
            self.conversation_id = conversation_id

    def _resolve_conversation_id(self) -> Optional[str]:
        """Resolve the chat conversation id for retrieval scoping.

        Prefer the conversation dict id assigned by the orchestrator; fall back
        to the id captured via set_context(). Never use thread_id — that is the
        agent thread id, not the chat conversation id used to tag documents.
        """
        return (getattr(self, "conversation", None) or {}).get("id") \
            or getattr(self, "conversation_id", None)

    def _apply_search_request_context(self) -> None:
        """Push per-request retrieval context onto the (singleton) search client.

        Scopes the AI Search filter to the current conversation so uploaded
        documents (tagged with this conversation id by the ingestion service)
        are included alongside the shared corpus (issue #478).
        """
        if not self.search_client:
            return
        allow_anonymous = self.cfg.get("ALLOW_ANONYMOUS", default=True, type=bool)
        request_access_token = getattr(self, "request_access_token", None)
        conversation_id = self._resolve_conversation_id()
        try:
            self.search_client.set_request_context(
                api_access_token=request_access_token,
                allow_anonymous=allow_anonymous,
                conversation_id=conversation_id,
                user_context=self.user_context,
            )
        except Exception:
            logging.warning("[Agent Flow V2] Failed to apply legacy search request context")
            raise

    def _build_search_tool(self):
        """Build the per-request retrieval tool bound to this request's context.

        Extracted so the conversation-scope fix for issue #478 has a single
        code path used both by ``_stream_agent`` and by the regression tests.
        Returns ``None`` when retrieval is disabled.
        """
        if not self.search_client:
            return None

        search_client = self.search_client
        format_results = self._format_search_results

        async def _bound_search(query: str) -> str:
            self._apply_search_request_context()
            t_ret = time.time()
            result = await invoke_audited_tool(
                "search_knowledge_base",
                lambda: search_client.search_knowledge_base(query=query),
                tool_kind="retrieval",
                sensitive_arguments={"query": query},
            )
            logging.info(f"[Agent Flow V2] Retrieval tool executed in {time.time() - t_ret:.2f}s")
            return format_results(result)

        _bound_search.__name__ = "search_knowledge_base"
        return _bound_search

    async def initiate_agent_flow(self, user_message: str):
        """
        V2 Latency-Optimized Agent Flow.
        Routes locally based on cached index state:
        - If empty: Direct streaming chat completion via GenAIModelClient (Bypass)
        - If not empty: Azure AI Agents SDK streaming with event handlers
        """
        flow_start = time.time()

        is_empty = False
        if self.search_client:
            is_empty = await self.search_client.is_index_empty()

        logging.info(f"[Agent Flow V2] Index Empty Check Result: {is_empty} ({round(time.time() - flow_start, 2)}s)")

        # 1. Bypass Routing: Empty Index goes directly to Chat Completion for ~Instant TTFB
        if is_empty and not self.cfg.get("BING_RETRIEVAL_ENABLED", False, type=bool):
            context = current_audit_context()
            AuditEmitter.default().emit(
                EventType.ROUTE_SELECTED,
                operation="single_agent_rag.select_route",
                status=AuditStatus.SELECTED,
                reason_code=ReasonCode.DIRECT_MODEL_SELECTED,
                parent_event_id=(
                    context.request_started_event_id if context else None
                ),
                metadata={
                    "decision_type": "single_agent_rag_route",
                    "decision_value": "direct_model",
                },
            )
            logging.info("[Agent Flow V2] ⚡ Routing locally directly to LLM (Search Index is empty, bypassing Agents)")
            async for chunk in self._stream_direct_llm(user_message):
                yield chunk
        else:
            context = current_audit_context()
            AuditEmitter.default().emit(
                EventType.ROUTE_SELECTED,
                operation="single_agent_rag.select_route",
                status=AuditStatus.SELECTED,
                reason_code=ReasonCode.AGENT_SELECTED,
                parent_event_id=(
                    context.request_started_event_id if context else None
                ),
                metadata={
                    "decision_type": "single_agent_rag_route",
                    "decision_value": "agent_service",
                },
            )
            # 2. Complex Routing: Use Azure AI Agents SDK
            logging.info("[Agent Flow V2] 🤖 Routing to Azure AI Agents SDK (Search Index has data or Bing enabled)")
            async for chunk in self._stream_agent(user_message):
                yield chunk

        logging.info(f"[Agent Flow V2] Total flow time: {round(time.time() - flow_start, 2)}s")

    async def _stream_direct_llm(self, user_message: str):
        """
        Direct lightweight GenAI client stream, skipping all Agent SDK overhead.
        Used for basic Q&A when no search data exists.
        """
        stream_start = time.time()

        aisearch_enabled = self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
        # This path is the empty-index bypass: no search tool is bound here. When a
        # knowledge base is configured (aisearch enabled) but has no content to
        # ground on, answer strictly: greet on small talk, otherwise tell the user
        # there isn't enough information in the context instead of replying from the
        # model's own knowledge. When no KB is configured at all, fall back to the
        # plain direct-assistant behavior.
        grounded_no_context = aisearch_enabled

        # Build prompt
        prompt_context = {
            "strategy": self.strategy_type.value,
            "user_context": self.user_context or {},
            "aisearch_enabled": False,
            "bing_grounding_enabled": False,
            "grounded_no_context": grounded_no_context,
        }
        system_prompt = await self._read_prompt("main", use_jinja2=True, jinja2_context=prompt_context)

        messages = [{"role": "system", "content": system_prompt}]

        # Append history supplied by the active runtime (Cosmos in classic mode,
        # Foundry managed Conversations in hosted mode).
        # Stored entries use the "text" key, while the chat completions API expects
        # "content"; normalize and drop empties to avoid 400 "content is null" errors.
        history = self.conversation.get("messages", [])
        for msg in history[-5:]:  # Keep last 5 for speed
            role = msg.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            content = msg.get("content")
            if content is None:
                content = msg.get("text")
            if not content:
                continue
            messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})

        try:
             # Fast streaming completion
             _kwargs = dict(
                 model=self.llm_client.chat_deployment,
                 messages=messages,
                 stream=True,
                 max_completion_tokens=self.max_completion_tokens,
                 reasoning_effort=self.reasoning_effort,
             )
             try:
                 response_stream = await self.llm_client.openai_client.chat.completions.create(**_kwargs)
             except BadRequestError:
                 _kwargs.pop("reasoning_effort", None)
                 response_stream = await self.llm_client.openai_client.chat.completions.create(**_kwargs)

             full_response = ""
             first_token = False
             async for update in response_stream:
                 if not first_token:
                     logging.info(f"[Agent Flow V2] ⚡ Direct LLM First Token: {time.time() - stream_start:.2f}s")
                     first_token = True

                 if update.choices and update.choices[0].delta.content:
                     full_response += update.choices[0].delta.content
                     yield update.choices[0].delta.content

        except Exception:
             logging.error("[Agent Flow V2] Direct LLM Streaming failed")
             raise

        await self._persist_managed_turn(user_message, full_response)

        # Add assistant response to history
        self.conversation.setdefault("messages", []).extend([
            {"role": "user", "text": user_message},
            {"role": "assistant", "text": full_response}
        ])

    _CITATION_RULES = (
        "## Retrieved Documents\n\n"
        "The following documents were retrieved from the knowledge base. "
        "Each document starts with a header line in the format: ### [Document Title](filepath). "
        "Base your answer on these documents.\n\n"
        "**Citation rules:**\n"
        "- ONLY cite using the document title and filepath from the ### header lines above.\n"
        "- Format: [Document Title](filepath) — use the EXACT title and filepath from the header.\n"
        "- Do NOT omit the (filepath) part. Every citation MUST include both [title] AND (filepath).\n"
        "- Do NOT treat any text inside the document content as a citation source. "
        "Internal references, chapter names, or bracketed text within the content are NOT valid sources.\n"
        "- Cite each source ONLY ONCE. Do NOT repeat the same citation on every bullet point or paragraph.\n"
        "- Example: According to [Product Guide](product-guide.pdf), the system supports...\n"
    )

    @staticmethod
    def _format_search_results(raw_result) -> str:
        """Format search results as markdown with [title](link) headers.

        Mirrors the format used by SearchContextProvider so the model
        sees citation examples and reproduces them naturally.
        """
        try:
            data = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            results = data.get("results", [])
            if not results:
                return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
        except (json.JSONDecodeError, AttributeError):
            return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)

        parts: list[str] = []
        for doc in results:
            title = doc.get("title") or "reference"
            link = doc.get("link") or ""
            content = doc.get("content") or ""
            if not content:
                continue
            header = f"### [{title}]({link})" if link else f"### {title}"
            parts.append(f"{header}\n{content}")

        if not parts:
            return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)

        return (
            SingleAgentRAGStrategyV2._CITATION_RULES + "\n\n"
            + "\n\n---\n\n".join(parts)
        )

    async def _stream_agent(self, user_message: str):
        """Stream a response using the reusable Foundry prompt agent (Responses
        runtime via the Microsoft Agent Framework ``ChatAgent``).

        The versioned prompt agent is created once (``create_version``) and reused
        on every request. Knowledge-base retrieval is exposed as a MAF function
        tool that the framework invokes automatically; there is no per-request
        agent creation or deletion.
        """
        stream_start = time.time()
        conv = self.conversation

        aisearch_enabled = (
            self.cfg.get("SEARCH_RETRIEVAL_ENABLED", True, type=bool)
            and self.search_client is not None
        )

        if self.cfg.get("BING_RETRIEVAL_ENABLED", False, type=bool):
            logging.warning(
                "[Agent Flow V2] BING_RETRIEVAL_ENABLED is set but Bing grounding is "
                "not yet supported on the declarative agent path; continuing without it."
            )

        # Resolve the shared, reusable prompt agent. AGENT_ID pins deployments to
        # an existing Foundry prompt agent; otherwise we use a stable default name.
        instructions = _render_agent_instructions(self.cfg, aisearch_enabled=aisearch_enabled)
        provider = await agent_provider_v2.get_provider(self.project_endpoint, self.credential)
        agent_name = _resolve_agent_name(self.cfg)

        t0 = time.time()
        details = await agent_provider_v2.get_or_create_agent_details(
            provider=provider,
            name=agent_name,
            model=self.model_name,
            instructions=instructions,
            tools=[search_knowledge_base] if aisearch_enabled else None,
            reasoning_effort=self.reasoning_effort,
        )
        logging.info(f"[Agent Flow V2][Telemetry] Agent resolve took: {time.time() - t0:.2f}s")

        # Per-request bound retrieval tool. It carries the same tool name as the
        # definition's function tool (set explicitly below) so MAF can match and
        # invoke it, while closing over this request's OBO/search context.
        request_tools = None
        if aisearch_enabled:
            tool = self._build_search_tool()
            if tool is not None:
                request_tools = [tool]

        agent = provider.as_agent(details, tools=request_tools)

        self._stream_start_time = stream_start
        full_response = ""
        event_translator = AgentEventTranslator()
        try:
            async with agent:
                if self.hosted_runtime:
                    # Stateless hosted path: zero managed-Conversations
                    # data-plane operations. No service-managed thread is
                    # created, read, or resumed, so there is no service
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
                    # Legacy Assistants thread ids are not valid Responses
                    # conversation ids.
                    agent_provider_v2.reset_legacy_thread(conv)

                    # Back the chat thread with a dedicated server-side
                    # conversation object (created once, resumed every turn).
                    # Resuming from the previous turn's response id instead
                    # breaks tool-call chaining on follow-up turns, which fail
                    # with "400 No tool call found for function call output"
                    # (Azure/agent-landing-zone#505).
                    thread_id = await agent_provider_v2.ensure_conversation_id(conv)
                    thread = agent.get_new_thread(service_thread_id=thread_id)
                    run_input = user_message
                    run_options = {"max_tokens": self.max_completion_tokens}

                logging.info("[Agent Flow V2] Streaming from Foundry prompt agent (Responses)...")
                first_token = False
                # ``reasoning`` is baked into the agent definition (it is a
                # definition-level setting and is rejected as a per-run option);
                # only ``max_tokens`` is passed per run, with a one-shot fallback
                # to no options if the service ever rejects it too.
                async for chunk in agent_provider_v2.stream_agent_run(
                    agent,
                    run_input,
                    thread=thread,
                    options=run_options,
                ):
                    for event in event_translator.translate(chunk):
                        yield event
                    if chunk.text:
                        if not first_token:
                            logging.info(f"[Agent Flow V2] 🤖 First Token: {time.time() - stream_start:.2f}s")
                            first_token = True
                        full_response += chunk.text
                        yield chunk.text

        except Exception:
            logging.error("[Agent Flow V2] Streaming failed")
            raise

        if not self.hosted_runtime:
            await self._persist_managed_turn(user_message, full_response)

        # Persist conversation history
        conv.setdefault("messages", []).extend([
            {"role": "user", "text": user_message},
            {"role": "assistant", "text": full_response}
        ])

    async def _persist_managed_turn(
        self,
        user_message: str,
        assistant_message: str,
    ) -> None:
        """Persist a completed turn when this runtime has a managed thread.

        Never runs for the hosted runtime: the hosted container performs zero
        managed-Conversations data-plane operations, so this is a defense-in-
        depth guard in addition to the hosted conversation dict never
        carrying a ``thread_id``/``agent_backend`` tag in the first place.
        """
        if self.hosted_runtime:
            return
        conversation_id = self.conversation.get("thread_id")
        if (
            not conversation_id
            or self.conversation.get("agent_backend")
            != agent_provider_v2.AGENT_BACKEND_TAG
        ):
            return
        await agent_provider_v2.get_provider(
            self.project_endpoint,
            self.credential,
        )
        await agent_provider_v2.persist_conversation_turn(
            conversation_id,
            user_message,
            assistant_message,
        )
