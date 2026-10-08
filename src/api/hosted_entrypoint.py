"""Hosted agent entrypoint for Azure AI Foundry.

This ASGI application wires the runtime-neutral orchestration core to the
Foundry Responses protocol.  It is intentionally separate from the
classic ``main.py`` so that:

- No Cosmos DB or orchestrator Container Apps dependency exists in the hosted
  execution path. The hosted runtime is history-blind and stateless: it
  performs zero managed Foundry Conversations data-plane operations (no
  create/read/append/delete, no Conversations client construction). The
  authenticated UI BFF owns managed Conversation lifecycle exclusively and
  sends the complete, bounded, ordered history on every request.
- Every ``POST /responses`` unconditionally forces ``store: False`` before the
  pinned ``azure-ai-agentserver-responses`` host processes it (ADR-0004),
  regardless of what a caller sends or omits. This fails closed against the
  host's own auto-activated, network-bound Foundry storage provider, which
  the hosted container has no RBAC to use and which otherwise raises a
  platform ``storage_error`` under network isolation whenever a caller's
  ``store`` is left unset (the Responses default is ``true``) or explicit
  ``true``. See ``HostedResponsesAgentServerHost._create_endpoint``.
- Only ADR-eligible strategies are admitted; unsupported strategies fail
  explicitly rather than silently falling back.
- The hosted image is immutable: the ``VERSION`` file is read once at startup
  and served on the ``GET /readiness`` and compatibility ``GET /health``
  endpoints.

Usage::

    uvicorn api.hosted_entrypoint:app --host 0.0.0.0 --port 8001

Or via Docker by overriding the entrypoint command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, Sequence, cast

from azure.ai.agentserver.core import (
    configure_observability as configure_agentserver_observability,
)
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponseProviderProtocol,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
)
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from api.responses_adapter import responses_terminal_events, serialize_responses_events
from dependencies import get_config
from orchestration.turn import (
    TurnCancelledEvent,
    TurnCitationEvent,
    TurnConversationEvent,
    TurnErrorEvent,
    TurnOutputEvent,
    TurnRequest,
    TurnTextEvent,
    TurnToolActivityEvent,
)
from strategies.agent_strategy_factory import AgentStrategyFactory
from strategies.hosted_strategies import (
    HOSTED_ELIGIBLE_STRATEGIES,
    HOSTED_TOOLBOX_STRATEGIES,
    HostedConversationMessage,
    build_hosted_conversation,
    guard_hosted_strategy,
)
from util.foundry_platform import (
    MISSING_CALL_CONTEXT_MESSAGE,
    MissingFoundryCallContextError,
    require_foundry_call_id,
)
from util.otel_context_noise import silence_context_detach_noise

logger = logging.getLogger(__name__)

# ── Immutable version (read once; never changes at runtime) ──────────────────
_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
try:
    _APP_VERSION: str = _VERSION_FILE.read_text().strip()
except FileNotFoundError:
    _APP_VERSION = "0.0.0"


# ── Pydantic schema for the legacy Invocations protocol ─────────────────────

class InvocationMessage(BaseModel):
    """One message turn in the inbound invocation request."""

    role: Literal["user", "assistant"] = Field(
        ...,
        description="Message role: 'user' or 'assistant'",
    )
    content: str = Field(..., description="Message text content")


class InvocationRequest(BaseModel):
    """Inbound invocation from the Foundry runtime to the hosted agent.

    The final message is the current user ask. An optional ``conversation_id``
    is an opaque caller-supplied label echoed back for response tagging and
    conversation-scoped retrieval; it never selects, creates, or reads a
    managed Foundry Conversation (the hosted runtime is stateless and
    performs zero Conversations data-plane operations).
    """

    messages: list[InvocationMessage] = Field(
        ...,
        min_length=1,
        description="Ordered message history; the final message is the current user ask.",
    )
    conversation_id: Optional[str] = Field(
        None,
        description=(
            "Opaque caller-supplied conversation label, echoed back and used "
            "only for conversation-scoped retrieval. Generated when omitted."
        ),
    )
    metadata: Optional[dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Request metadata such as correlation_id and question_id. "
            "Caller-provided identity fields are not trusted by hosted mode."
        ),
    )


class HealthResponse(BaseModel):
    """Liveness/readiness response surfacing the immutable image version."""

    status: str = "ok"
    version: str
    eligible_strategies: list[str]


# ── Hosted execution path (no Cosmos) ───────────────────────────────────────

async def _hosted_stream(
    turn: TurnRequest,
    strategy_key: str,
    history: Sequence[HostedConversationMessage] = (),
) -> AsyncIterator[TurnOutputEvent]:
    """Run one turn with caller-supplied history and no Cosmos-backed runtime
    state.

    The caller supplies the complete ordered prior history. The hosted runtime
    performs zero managed Foundry Conversations data-plane operations: it never
    constructs a Conversations client and never creates, reads, appends to, or
    deletes a managed Conversation. The turn's conversation id is either the
    caller-supplied opaque label (used only for response tagging and
    conversation-scoped retrieval) or a freshly generated one; it never selects
    or recreates access to any service-managed identity. Per-user profile
    memory remains disabled because the hosted invocation contract does not yet
    expose an authenticated Foundry identity.

    Raises :class:`ValueError` for unsupported strategies — never silently
    falls back. Raises :class:`~util.foundry_platform.MissingFoundryCallContextError`
    (a :class:`ValueError` subclass) when *strategy_key* is Toolbox-integrated
    and the turn carries no validated Foundry call id — hosted retrieval must
    fail closed rather than fall back to service identity or a manual filter.
    """
    guard_hosted_strategy(strategy_key)
    if strategy_key in HOSTED_TOOLBOX_STRATEGIES and not turn.foundry_call_id:
        raise MissingFoundryCallContextError(MISSING_CALL_CONTEXT_MESSAGE)
    strategy = await AgentStrategyFactory.get_strategy(
        strategy_key,
        hosted_runtime=True,
    )

    conversation_id = turn.conversation_id or str(uuid.uuid4())

    if hasattr(strategy, "set_context"):
        strategy.set_context(conversation_id)

    strategy.user_context = {}
    strategy.foundry_call_id = turn.foundry_call_id
    strategy.conversation = build_hosted_conversation(
        strategy_key,
        conversation_id,
        history,
    )

    yield TurnConversationEvent(conversation_id=conversation_id)

    try:
        async for chunk in strategy.initiate_agent_flow(turn.ask):
            if isinstance(chunk, (TurnCitationEvent, TurnToolActivityEvent)):
                yield chunk
            elif isinstance(chunk, str):
                yield TurnTextEvent(text=chunk)
            else:
                raise TypeError(
                    f"Strategy emitted unsupported chunk type: {type(chunk).__name__}"
                )
    except asyncio.CancelledError:
        yield TurnCancelledEvent()
        raise
    except Exception:
        yield TurnErrorEvent()
        raise


def _sse_generator(
    turn: TurnRequest,
    strategy_key: str,
    response_id: str,
    item_id: str,
    history: Sequence[HostedConversationMessage] = (),
    *,
    model: str = "unknown",
    response_metadata: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    """Wrap ``_hosted_stream`` and serialize events to Responses API SSE."""

    async def _gen() -> AsyncIterator[str]:
        full_text: list[str] = []
        error_emitted = False
        managed_conversation_id: str | None = None
        sequence_number = 0
        created_at = time.time()

        try:
            async for event in _hosted_stream(turn, strategy_key, history):
                if isinstance(event, TurnConversationEvent):
                    managed_conversation_id = event.conversation_id
                frames = serialize_responses_events(
                    event,
                    response_id=response_id,
                    item_id=item_id,
                    sequence_number=sequence_number,
                    created_at=created_at,
                    model=model,
                    response_metadata=response_metadata,
                )
                if isinstance(event, (TurnErrorEvent, TurnCancelledEvent)):
                    error_emitted = True
                if isinstance(event, TurnTextEvent):
                    full_text.append(event.text)
                for frame in frames:
                    yield frame
                sequence_number += len(frames)

            # Emit closing frames only when no error occurred.
            if not error_emitted:
                if managed_conversation_id is None:
                    raise ValueError(
                        "Hosted stream ended without a managed conversation id."
                    )
                for frame in responses_terminal_events(
                    response_id=response_id,
                    item_id=item_id,
                    conversation_id=managed_conversation_id,
                    full_text="".join(full_text),
                    created_at=created_at,
                    model=model,
                    sequence_number=sequence_number,
                    response_metadata=response_metadata,
                ):
                    yield frame

        except MissingFoundryCallContextError as exc:
            # Defense-in-depth only: the normal ``/invocations`` route rejects a
            # missing/malformed call id with an HTTP 401 *before*
            # ``StreamingResponse`` is constructed, so this branch cannot be
            # reached from a real HTTP request today. It exists solely to
            # correctly classify the error if some future/internal caller
            # ever invokes ``_hosted_stream``/``_sse_generator`` directly and
            # bypasses that precheck. By the time this generator body is
            # running, SSE response headers (HTTP 200) are already committed
            # to the client -- there is no way to retroactively send a 401
            # here. We can only emit a distinctly-coded SSE error frame
            # instead of silently downgrading it to a generic
            # ``internal_error`` frame via the broad ``except Exception``
            # below.
            logger.warning(
                "[hosted] Missing Foundry call context reached SSE generator "
                "directly (bypassed /invocations precheck) for strategy=%s",
                strategy_key,
            )
            if not error_emitted:
                for frame in serialize_responses_events(
                    TurnErrorEvent(
                        code="missing_call_context",
                        message=str(exc),
                        retryable=False,
                    ),
                    response_id=response_id,
                    item_id=item_id,
                    sequence_number=sequence_number,
                ):
                    yield frame

        except Exception:
            logger.error("[hosted] Unhandled error in SSE generator (internal_error)")
            if not error_emitted:
                for frame in serialize_responses_events(
                    TurnErrorEvent(
                        code="internal_error",
                        message="An internal server error occurred.",
                        retryable=False,
                    ),
                    response_id=response_id,
                    item_id=item_id,
                    sequence_number=sequence_number,
                ):
                    yield frame

    return _gen()


# ── Hosted protocol application ──────────────────────────────────────────────

async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=_APP_VERSION,
        eligible_strategies=sorted(HOSTED_ELIGIBLE_STRATEGIES),
    )


async def _health_endpoint(_request: Request) -> JSONResponse:
    return JSONResponse((await health()).model_dump())


async def _create_hosted_streaming_response(
    request: Request,
    *,
    ask: str,
    conversation_id: Optional[str],
    metadata: dict[str, Any],
    history: Sequence[HostedConversationMessage] = (),
    response_metadata: dict[str, str] | None = None,
) -> StreamingResponse:
    """Build one hosted SSE response after protocol-specific validation."""
    # Resolve and guard the strategy.
    cfg = get_config()
    strategy_key = cfg.get("AGENT_STRATEGY", "maf_lite")
    model = cfg.get("CHAT_DEPLOYMENT_NAME", "unknown")

    try:
        guard_hosted_strategy(strategy_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Toolbox-integrated strategies must carry a validated, platform-injected
    # call id (ADR-0001, Azure/agent-landing-zone#591). Never trust Authorization,
    # caller/model identity fields, or x-client group claims here.
    foundry_call_id: Optional[str] = None
    if strategy_key in HOSTED_TOOLBOX_STRATEGIES:
        try:
            foundry_call_id = require_foundry_call_id(request.headers)
        except MissingFoundryCallContextError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    metadata = metadata or {}
    turn = TurnRequest(
        ask=ask,
        conversation_id=conversation_id,
        question_id=metadata.get("question_id"),
        user_context={},
        correlation_id=metadata.get("correlation_id"),
        foundry_call_id=foundry_call_id,
    )

    response_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"item_{uuid.uuid4().hex}"

    logger.info(
        "[hosted] response: strategy=%s conversation_id=%s response_id=%s",
        strategy_key,
        conversation_id or "∅",
        response_id,
    )

    return StreamingResponse(
        _sse_generator(
            turn,
            strategy_key,
            response_id,
            item_id,
            history,
            model=model,
            response_metadata=response_metadata,
        ),
        media_type="text/event-stream",
        headers={"X-Response-ID": response_id},
    )


async def invocations(request: Request, body: InvocationRequest) -> StreamingResponse:
    """Translate the legacy Foundry invocation contract into an SSE stream."""
    current_message = body.messages[-1]
    if current_message.role != "user":
        raise HTTPException(
            status_code=422,
            detail=(
                "The final message must have role='user'; messages after the "
                "current ask are not allowed."
            ),
        )
    ask = current_message.content.strip()
    if not ask:
        raise HTTPException(
            status_code=422,
            detail="The last user message must not be empty.",
        )

    history: list[HostedConversationMessage] = [
        {"role": message.role, "text": message.content}
        for message in body.messages[:-1]
    ]
    return await _create_hosted_streaming_response(
        request,
        ask=ask,
        conversation_id=body.conversation_id,
        metadata=body.metadata or {},
        history=history,
    )


async def _invocations_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """Validate the legacy request model before invoking its SSE handler."""
    try:
        body = InvocationRequest.model_validate(await request.json())
        return await invocations(request, body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"detail": "The Invocations request body must be valid JSON."},
            status_code=400,
        )
    except ValidationError as exc:
        return JSONResponse(
            {"detail": exc.errors(include_url=False)},
            status_code=422,
        )
    except HTTPException as exc:
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )


def _responses_history(
    items: Sequence[dict[str, Any]],
) -> list[HostedConversationMessage]:
    """Project an ordered Responses message array into strategy chat history.

    Used both for the caller-supplied ``input`` array (canonical hosted path)
    and, historically, for platform-tracked history; items may omit ``type``
    entirely (bare ``{"role", "content"}`` input messages), so a missing
    ``type`` defaults to ``"message"``.
    """
    messages: list[HostedConversationMessage] = []
    for item in items:
        if item.get("type", "message") not in {"message", "output_message"}:
            continue
        role = item.get("role")
        if role == "developer":
            role = "system"
        if role not in {"user", "assistant", "system"}:
            continue
        content = item.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") in {"input_text", "output_text"}
                and isinstance(part.get("text"), str)
            )
        else:
            continue
        if text:
            messages.append({"role": role, "text": text})
    return messages


_RESPONSES_MESSAGE_ROLES = {"user", "assistant", "system", "developer"}


def _extract_message_text(item: dict[str, Any]) -> str:
    """Concatenate the text content of one Responses input message item."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"input_text", "output_text"}
            and isinstance(part.get("text"), str)
        )
    return ""


