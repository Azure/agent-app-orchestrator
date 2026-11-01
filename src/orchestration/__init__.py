from __future__ import annotations

from typing import Any

from .turn import (
    TurnCancelledEvent,
    TurnCitation,
    TurnCitationEvent,
    TurnConversationEvent,
    TurnErrorEvent,
    TurnOutputEvent,
    TurnRequest,
    TurnTextEvent,
    TurnToolActivity,
    TurnToolActivityEvent,
    TurnToolStatus,
)

__all__ = [
    "TurnCancelledEvent",
    "TurnCitation",
    "TurnCitationEvent",
    "TurnConversationEvent",
    "TurnErrorEvent",
    "TurnOutputEvent",
    "TurnRequest",
    "TurnTextEvent",
    "TurnToolActivity",
    "TurnToolActivityEvent",
    "TurnToolStatus",
]


def __getattr__(name: str) -> Any:
    if name == "Orchestrator":
        raise RuntimeError("Classic orchestration is retired; use native.hosted or native.durable.")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")