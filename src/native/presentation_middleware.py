"""Public SDK composition; no host builder, transport or transcript overrides."""

import asyncio
import copy
from contextvars import ContextVar
from uuid import uuid4
from typing import Any
from collections.abc import Awaitable, Callable, AsyncIterator, Sequence

from agent_framework import (
    AgentContext, AgentResponse, AgentResponseUpdate, BaseChatClient, Content,
    FunctionInvocationContext, Message, ResponseStream, function_middleware,
)
from jsonschema.exceptions import ValidationError
from azure.core.exceptions import AzureError
from openai import OpenAIError

from .presentation import (
    MARKER, MAX_BYTES, compact, diagnostic, guard_primary, hydrate, loads, normalize, supervise, validator,
)

_capture: ContextVar[list[tuple[Any, str, str]] | None] = ContextVar("presentation_sources", default=None)


@function_middleware
async def capture_result(context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
    await call_next()
    captured = _capture.get()
    if captured is not None:
        # This is the successful public invocation outcome, not payload authority.
        result = context.result
        # Public FunctionTool.invoke returns a content list on Core 1.24.
        # Only a single structural text result is eligible, never approvals,
        # arbitrary multimodal concatenation or partial text fragments.
        if isinstance(result, list) and len(result) == 1 and isinstance(result[0], Content):
            result = result[0]
        if isinstance(result, Content) and result.type == "text":
            result = result.text
        if isinstance(result, (dict, str)) and len(captured) <= 128:
            captured.append((result, context.function.name, uuid4().hex))


async def stage(client: BaseChatClient, captured: list[tuple[Any, str, str]], agent_id: str,
                revision: str, guidance: str, intent: str,
                rules: Sequence[dict[str, Any]] = ()) -> dict[str, Any] | None:
    # Offload synchronous bounded parsing/hydration so deadline supervision stays responsive.
    sources = []
    if len(captured) > 128:
        raise ValueError("source registry bounds")
    for result, tool, invocation in captured:
        try:
            sources.append(await asyncio.to_thread(normalize, result, tool, invocation))
        except (ValueError, TypeError, KeyError, ValidationError, RecursionError):
            diagnostic("presentation_source_rejected", "unsupported_structure", source_count=1)
    if not sources:
        return None
    metadata = [{k: source[k] for k in ("sourceId", "fields", "rows")} for source in sources]
    serialized_metadata = await asyncio.to_thread(compact, metadata)
    if len(serialized_metadata.encode("utf-8")) > MAX_BYTES:
        raise ValueError("source registry byte bounds")
    selected = await client.get_response([
        Message("system", ["Select source bindings and layout only. Do not invent measurements or rules. " + guidance]),
        Message("user", [intent + "\nAuthorized typed results:\n" + serialized_metadata]),
    ], options={"tools": [], "response_format": {
        "type": "json_schema", "json_schema": {
            "name": "presentation_selection", "schema": validator("presentation-selection").schema,
            "strict": False,
        },
    }})
    selection = await asyncio.to_thread(loads, selected.text)
    return await asyncio.to_thread(hydrate, selection, sources, agent_id, revision, rules)


async def compose(context: AgentContext, call_next: Callable[[], Awaitable[None]],
                  client: BaseChatClient, selected: tuple[dict[str, Any], str] | None, agent_id: str,
                  *, workflow_ordinal: int = 0) -> None:
    captured: list[tuple[Any, str, str]] = []
    token = _capture.set(captured if selected is not None else None)
    try:
        await call_next()
    finally:
        _capture.reset(token)
    config, revision = selected if selected is not None else ({}, "")
    intent = "\n".join(message.text for message in context.messages if message.role == "user")

    async def finish(primary: AgentResponse) -> AgentResponse:
        # Never mutate the original/native completed response in a late child task.
        result = copy.copy(primary)
        result.messages = copy.deepcopy(primary.messages)
        for message in result.messages:
            if message.role == "assistant":
                for content in message.contents:
                    if content.type == "text":
                        content.text = guard_primary(content.text or "")
        async def serialized_stage() -> tuple[str, str] | None:
            try:
                envelope = await stage(client, captured, agent_id, revision, config.get("guidance", ""), intent, config.get("rules", ()))
                if envelope is None:
                    return None
                envelope["contributions"][0]["workflowOrdinal"] = workflow_ordinal
                carrier = await asyncio.to_thread(compact, envelope)
                if len(carrier.encode("utf-8")) > MAX_BYTES:
                    raise ValueError("aggregate byte bounds")
                return envelope["presentationId"], carrier
            except (ValueError, TypeError, KeyError, ValidationError, RecursionError, AzureError, OpenAIError, RuntimeError):
                diagnostic("presentation_rejected", "selection_or_envelope", source_count=len(captured))
                raise
        try:
            accepted = (await supervise(serialized_stage())
                        if selected is not None else None)
        except (ValueError, TypeError, KeyError, ValidationError, RecursionError, AzureError, OpenAIError, RuntimeError):
            accepted = None
        if accepted is not None:
            identity, carrier = accepted
            result.messages.append(Message("assistant", [carrier], message_id=identity))
        return result

    if not context.stream:
        context.result = await finish(context.result)
        return
    original = context.result
    completed: AgentResponse | None = None

    async def close_original() -> None:
        try:
            await original.close()
        except (RuntimeError, OSError, AzureError, OpenAIError):
            diagnostic("presentation_cleanup_failed", "native_stream_cleanup", source_count=len(captured))
            raise  # Preserve the native transport/cleanup failure; never log its payload.

    if selected is None:
        async def ordinary_updates() -> AsyncIterator[AgentResponseUpdate]:
            nonlocal completed
            tails: dict[str, str] = {}
            identities: dict[str, str | None] = {}
            try:
                async for update in original:
                    guarded = copy.deepcopy(update)
                    if guarded.role in ("assistant", None):
                        for content in guarded.contents:
                            if content.type == "text":
                                key = guarded.message_id or "default"
                                text = guard_primary(tails.get(key, "") + (content.text or ""))
                                cut = max(0, len(text) - len(MARKER) + 1)
                                content.text, tails[key] = text[:cut], text[cut:]
                                identities[key] = guarded.message_id
                    yield guarded
                for key, tail in tails.items():
                    if tail:
                        yield AgentResponseUpdate(role="assistant", message_id=identities[key],
                                                  contents=[Content.from_text(tail)])
                completed = await finish(await original.get_final_response())
            finally:
                await close_original()
        context.result = ResponseStream(ordinary_updates(), finalizer=lambda _: completed)
        return

    async def updates() -> AsyncIterator[AgentResponseUpdate]:
        nonlocal completed
        try:
            # Buffer enabled primary text to guard split-marker imitations atomically.
            iterator = original.__aiter__()
            while True:
                token = _capture.set(captured)
                try:
                    update = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                finally:
                    # Never leave a ContextVar token active across an async yield:
                    # public finalization/close may resume from another task.
                    _capture.reset(token)
                forwarded = copy.copy(update)
                forwarded.contents = [c for c in update.contents if c.type != "text"]
                if forwarded.contents:
                    yield forwarded
            primary = await original.get_final_response()
            completed = await finish(primary)
            for message in completed.messages:
                if message.role == "assistant":
                    text_contents = [c for c in message.contents if c.type == "text"]
                    if text_contents:
                        yield AgentResponseUpdate(role="assistant", message_id=message.message_id or uuid4().hex,
                                                  contents=text_contents)
        finally:
            await close_original()

    context.result = ResponseStream(updates(), finalizer=lambda _: completed)