def _validate_responses_input_items(items: list[Any]) -> str | None:
    """Return a 422 detail string when *items* is not a well-formed ordered
    Responses input array, or ``None`` when it is valid.

    The canonical hosted path carries no separate conversation or
    previous-response state: the caller supplies the complete, bounded,
    ordered history explicitly as this array on every request. Each item must
    be a ``{"role", "content"}`` message object with a supported role and
    text-only content (a plain string, or a list of ``input_text``/
    ``output_text`` content parts); the final item must be the current user
    ask.
    """
    if not items:
        return "The Responses input array must not be empty."

    for item in items:
        if not isinstance(item, dict):
            return (
                "Each Responses input array item must be a role/content "
                "message object."
            )
        if item.get("role") not in _RESPONSES_MESSAGE_ROLES:
            return f"Unsupported Responses input role: {item.get('role')!r}."
        content = item.get("content")
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") not in {"input_text", "output_text"}
                    or not isinstance(part.get("text"), str)
                ):
                    return (
                        "This hosted adapter supports text-only Responses "
                        "input content."
                    )
            continue
        return "This hosted adapter supports text-only Responses input content."

    last = items[-1]
    if last.get("role") != "user":
        return "The final Responses input item must have role='user'."
    if not _extract_message_text(last).strip():
        return "The Responses input must not be empty."

    return None


