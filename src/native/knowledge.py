"""Managed Foundry knowledge tools; credentials and user exchange stay in Foundry."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import (
    Content, FunctionInvocationContext, MiddlewareFailure, UserInputRequiredException,
    function_middleware,
)
from agent_framework.exceptions import ToolExecutionException
from agent_framework_foundry_hosting import FoundryToolbox
from azure.ai.agentserver.core import get_request_context
from mcp import MCPError

from util.foundry_platform import require_foundry_call_id

KNOWLEDGE_TOOL = "knowledge_base___knowledge_base_retrieve"


def trusted_toolbox_context(_: dict[str, Any]) -> dict[str, str]:
    context = get_request_context()
    if context is None or not context.user_id:
        raise ValueError("Managed KB tools require trusted platform caller context.")
    require_foundry_call_id(context.platform_headers())
    # FoundryToolbox itself resolves and forwards CURRENT platform headers.
    return {}


def knowledge_toolbox(endpoint: str, credential: Any) -> FoundryToolbox:
    return FoundryToolbox(
        credential, url=endpoint, name="knowledge_base",
        header_provider=trusted_toolbox_context,
        allowed_tools=[KNOWLEDGE_TOOL], approval_mode="never_require",
    )


def consent_content(error: ToolExecutionException) -> list[Content]:
    """Decode the documented Toolbox gateway consent envelope, not tool text."""
    inner = next((arg for arg in error.args if isinstance(arg, MCPError)), None)
    if inner is None or inner.code != -32006:
        return []
    message = inner.error.message
    start = message.find("{")
    if start < 0:
        return []
    try:
        payload = json.loads(message[start:])
    except ValueError:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
        return []
    contents = []
    for source in payload["errors"]:
        detail = source.get("error") if isinstance(source, dict) and source.get("type") == "mcp" else None
        if (
            isinstance(detail, dict) and detail.get("code") == "CONSENT_REQUIRED"
            and isinstance(detail.get("message"), str)
        ):
            contents.append(Content.from_oauth_consent_request(
                detail["message"], additional_properties={"server_label": "knowledge_base"},
            ))
    return contents


def enforce_tool_outcomes(auth_mode: str) -> Any:
    @function_middleware
    async def enforce(context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if context.function.name == "multimodal_retrieve" and auth_mode == "service_identity":
            raise MiddlewareFailure(
                "Multimodal retrieval requires user delegation (OBO). "
                "Enable OBO for this application before using this tool."
            )
        if context.function.name != KNOWLEDGE_TOOL:
            await call_next()
            return
        try:
            await call_next()
        except ToolExecutionException as error:
            consent = consent_content(error) if auth_mode == "user_delegated" else []
            if consent:
                raise UserInputRequiredException(consent) from None
            # MAF normally turns tool exceptions into model-visible text. This
            # managed authorization boundary must instead abort the invocation.
            message = (
                "The source could not be accessed with service identity. Check its service permissions. "
                "If it requires user delegation (OBO), enable OBO for this application."
                if auth_mode == "service_identity" else
                "Managed knowledge retrieval failed; check authorization and tool configuration."
            )
            raise MiddlewareFailure(message) from None
    return enforce
