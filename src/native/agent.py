"""Shared native Agent definition, not a strategy selector or state abstraction."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from agent_framework import Agent, AgentContext, agent_middleware
from agent_framework_foundry import FoundryChatClient

from util.foundry_platform import FOUNDRY_CALL_ID_HEADER

from .config import FunctionalSnapshot, NativeConfig
from .contracts import AGENT_NAME
from .knowledge import KNOWLEDGE_TOOL, enforce_tool_outcomes, knowledge_toolbox
from .tools import ToolIdentity, compose_tools
from .presentation import settings as presentation_settings
from .presentation_middleware import capture_result, compose as compose_presentation

_instructions = (Path(__file__).parent / "prompts" / "main.txt").read_text(encoding="utf-8")


def hosted_identity(auth_mode: str = "user_delegated") -> ToolIdentity:
    from azure.ai.agentserver.core import get_request_context
    from util.foundry_platform import require_foundry_call_id

    if auth_mode not in {"user_delegated", "service_identity"}:
        raise ValueError("Unsupported HOSTED_AGENT_AUTH_MODE.")
    context = get_request_context()
    if context is None or not context.user_id:
        if auth_mode == "service_identity":
            raise ValueError(
                "The pinned native host requires a trusted platform caller partition and call ID "
                "for service callers too. No end-user identity can be fabricated; "
                "the gateway must supply the service principal's native storage context."
            )
        raise ValueError("Hosted tools require trusted platform user context.")
    # user_id is an opaque SDK storage partition, not a document authorization
    # assertion. App-only callers must receive it from the platform, never the UI.
    call_id = require_foundry_call_id(context.platform_headers())
    return ToolIdentity(call_id=call_id)


def create_agent(
    config: NativeConfig, *, snapshot: FunctionalSnapshot | None = None,
    client: Any = None, identity_provider: Callable[[], ToolIdentity] = ToolIdentity,
    definition_id: str = AGENT_NAME, workflow_ordinal: int = 0,
) -> Agent:
    if not definition_id or type(workflow_ordinal) is not int or workflow_ordinal < 0:
        raise ValueError("Presentation requires a stable definition ID and nonnegative occurrence ordinal.")
    settings = config.settings
    if settings is None:
        raise ValueError("Native configuration must be opened before building an agent.")
    chat_client = client or FoundryChatClient(
        project_endpoint=settings.project_endpoint, model=settings.model,
        credential=config.sync_credential, timeout=60,
    )
    toolbox = (
        knowledge_toolbox(settings.toolbox_endpoint, config.sync_credential)
        if settings.toolbox_endpoint else None
    )

    @agent_middleware
    async def configure(context: AgentContext, call_next: Callable[[], Awaitable[None]]) -> None:
        view = await config.view(snapshot)
        selected = presentation_settings(view.get("AGENT_PRESENTATION_SETTINGS", None, type=None), definition_id)
        identity = identity_provider()
        if identity.call_id:
            context.client_kwargs = {
                **dict(context.client_kwargs or {}),
                "extra_headers": {FOUNDRY_CALL_ID_HEADER: identity.call_id},
            }
        resources = AsyncExitStack()
        defer_cleanup = False
        try:
            managed = list(toolbox.functions) if toolbox is not None else []
            if toolbox is not None and not any(tool.name == KNOWLEDGE_TOOL for tool in managed):
                raise ValueError("The configured Toolbox does not expose knowledge_base_retrieve.")
            context.tools = [*managed, *await compose_tools(view, identity, resources)]
            context.options = {
                **dict(context.options or {}),
                "temperature": view.get("CHAT_TEMPERATURE", 0.2, type=float),
                "max_tokens": view.get("CHAT_OUTPUT_TOKENS", 2048, type=int),
            }
            if context.stream:
                # Native middleware owns lazy-stream cleanup, not a browser socket.
                context.stream_cleanup_hooks.append(resources.aclose)
            await compose_presentation(context, call_next, chat_client, selected, definition_id,
                                       workflow_ordinal=workflow_ordinal)
            defer_cleanup = context.stream
        finally:
            if not defer_cleanup:
                await resources.aclose()

    return Agent(
        client=chat_client,
        name=definition_id, id=definition_id, instructions=_instructions,
        tools=toolbox,
        middleware=[configure, enforce_tool_outcomes(settings.auth_mode), capture_result],
    )


def agent_factory(
    config: NativeConfig, identity_provider: Callable[[], ToolIdentity] = ToolIdentity,
) -> Callable[[FunctionalSnapshot | None], AbstractAsyncContextManager[Agent]]:
    @asynccontextmanager
    async def factory(snapshot: FunctionalSnapshot | None = None) -> AsyncIterator[Agent]:
        async with create_agent(config, snapshot=snapshot, identity_provider=identity_provider) as agent:
            yield agent
    return factory