async def _cancel_aware_events(
    events: AsyncIterator[TurnOutputEvent],
    cancellation_signal: asyncio.Event,
    shutdown_signal: asyncio.Event,
) -> AsyncIterator[TurnOutputEvent]:
    """Stop and close in-flight strategy work on cancellation or shutdown."""
    iterator = events.__aiter__()
    active_tasks: set[asyncio.Task[Any]] = set()
    try:
        while not cancellation_signal.is_set() and not shutdown_signal.is_set():
            next_event = asyncio.create_task(anext(iterator))
            cancelled = asyncio.create_task(cancellation_signal.wait())
            shutting_down = asyncio.create_task(shutdown_signal.wait())
            active_tasks.update({next_event, cancelled, shutting_down})
            done, _ = await asyncio.wait(
                active_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done or shutting_down in done:
                return

            cancelled.cancel()
            shutting_down.cancel()
            with suppress(asyncio.CancelledError):
                await cancelled
            with suppress(asyncio.CancelledError):
                await shutting_down
            active_tasks.clear()
            try:
                yield next_event.result()
            except StopAsyncIteration:
                return
    finally:
        for task in active_tasks:
            if not task.done():
                task.cancel()
        for task in active_tasks:
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await task
        if hasattr(iterator, "aclose"):
            await iterator.aclose()


async def _responses_events(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
    provider: ResponseProviderProtocol,
) -> AsyncIterator[Any]:
    """Adapt one strategy turn to the official Responses event lifecycle.

    The canonical hosted path is history-blind: top-level ``conversation``
    routing is discarded, while ``previous_response_id`` is rejected before
    this handler ever runs (see ``_create_endpoint``). The caller supplies the
    complete, bounded, ordered input on every request; a
    string ``input`` is the current ask with no prior turns, and an ordered
    message-array ``input`` carries the prior turns plus the current ask as
    its final ``user`` item.
    """
    del provider  # no longer used: no server-side state selector to resolve
    response_input = request.get("input")
    if isinstance(response_input, str):
        ask = response_input.strip()
        history: list[HostedConversationMessage] = []
    else:
        projected = _responses_history(
            cast(Sequence[dict[str, Any]], response_input)
        )
        if not projected or projected[-1]["role"] != "user":
            raise ValueError(
                "The final Responses input item must have role='user'."
            )
        ask = projected[-1]["text"].strip()
        history = projected[:-1]

    if not ask:
        raise ValueError("The Responses input must not be empty.")

    cfg = get_config()
    strategy_key = cfg.get("AGENT_STRATEGY", "maf_lite")
    guard_hosted_strategy(strategy_key)

    foundry_call_id: Optional[str] = None
    if strategy_key in HOSTED_TOOLBOX_STRATEGIES:
        platform_call_id = context.platform_context.call_id
        headers = (
            {"x-agent-foundry-call-id": platform_call_id}
            if platform_call_id is not None
            else {}
        )
        foundry_call_id = require_foundry_call_id(headers)

    metadata = request.get("metadata")
    turn = TurnRequest(
        ask=ask,
        conversation_id=None,
        question_id=metadata.get("question_id") if metadata else None,
        user_context={},
        correlation_id=metadata.get("correlation_id") if metadata else None,
        foundry_call_id=foundry_call_id,
    )

    response_stream: ResponseEventStream | None = None
    message = None
    text_content = None
    full_text: list[str] = []

    async for event in _cancel_aware_events(
        _hosted_stream(turn, strategy_key, history),
        cancellation_signal,
        context.shutdown,
    ):
        if isinstance(event, TurnConversationEvent):
            response_stream = ResponseEventStream(
                response_id=context.response_id,
                request=request,
            )
            response_stream.response.setdefault("tools", [])
            response_stream.response.setdefault("tool_choice", "auto")
            response_stream.response["conversation"] = {"id": event.conversation_id}
            yield response_stream.emit_created()
            yield response_stream.emit_in_progress()
            message = response_stream.add_output_item_message()
            yield message.emit_added()
            text_content = message.add_text_content()
            yield text_content.emit_added()
        elif isinstance(event, TurnTextEvent):
            if text_content is None:
                raise RuntimeError("Hosted strategy emitted text before conversation identity.")
            full_text.append(event.text)
            yield text_content.emit_delta(event.text)
        elif isinstance(event, TurnErrorEvent):
            raise RuntimeError(event.message)
        elif isinstance(event, TurnCancelledEvent):
            cancellation_signal.set()
            return

    if cancellation_signal.is_set() or context.shutdown.is_set():
        return

    if response_stream is None or message is None or text_content is None:
        raise RuntimeError("Hosted strategy did not emit a conversation identity.")

    final_text = "".join(full_text)
    yield text_content.emit_text_done(final_text)
    yield text_content.emit_done()
    yield message.emit_done()
    yield response_stream.emit_completed()


class HostedResponsesAgentServerHost(ResponsesAgentServerHost):
    """Responses host with the image's immutable readiness contract."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._responses_provider = cast(
            ResponseProviderProtocol,
            self._endpoint._provider,
        )
        for index, route in enumerate(self.routes):
            route_name = getattr(route, "name", None)
            if route_name not in {
                "create_response",
                "get_response",
                "delete_response",
                "cancel_response",
                "get_input_items",
            }:
                continue
            endpoint = cast(Callable[[Request], Awaitable[Response]], route.endpoint)
            guarded_endpoint = (
                self._create_endpoint(endpoint)
                if route_name == "create_response"
                else self._responses_endpoint(endpoint)
            )
            self.routes[index] = Route(
                route.path,
                guarded_endpoint,
                methods=sorted(route.methods or ()),
                name=route.name,
            )

    async def handle_response(
        self,
        request: CreateResponse,
        context: ResponseContext,
        cancellation_signal: asyncio.Event,
    ) -> AsyncIterator[Any]:
        async for event in _responses_events(
            request,
            context,
            cancellation_signal,
            self._responses_provider,
        ):
            yield event

    async def _readiness_endpoint(self, request: Request) -> JSONResponse:
        return await _health_endpoint(request)

    def _responses_endpoint(
        self,
        endpoint: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def guarded(request: Request) -> Response:
            call_context_error = self._call_context_error(request)
            if call_context_error is not None:
                return call_context_error
            return await endpoint(request)

        return guarded

    def _create_endpoint(
        self,
        endpoint: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def validated(request: Request) -> Response:
            def reject(detail: str, status_code: int = 422) -> JSONResponse:
                # A rejected request used to surface only in the caller's
                # response body, so Foundry-side failures were undiagnosable
                # from container telemetry. Log every rejection reason.
                logger.warning(
                    "Rejecting hosted Responses request (%s): %s",
                    status_code,
                    detail,
                )
                return JSONResponse({"detail": detail}, status_code=status_code)

            call_context_error = self._call_context_error(request)
            if call_context_error is not None:
                return call_context_error

            try:
                payload = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return reject(
                    "The Responses request body must be valid JSON.",
                    status_code=400,
                )

            if not isinstance(payload, dict) or "input" not in payload:
                return reject("The Responses request requires an input field.")

            # Foundry clients serialize unset optional Responses parameters as
            # explicit nulls. A null carries no caller intent, so treat those
            # keys as absent rather than as caller-supplied fields.
            for null_field in [
                key
                for key, value in payload.items()
                if value is None and key != "input"
            ]:
                payload.pop(null_field)

            # ``previous_response_id`` is the only request field this adapter
            # refuses: it is a server-side state selector that would recreate
            # the cross-turn history access the stateless runtime cannot
            # authorize (ADR-0004). Callers must resend the ordered input.
            unsupported_fields = sorted(
                set(payload).intersection({"previous_response_id"})
            )
            if unsupported_fields:
                return reject(
                    "Unsupported Responses request fields: "
                    + ", ".join(unsupported_fields)
                    + "."
                )

            # Every other field this adapter does not act on is dropped rather
            # than rejected. Foundry injects routing selectors the gateway has
            # already resolved (``conversation``, ``model``, ``session_id``)
            # plus client-side sampling parameters owned by the server-side
            # agent definition, and that set grows with the product. A strict
            # allowlist turned each newly injected client field into a hosted
            # agent outage, so ignore-and-log is the contract.
            honored_fields = {
                "agent_reference",
                "background",
                "input",
                "metadata",
                "store",
                "stream",
            }
            ignored_fields = sorted(set(payload) - honored_fields)
            if ignored_fields:
                logger.info(
                    "Ignoring Responses request fields %s; the hosted runtime "
                    "is stateless and its behavior is defined server-side.",
                    ", ".join(ignored_fields),
                )
                for ignored_field in ignored_fields:
                    payload.pop(ignored_field)

            response_input = payload["input"]
            if isinstance(response_input, str):
                if not response_input.strip():
                    return reject("The Responses input must not be empty.")
            elif isinstance(response_input, list):
                input_error = _validate_responses_input_items(response_input)
                if input_error is not None:
                    return reject(input_error)
            else:
                return reject(
                    "This hosted adapter supports string or ordered "
                    "message-array Responses input."
                )

            metadata = payload.get("metadata")
            if metadata is not None and (
                not isinstance(metadata, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in metadata.items()
                )
            ):
                return reject(
                    "Responses metadata must contain only string keys and values."
                )

            # The pinned SDK requires ``store: true`` whenever ``background:
            # true`` is requested (a queued/polled response is meaningless if
            # it can never be retrieved). This hosted runtime forces
            # ``store: False`` unconditionally below (ADR-0004), so
            # ``background: true`` can never be honored. Reject it outright
            # with an explicit, self-documenting error instead of letting the
            # caller-opaque SDK-level "background=true requires store=true"
            # 400 stand as the de-facto contract.
            if payload.get("background"):
                return reject(
                    "The Responses background parameter is not "
                    "supported by this hosted runtime: background=true "
                    "requires store=true, and every request is forced "
                    "to store=False (ADR-0004, zero managed-"
                    "Conversations RBAC, stateless hosted container)."
                )

            # ADR-0004: the hosted container holds zero managed-Conversations
            # data-plane RBAC and must never rely on Foundry managed
            # persistence. ``ResponsesAgentServerHost.__init__`` auto-activates
            # a network-bound ``FoundryStorageProvider`` whenever no explicit
            # ``store`` override is supplied and the process detects it is
            # running as a hosted agent, and the pinned SDK orchestrator only
            # calls that provider when the *caller's* ``store`` is true. Per
            # the OpenAI/Foundry Responses contract ``store`` defaults to
            # ``true`` when omitted, so an unset or explicit ``store: true``
            # request reaches that provider and fails closed with a platform
            # ``storage_error`` under network isolation, while ``store: false``
            # skips it and succeeds. Fail closed unconditionally instead of
            # trusting caller intent: force ``store: False`` for every create
            # call regardless of what was sent or omitted, so implicit managed
            # persistence is never attempted by any caller on any product
            # surface. Mutating ``payload`` here is sufficient — Starlette's
            # ``Request.json()`` caches the parsed body dict on the request
            # instance, so the SDK's own downstream ``await request.json()``
            # call returns this same, already-overridden object.
            if payload.get("store") is not False:
                logger.info(
                    "Hosted Responses request store=%r (omitted defaults to "
                    "true); overriding to store=False so Foundry managed "
                    "persistence is never attempted (ADR-0004 zero-RBAC "
                    "stateless hosted container).",
                    payload.get("store"),
                )
            payload["store"] = False

            return await endpoint(request)

        return validated

    @staticmethod
    def _call_context_error(request: Request) -> JSONResponse | None:
        strategy_key = get_config().get("AGENT_STRATEGY", "maf_lite")
        if strategy_key not in HOSTED_TOOLBOX_STRATEGIES:
            return None
        try:
            require_foundry_call_id(request.headers)
        except MissingFoundryCallContextError as exc:
            # Same observability contract as ``reject`` above: a refused
            # request must be diagnosable from container telemetry alone.
            logger.warning(
                "Rejecting hosted Responses request (401): %s", exc
            )
            return JSONResponse({"detail": str(exc)}, status_code=401)
        return None


_DEFAULT_OBSERVABILITY = object()


def _configure_host_observability(
    *,
    connection_string: str | None,
    log_level: str | None,
    enable_sensitive_data: bool,
) -> None:
    """Configure SDK telemetry with content capture disabled by default."""
    del enable_sensitive_data
    capture_setting = os.environ.get(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "false",
    )
    configure_agentserver_observability(
        connection_string=connection_string,
        log_level=log_level,
        enable_sensitive_data=capture_setting.strip().lower() in {"true", "1"},
    )
    # Applied after the SDK configures logging so the filter is not discarded.
    silence_context_detach_noise()


def create_app(
    *,
    store: ResponseProviderProtocol | None = None,
    configure_observability: Any = _DEFAULT_OBSERVABILITY,
) -> HostedResponsesAgentServerHost:
    """Create the multi-protocol hosted app with injectable test boundaries."""
    host_options: dict[str, Any] = {}
    if configure_observability is _DEFAULT_OBSERVABILITY:
        host_options["configure_observability"] = _configure_host_observability
    else:
        host_options["configure_observability"] = configure_observability

    hosted_app = HostedResponsesAgentServerHost(
        options=ResponsesServerOptions(
            additional_server_version=f"gpt-rag-orchestrator/{_APP_VERSION}",
        ),
        store=store,
        routes=[
            Route("/health", _health_endpoint, methods=["GET"], name="health"),
            Route(
                "/invocations",
                _invocations_endpoint,
                methods=["POST"],
                name="invocations",
            ),
        ],
        **host_options,
    )
    hosted_app.response_handler(hosted_app.handle_response)
    return hosted_app


app = create_app()
