"""
Shared helpers for the Foundry declarative Agent API (Agent Service v2).

This module centralises the *new* Azure AI Foundry agent pattern:

* a process-wide ``AIProjectClient`` + ``AzureAIProjectAgentProvider`` singleton,
* a get-or-create-by-name routine that materialises a *versioned prompt agent*
  (``AIProjectClient.agents.create_version`` + ``PromptAgentDefinition``) exactly
  once and reuses it across every request.

It replaces the legacy Assistants-style ``AgentsClient.create_agent()`` /
``delete_agent()`` ephemeral-per-request pattern used by the older strategies.

Strategies are instantiated per request by ``AgentStrategyFactory``, so any state
that must survive across requests (the provider, the project client and the
resolved agent definitions) lives at module scope here.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from typing import Any, AsyncIterator, Optional, Sequence, Union

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, Reasoning
from agent_framework import normalize_tools
from agent_framework.azure import AzureAIProjectAgentProvider

# ``to_azure_ai_tools`` converts Agent Framework function tools into the Azure AI
# ``FunctionTool`` schema that must be embedded in the *agent definition* (the
# service uses it to emit tool calls). It currently lives in a private module of
# the pinned ``agent-framework-azure-ai`` beta; import defensively so a future
# package reshuffle degrades loudly (fail-fast at create time) rather than
# importing-time crashing the whole app.
try:
    from agent_framework_azure_ai._shared import to_azure_ai_tools
    _HAS_TOOL_CONVERTER = True
except ImportError:
    to_azure_ai_tools = None  # type: ignore[assignment]
    _HAS_TOOL_CONVERTER = False

# Conversations whose server-side ``thread_id`` was created by the new
# Responses-based prompt agents are tagged with this value. A conversation
# carrying a different/absent tag holds a legacy Assistants thread id that is
# NOT a valid Responses conversation id and must be reset before reuse.
AGENT_BACKEND_TAG = "foundry_responses_v1"

_provider: Optional[AzureAIProjectAgentProvider] = None
_project_client: Optional[AIProjectClient] = None
_provider_lock = asyncio.Lock()

_openai_client: Any = None
_openai_client_lock = asyncio.Lock()

# name -> AgentVersionDetails, cached so per-request agent construction is a
# pure in-process call (no HTTP) once the agent has been resolved once.
_agent_details_cache: dict[str, Any] = {}
_agent_locks: dict[str, asyncio.Lock] = {}


async def get_provider(endpoint: str, credential: Any) -> AzureAIProjectAgentProvider:
    """Return the process-wide provider, creating it (and its ``AIProjectClient``)
    once. The client is intentionally never closed: it lives for the lifetime of
    the worker process and is shared by all requests."""
    global _provider, _project_client
    if _provider is not None:
        return _provider
    async with _provider_lock:
        if _provider is None:
            _project_client = AIProjectClient(endpoint=endpoint, credential=credential)
            _provider = AzureAIProjectAgentProvider(project_client=_project_client)
            logging.info("[AgentProviderV2] Initialized AIProjectClient + provider (endpoint=%s)", endpoint)
    return _provider


def compute_agent_name(
    base_name: str,
    *,
    model: str,
    instructions: str,
    tool_names: Sequence[str],
    extra: Optional[dict] = None,
) -> str:
    """Derive a stable, definition-aware agent name.

    A short fingerprint of the definition (model + instructions + tool names +
    extra flags) is embedded in the name so that:

    * identical definitions resolve to the *same* persistent agent — created
      once, reused on every subsequent request and across restarts;
    * a changed definition (e.g. a new release with updated instructions or
      tools) deterministically maps to a *new* agent name, avoiding stale-version
      reuse during rolling deployments where multiple app versions coexist.
    """
    h = hashlib.sha256()
    h.update((model or "").encode("utf-8"))
    h.update(b"\0")
    h.update((instructions or "").encode("utf-8"))
    h.update(b"\0")
    h.update(",".join(sorted(tool_names)).encode("utf-8"))
    if extra:
        for key in sorted(extra):
            h.update(f"\0{key}={extra[key]}".encode("utf-8"))
    fingerprint = h.hexdigest()[:10]
    return f"{base_name}-{fingerprint}"


async def get_or_create_agent_details(
    *,
    provider: AzureAIProjectAgentProvider,
    name: str,
    model: str,
    instructions: str,
    tools: Optional[Sequence[Any]] = None,
    reasoning_effort: Optional[str] = None,
) -> Any:
    """Return cached ``AgentVersionDetails`` for ``name``, creating the versioned
    prompt agent exactly once when it does not yet exist.

    Existence is checked with ``AIProjectClient.agents.get(name)``; creation goes
    through ``agents.create_version`` with a ``PromptAgentDefinition``.

    ``reasoning_effort`` (``minimal|low|medium|high``) is baked into the agent
    *definition* when provided. Reasoning is a definition-level setting on the
    new declarative agents: it cannot be passed as a per-run option (the service
    rejects ``reasoning`` with ``invalid_payload`` when an agent is specified), so
    it must live on the version. Callers MUST also fold ``reasoning_effort`` into
    the agent ``name`` fingerprint so a changed effort maps to a new agent.

    There is no ephemeral per-request creation and no ``delete_agent``: the
    resolved definition is cached in-process and reused.

    Note: ``create_version`` is not idempotent, so creation is guarded by an
    existence check and a per-name lock. Concurrent cold replicas may still each
    create an (identical) version; that is acceptable because every process
    always reuses ``versions.latest`` for the same definition-fingerprinted name.
    """
    cached = _agent_details_cache.get(name)
    if cached is not None:
        return cached

    lock = _agent_locks.setdefault(name, asyncio.Lock())
    async with lock:
        cached = _agent_details_cache.get(name)
        if cached is not None:
            return cached

        client = _project_client
        try:
            existing = await client.agents.get(agent_name=name)
            details = existing.versions.latest
            logging.info(
                "[AgentProviderV2] Reusing existing prompt agent '%s' (version=%s)",
                name, getattr(details, "version", "?"),
            )
        except ResourceNotFoundError:
            logging.info(
                "[AgentProviderV2] Prompt agent '%s' not found; creating once via create_version",
                name,
            )
            try:
                await _create_versioned_agent(
                    provider=provider,
                    client=client,
                    name=name,
                    model=model,
                    instructions=instructions,
                    tools=tools,
                    reasoning_effort=reasoning_effort,
                )
            except (ResourceExistsError, HttpResponseError) as create_err:
                # Another cold replica created the same definition-fingerprinted
                # agent first (create_version is not idempotent). Fall back to
                # reusing whatever version now exists for this name.
                status = getattr(create_err, "status_code", None)
                if isinstance(create_err, HttpResponseError) and status not in (409, None):
                    raise
                logging.info(
                    "[AgentProviderV2] Prompt agent '%s' was created concurrently "
                    "(status=%s); reusing existing version",
                    name, status,
                )
            existing = await client.agents.get(agent_name=name)
            details = existing.versions.latest
            logging.info(
                "[AgentProviderV2] Created prompt agent '%s' (version=%s)",
                name, getattr(details, "version", "?"),
            )

        _agent_details_cache[name] = details
        return details


async def _create_versioned_agent(
    *,
    provider: AzureAIProjectAgentProvider,
    client: AIProjectClient,
    name: str,
    model: str,
    instructions: str,
    tools: Optional[Sequence[Any]],
    reasoning_effort: Optional[str],
) -> None:
    """Materialise the versioned prompt agent via ``create_version``.

    When ``reasoning_effort`` is set the definition is built directly so the
    ``reasoning`` field can be embedded (the Agent Framework ``create_agent``
    helper does not surface ``reasoning`` on the definition). Otherwise the
    framework helper is used so its tool-normalisation/response-format handling
    is preserved verbatim.
    """
    if not reasoning_effort:
        await provider.create_agent(
            name=name,
            model=model,
            instructions=instructions,
            tools=tools,
        )
        return

    if not _HAS_TOOL_CONVERTER and tools:
        # Refuse to create a reasoning-fingerprinted agent whose tool schema would
        # silently be dropped; failing fast avoids poisoning the create-once cache
        # with a misleading agent. (No tools => converter is not needed.)
        raise RuntimeError(
            "Cannot build the prompt agent definition: the Agent Framework tool "
            "converter (agent_framework_azure_ai._shared.to_azure_ai_tools) is "
            "unavailable in this build but tools were requested."
        )

    azure_tools = to_azure_ai_tools(normalize_tools(tools)) if tools else None
    definition = PromptAgentDefinition(
        model=model,
        instructions=instructions,
        tools=azure_tools,
        reasoning=Reasoning(effort=reasoning_effort),
    )
    await client.agents.create_version(agent_name=name, definition=definition)


def is_invalid_payload_error(exc: BaseException) -> bool:
    """True when ``exc`` is a Responses ``invalid_payload`` rejection of a
    run-time option (e.g. ``reasoning`` or ``max_output_tokens`` not being
    permitted alongside an agent reference).

    The Agent Framework wraps the underlying ``HttpResponseError`` in its own
    exception type, so match on the surfaced message rather than the type.
    """
    msg = str(getattr(exc, "message", "") or exc)
    return (
        "invalid_payload" in msg
        or "Not allowed when agent is specified" in msg
    )


async def stream_agent_run(
    agent: Any,
    user_message: Union[str, Sequence[Any]],
    *,
    thread: Any,
    options: Optional[dict] = None,
) -> AsyncIterator[Any]:
    """Stream ``agent.run_stream`` and, if the service rejects the run-time
    ``options`` as an invalid payload *before any output is produced*, retry once
    without the optional token limit.

    Retry requires a field-scoped "Not allowed when agent is specified"
    rejection of the supplied ``max_tokens`` option. Unrecognized or ambiguous
    diagnostics propagate rather than risking a duplicate run.
    This guards against deployments/models where even ``max_tokens``
    (``max_output_tokens``) is not permitted alongside an agent reference. The
    common path (options accepted) never triggers the retry. The retry only fires
    when nothing has been yielded yet, so it cannot duplicate streamed text; the
    rejection is a request-validation 400 that the service raises before mutating
    the thread.

    ``user_message`` may be a single string (the current ask, relying on the
    ``thread`` for prior context) or a complete ordered sequence of chat
    messages (the stateless hosted path, which replays history explicitly on
    every turn instead of resuming a service-managed thread).
    """
    produced = False
    try:
        async for chunk in agent.run_stream(user_message, thread=thread, options=options or {}):
            # Treat any yielded chunk (text, tool-call, metadata) as "produced":
            # once the stream has emitted anything the run is in flight and the
            # one-shot fallback below must not re-issue it.
            produced = True
            yield chunk
    except Exception as exc:
        message = str(getattr(exc, "message", "") or exc)
        if (
            produced
            or not options
            or "max_tokens" not in options
            or not is_invalid_payload_error(exc)
            or not re.search(
                r"\b(?:max_tokens|max_output_tokens)\b['\"]?\s*:\s*['\"]?"
                r"Not allowed when agent is specified",
                message,
            )
        ):
            raise
        logging.warning(
            "[AgentProviderV2] Run rejected run-time options as invalid payload; "
            "retrying without the optional token limit",
        )
        retry_options = dict(options)
        retry_options.pop("max_tokens", None)
        async for chunk in agent.run_stream(
            user_message,
            thread=thread,
            options=retry_options,
        ):
            yield chunk


async def _get_openai_client() -> Any:
    """Return a process-wide ``AsyncOpenAI`` client bound to the Foundry
    Responses endpoint, created once from the shared project client.

    Like the project client, it is intentionally never closed: it lives for the
    lifetime of the worker process and is shared by all requests.
    """
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    async with _openai_client_lock:
        if _openai_client is None:
            if _project_client is None:
                raise RuntimeError(
                    "AgentProviderV2 is not initialized; call get_provider() before "
                    "creating a conversation object."
                )
            _openai_client = _project_client.get_openai_client()
            logging.info("[AgentProviderV2] Initialized OpenAI (Responses) client")
    return _openai_client


async def ensure_conversation_id(conv: dict) -> str:
    """Return a stable server-side conversation id for the chat, creating a
    dedicated Responses *conversation object* once and resuming it every turn.

    The chat thread must be backed by a persistent conversation object rather
    than the previous turn's per-turn response id (``resp_``). When a thread is
    resumed from a ``resp_`` id, a follow-up turn that triggers a tool call
    chains the tool output to the wrong response: the resumed turn id overrides
    the in-loop response that actually holds the ``function_call``, so the
    service rejects the tool result with ``400 No tool call found for function
    call output`` (Azure/agent-landing-zone#505). Backing the thread with a conversation
    object keeps the ``function_call`` and its output in the same conversation,
    so chaining resolves on every turn.

    The id is cached on ``conv['thread_id']`` (tagged with the current backend by
    ``reset_legacy_thread``) so it is created on the first turn and reused
    thereafter.
    """
    thread_id = conv.get("thread_id")
    if thread_id:
        return thread_id
    client = await _get_openai_client()
    conversation = await client.conversations.create()
    conv["thread_id"] = conversation.id
    logging.info(
        "[AgentProviderV2] Created conversation object %s to back the chat thread",
        conversation.id,
    )
    return conversation.id


async def persist_conversation_turn(
    conversation_id: str,
    user_message: str,
    assistant_message: str,
) -> None:
    """Append a completed user/assistant turn to a managed Conversation.

    The pinned Azure Agent Framework chat client explicitly drops the Responses
    ``store`` option as unsupported. Persist the completed turn through the
    Conversations API instead so hosted compute replacements can resume the
    full managed history.
    """
    client = await _get_openai_client()
    assistant_item_id = f"msg_{uuid.uuid4().hex}"
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": user_message,
                }
            ],
        },
        {
            "type": "message",
            "id": assistant_item_id,
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": assistant_message,
                    "annotations": [],
                }
            ],
        },
    ]
    try:
        await client.conversations.items.create(
            conversation_id,
            items=items,
        )
    except Exception:
        if await _conversation_tail_matches(
            client,
            conversation_id,
            user_message,
            assistant_message,
            assistant_item_id=assistant_item_id,
        ):
            logging.warning(
                "[AgentProviderV2] Conversation turn create returned an error "
                "after the turn was committed; treating it as persisted (%s)",
                conversation_id,
            )
            return
        raise
    logging.info(
        "[AgentProviderV2] Persisted completed turn to conversation %s",
        conversation_id,
    )


def _conversation_item_text(item: Any) -> tuple[str | None, str]:
    """Return the role and concatenated text from a Conversation message."""
    role = getattr(item, "role", None)
    content = getattr(item, "content", None)
    if role not in ("user", "assistant") or not isinstance(content, list) or not content:
        raise ValueError("Invalid conversation message evidence")
    parts = [getattr(part, "text", None) for part in content]
    if any(not isinstance(text, str) for text in parts):
        raise ValueError("Invalid conversation text evidence")
    return role, "".join(parts)


async def _conversation_tail_matches(
    client: Any,
    conversation_id: str,
    user_message: str,
    assistant_message: str,
    *,
    assistant_item_id: str,
) -> bool:
    """Require the actual submitted assistant identity, not repeated text.

    The SDK accepts this id on ResponseOutputMessageParam and returns message
    ids in the listed tail. Missing/rewritten ids remain unconfirmed; this is
    not a remote idempotency guarantee.
    """
    try:
        page = await client.conversations.items.list(
            conversation_id,
            limit=2,
            order="desc",
        )
        data = list(page.data)
        if len(data) < 2:
            return False
        newest_role, newest_text = _conversation_item_text(data[0])
        previous_role, previous_text = _conversation_item_text(data[1])
        return (
            bool(assistant_item_id)
            and getattr(data[0], "id", None) == assistant_item_id
            and newest_role == "assistant"
            and newest_text == assistant_message
            and previous_role == "user"
            and previous_text == user_message
        )
    except Exception:
        logging.error(
            "[AgentProviderV2] Failed to reconcile ambiguous Conversation "
            "persistence",
        )
        return False


def reset_legacy_thread(conv: dict) -> None:
    """Drop a server ``thread_id`` created by a different agent backend.

    Legacy Assistants-API thread ids are not valid Responses conversation ids, so
    they are cleared whenever the stored backend tag does not match the current
    one. Fresh conversations are simply tagged with the current backend.
    """
    if conv.get("agent_backend") != AGENT_BACKEND_TAG:
        if conv.get("thread_id"):
            logging.info(
                "[AgentProviderV2] Resetting incompatible thread_id (stored backend=%s)",
                conv.get("agent_backend"),
            )
        conv["thread_id"] = None
        conv["agent_backend"] = AGENT_BACKEND_TAG
