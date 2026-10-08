"""Retrieval backend selector.

Resolves which retrieval backend the orchestrator uses — classic Azure AI
Search (``ai_search``) or Foundry IQ knowledge bases (``foundry_iq``) — from the
``RETRIEVAL_BACKEND`` App Configuration key.

The value is resolved once and cached so the four strategy seams and the
``search_knowledge_base`` connector path read a single startup-resolved accessor
instead of calling ``cfg.get`` per request. ``reset_retrieval_backend_cache`` is
provided for tests that need to flip the value.

Introduced for Azure/agent-landing-zone#526 (Foundry IQ as a first-class retrieval
backend). The default stays ``ai_search`` so enabling the seam changes no
runtime behavior until an operator opts in.
"""

import logging
from typing import Optional

from dependencies import get_config

# Backend identifiers. These are the accepted values of RETRIEVAL_BACKEND.
RETRIEVAL_BACKEND_AI_SEARCH = "ai_search"
RETRIEVAL_BACKEND_FOUNDRY_IQ = "foundry_iq"

_VALID_BACKENDS = frozenset({RETRIEVAL_BACKEND_AI_SEARCH, RETRIEVAL_BACKEND_FOUNDRY_IQ})
_DEFAULT_BACKEND = RETRIEVAL_BACKEND_AI_SEARCH

# Config key name (kept here so connectors don't import from api/).
RETRIEVAL_BACKEND_KEY = "RETRIEVAL_BACKEND"

_cached_backend: Optional[str] = None


def resolve_retrieval_backend(cfg=None) -> str:
    """Resolve the configured retrieval backend, validating the raw value.

    Unknown or empty values fall back to ``ai_search`` with a warning so a
    typo never silently routes traffic to an unconfigured backend.
    """
    cfg = cfg or get_config()
    raw = (cfg.get(RETRIEVAL_BACKEND_KEY, _DEFAULT_BACKEND, type=str) or "").strip().lower()
    if raw not in _VALID_BACKENDS:
        if raw:
            logging.warning(
                "[RetrievalBackend] Unknown RETRIEVAL_BACKEND=%r; falling back to %s. "
                "Valid values: %s",
                raw,
                _DEFAULT_BACKEND,
                ", ".join(sorted(_VALID_BACKENDS)),
            )
        return _DEFAULT_BACKEND
    return raw


def get_retrieval_backend() -> str:
    """Return the startup-resolved retrieval backend, resolving once and caching."""
    global _cached_backend
    if _cached_backend is None:
        _cached_backend = resolve_retrieval_backend()
        logging.info("[RetrievalBackend] Resolved retrieval backend: %s", _cached_backend)
    return _cached_backend


def is_foundry_iq() -> bool:
    """Convenience predicate: ``True`` when the resolved backend is Foundry IQ."""
    return get_retrieval_backend() == RETRIEVAL_BACKEND_FOUNDRY_IQ


def reset_retrieval_backend_cache() -> None:
    """Clear the cached backend value. Intended for tests."""
    global _cached_backend
    _cached_backend = None
