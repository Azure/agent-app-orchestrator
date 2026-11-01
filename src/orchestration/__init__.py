from __future__ import annotations

from typing import Any

__all__: list[str] = []


def __getattr__(name: str) -> Any:
    if name == "Orchestrator":
        raise RuntimeError("Classic orchestration is retired; use native.hosted or native.durable.")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")