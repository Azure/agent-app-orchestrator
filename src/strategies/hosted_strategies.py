"""Hosted runtime strategy guard.

Only the strategies listed in ``HOSTED_ELIGIBLE_STRATEGIES`` are approved for
the shared hosted runtime (Foundry hosted agents, no-panel mode).  Any other
strategy must fail explicitly rather than silently falling back.

The hosted runtime is history-blind and stateless: it performs zero managed
Foundry Conversations data-plane operations (no create/read/append/delete, no
Conversations client construction). The authenticated UI BFF owns managed
Conversation lifecycle exclusively and sends the complete, bounded, ordered
history on every turn; the hosted container only ever replays that history
locally for the current request and never persists or resolves it against a
service-managed identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from agent_framework import ChatMessage, Role

# Strategies that call out to the Toolbox MCP server for retrieval and must
# therefore carry a validated Foundry call id (ADR-0001, Azure/agent-landing-zone#591).
# Other hosted-eligible strategies use the classic Foundry IQ / OBO retrieval
# path and are out of scope for this passthrough.
HOSTED_TOOLBOX_STRATEGIES: frozenset[str] = frozenset({
    "mcp",
})

# ADR-eligible strategies for the hosted runtime.
# multimodal and nl2sql are excluded pending further ADR investigation.
HOSTED_ELIGIBLE_STRATEGIES: frozenset[str] = frozenset({
    "maf_lite",
    "maf_agent_service",
    "single_agent_rag",
    "mcp",
})

_ROLE_BY_NAME: dict[str, Role] = {
    "system": Role.SYSTEM,
    "user": Role.USER,
    "assistant": Role.ASSISTANT,
}


class HostedConversationMessage(TypedDict):
    role: str
    text: str


def build_hosted_conversation(
    strategy_key: str,
    conversation_id: str,
    messages: Sequence[HostedConversationMessage],
) -> dict:
    """Build request-local strategy state from the caller-supplied history.

    The complete ordered prior history is copied so strategy mutations cannot
    leak into another request. The resulting dict never carries a
    service-managed thread/conversation identity: the hosted runtime does not
    create, read, append to, or delete any Foundry Conversation, so there is
    nothing here for a caller-selected id to redirect.
    """
    del strategy_key  # no longer distinguishes server-thread binding
    return {
        "id": conversation_id,
        "messages": [dict(message) for message in messages],
    }


def build_stateless_messages(
    history: Sequence[HostedConversationMessage],
    user_message: str,
) -> list[ChatMessage]:
    """Replay the caller-supplied ordered history as local chat messages.

    Used by hosted-eligible strategies that would otherwise bind their
    underlying model call to a service-managed thread/conversation. Passing
    the complete history explicitly on every turn keeps the call fully
    stateless: no server-side conversation object is created, read, or
    resumed, so there is no service identity a caller-selected id could
    redirect.
    """
    replayed: list[ChatMessage] = []
    for entry in history:
        role = _ROLE_BY_NAME.get(entry.get("role"))
        text = entry.get("text")
        if role is None or not text:
            continue
        replayed.append(ChatMessage(role=role, text=text))
    replayed.append(ChatMessage(role=Role.USER, text=user_message))
    return replayed


def guard_hosted_strategy(key: str) -> None:
    """Raise :class:`ValueError` explicitly when *key* is not hosted-eligible.

    The hosted runtime must never silently fall back to an unsupported strategy.
    Callers that need a user-friendly HTTP error should catch this and map it to
    a 422 or 400 response.
    """
    if key not in HOSTED_ELIGIBLE_STRATEGIES:
        raise ValueError(
            f"Strategy '{key}' is not supported in the hosted runtime. "
            f"Eligible strategies: {sorted(HOSTED_ELIGIBLE_STRATEGIES)}"
        )
