"""Formatting helpers for surfacing indexed ``custom_metadata`` into context.

The ``custom_metadata`` field is populated during ingestion (see
Azure/agent-landing-zone#487) and already lives in the AI Search index. These helpers turn
that raw field into a compact, deterministic text block that retrieval paths can
prepend to each document's content before it reaches the LLM (Azure/agent-landing-zone#506).

This module is intentionally dependency-free: it imports only the standard
library so it can be reused by connectors and strategies without creating import
cycles.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, List, Optional, Tuple

# Header used to delimit the metadata block inside a document's context text.
METADATA_HEADER = "[Document metadata]"


def parse_allowed_keys(raw: Optional[str]) -> List[str]:
    """Parse a comma-separated allow-list of metadata keys.

    An empty or missing value yields an empty list, which callers treat as
    "allow all keys".
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _coerce_to_pairs(raw: Any) -> List[Tuple[str, str]]:
    """Normalize a raw ``custom_metadata`` value into (key, value) string pairs.

    Accepts the shapes AI Search may return the field as:
    - a JSON string (parsed defensively; unparseable strings yield nothing),
    - a list of ``{"key": ..., "value": ...}`` dicts (the indexed shape),
    - a list of plain ``{key: value}`` dicts,
    - a plain ``{key: value}`` dict.

    Empty keys and empty/None values are skipped. Internal whitespace and
    newlines in values are collapsed to single spaces so the block stays compact
    and single-line per entry.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return []

    raw_items: Iterable[Tuple[Any, Any]]
    if isinstance(raw, dict):
        raw_items = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        collected: List[Tuple[Any, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if "key" in entry:
                collected.append((entry.get("key"), entry.get("value")))
            else:
                collected.extend(entry.items())
        raw_items = collected
    else:
        return []

    pairs: List[Tuple[str, str]] = []
    for key, value in raw_items:
        if key is None:
            continue
        key_str = str(key).strip()
        if not key_str:
            continue
        if value is None:
            continue
        value_str = " ".join(str(value).split())
        if not value_str:
            continue
        pairs.append((key_str, value_str))
    return pairs


def format_custom_metadata(
    raw: Any,
    max_chars: int = 500,
    allowed_keys: Optional[Iterable[str]] = None,
) -> str:
    """Render ``custom_metadata`` as a compact text block for LLM context.

    :param raw: The raw ``custom_metadata`` field from a search result.
    :param max_chars: Hard cap on the rendered block length; non-positive
        disables truncation.
    :param allowed_keys: Optional iterable of keys to keep; when empty/None all
        keys are kept.
    :return: A ``[Document metadata]`` block with sorted ``key: value`` lines, or
        ``""`` when there is nothing usable so callers can no-op.
    """
    pairs = _coerce_to_pairs(raw)
    if not pairs:
        return ""

    if allowed_keys:
        allow = {k.strip() for k in allowed_keys if k and str(k).strip()}
        if allow:
            pairs = [(k, v) for k, v in pairs if k in allow]
            if not pairs:
                return ""

    pairs.sort(key=lambda kv: kv[0])

    lines = [METADATA_HEADER]
    lines.extend(f"{k}: {v}" for k, v in pairs)
    block = "\n".join(lines)

    if max_chars and max_chars > 0 and len(block) > max_chars:
        block = block[:max_chars].rstrip()

    return block
