"""Shared native Agent definition, not a strategy selector or state abstraction."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from agent_framework import Agent, AgentContext, agent_middleware
from agent_framework_foundry import FoundryChatClient

from .config import FunctionalSnapshot, NativeConfig
from .contracts import AGENT_NAME
from .tools import ToolIdentity, compose_tools

_instructions = (Path(__file__).parent / "prompts" / "main.txt").read_text(encoding="utf-8")


def hosted_identity() -> ToolIdentity:
    from azure.ai.agentserver.core import get_request_context
    from util.foundry_platform import require_foundry_call_id

    context = get_request_context()
    if context is None or not context.user_id:
        raise ValueError("Hosted tools require trusted platform user context.")
    call_id = require_foundry_call_id(context.platform_headers())
    return ToolIdentity(call_id=call_id)


def create_agent(
    config: NativeConfig, *, snapshot: FunctionalSnapshot | None = None,
    client: Any = None, identity_provider: Callable[[], ToolIdentity] = ToolIdentity,
) -> Agent:
    settings = config.settings
    if settings is None:
        raise ValueError("Native configuration must be opened before building an agent.")
    identity = identity_provider()

    @agent_middleware
    async def configure(context: AgentContext, call_next: Callable[[], Awaitable[None]]) -> None:
        view = await config.view(snapshot)
        identity = identity_provider()
        resources = AsyncExitStack()
        defer_cleanup = False
        try:
            context.tools = await compose_tools(view, identity, resources)
            context.options = {
                **dict(context.options or {}),
                "temperature": view.get("CHAT_TEMPERATURE", 0.2, type=float),
                "max_tokens": view.get("CHAT_OUTPUT_TOKENS", 2048, type=int),
            }
            if context.stream:
                # Native middleware owns lazy-stream cleanup, not a browser socket.
                context.stream_cleanup_hooks.append(resources.aclose)
            await call_next()
            defer_cleanup = context.stream
        finally:
            if not defer_cleanup:
                await resources.aclose()

    return Agent(
        client=client or FoundryChatClient(
            project_endpoint=settings.project_endpoint, model=settings.model,
            credential=config.sync_credential, timeout=60,
            default_headers={"x-agent-foundry-call-id": identity.call_id} if identity.call_id else None,
        ),
        name=AGENT_NAME, id=AGENT_NAME, instructions=_instructions, middleware=[configure],
    )


def agent_factory(
    config: NativeConfig, identity_provider: Callable[[], ToolIdentity] = ToolIdentity,
) -> Callable[[FunctionalSnapshot | None], AbstractAsyncContextManager[Agent]]:
    @asynccontextmanager
    async def factory(snapshot: FunctionalSnapshot | None = None) -> AsyncIterator[Agent]:
        async with create_agent(config, snapshot=snapshot, identity_provider=identity_provider) as agent:
            yield agent
    return factory
