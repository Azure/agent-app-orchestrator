"""Dependency-neutral input and output contracts for one orchestration turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from connectors.obo import DelegatedRetrievalCredentials


@dataclass(slots=True)
class TurnRequest:
    """Input required by the orchestration core for one conversational turn."""

    ask: str
    conversation_id: str | None = None
    question_id: str | None = None
    user_context: dict[str, Any] = field(default_factory=dict)
    request_access_token: str | None = None
    correlation_id: str | None = None
    # Opaque, per-request Foundry hosted-agent call id
    # ("x-agent-foundry-call-id"), validated at the HTTP boundary.
    # Toolbox-integrated hosted strategies echo this outbound so Toolbox can
    # resolve the signed-in user and apply native document-level security.
    # Never log this value.
    foundry_call_id: str | None = None
    delegated_retrieval: DelegatedRetrievalCredentials | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class TurnConversationEvent:
    """Announce the conversation identity selected for the turn."""

    conversation_id: str


@dataclass(frozen=True, slots=True)
class TurnTextEvent:
    """A streamed assistant text fragment."""

    text: str


@dataclass(frozen=True, slots=True)
class TurnCitation:
    """Structured source attribution associated with an assistant response."""

    citation_id: str
    title: str | None = None
    url: str | None = None
    snippet: str | None = None


@dataclass(frozen=True, slots=True)
class TurnCitationEvent:
    """A citation plus its optional classic-rendering text."""

    citation: TurnCitation
    text: str = ""


class TurnToolStatus(str, Enum):
    """Lifecycle states exposed for a tool invocation."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnToolActivity:
    """Structured progress for a tool invocation."""

    tool_name: str
    status: TurnToolStatus
    call_id: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class TurnToolActivityEvent:
    """Tool activity plus its optional classic-rendering text."""

    activity: TurnToolActivity
    text: str = ""


@dataclass(frozen=True, slots=True)
class TurnErrorEvent:
    """A safe terminal error for runtime adapters."""

    message: str = "An internal server error occurred."
    code: str = "internal_error"
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class TurnCancelledEvent:
    """Signal cancellation before propagating the cancellation to the caller."""

    reason: str = "cancelled"


TurnOutputEvent = (
    TurnConversationEvent
    | TurnTextEvent
    | TurnCitationEvent
    | TurnToolActivityEvent
    | TurnErrorEvent
    | TurnCancelledEvent
)
