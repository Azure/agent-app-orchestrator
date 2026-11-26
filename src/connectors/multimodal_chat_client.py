"""
Multimodal OpenAI Chat Client for Microsoft Agent Framework.

Extends OpenAIChatClient to support multimodal messages (text + images).
When a ChatMessage contains the MULTIMODAL_PREFIX marker, its text is
parsed as a JSON array of content parts and converted to OpenAI's
multimodal format: [{"type": "text", ...}, {"type": "image_url", ...}].
"""

import json
import logging
from collections.abc import Sequence

from agent_framework import ChatMessage

from connectors.openai_chat_client import OpenAIChatClient

logger = logging.getLogger(__name__)

# Magic prefix used by MultimodalSearchContextProvider to mark messages
# that contain structured multimodal content (text + base64 images).
MULTIMODAL_PREFIX = "\x00MULTIMODAL\x00"


class MultimodalChatClient(OpenAIChatClient):
    """Chat client that handles multimodal messages (text + images).

    Messages whose text starts with ``MULTIMODAL_PREFIX`` are treated as
    JSON-encoded content arrays and converted to the OpenAI vision format.
    All other messages pass through unchanged to the parent class.
    """

    @staticmethod
    def _to_openai_messages(
        messages: str | ChatMessage | Sequence[str | ChatMessage],
    ) -> list[dict]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, ChatMessage):
            return [MultimodalChatClient._convert_message(messages)]

        result: list[dict] = []
        for m in messages:
            if isinstance(m, str):
                result.append({"role": "user", "content": m})
            else:
                result.append(MultimodalChatClient._convert_message(m))
        return result

    @staticmethod
    def _convert_message(msg: ChatMessage) -> dict:
        """Convert a single ChatMessage, detecting multimodal content."""
        text = msg.text or ""
        role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)

        if text.startswith(MULTIMODAL_PREFIX):
            try:
                payload = json.loads(text[len(MULTIMODAL_PREFIX):])
                content_parts = []
                for part in payload:
                    if part.get("type") == "text":
                        content_parts.append({
                            "type": "text",
                            "text": part["text"],
                        })
                    elif part.get("type") == "image_url":
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": part["url"],
                                "detail": part.get("detail", "auto"),
                            },
                        })
                logger.info(
                    "[MultimodalChatClient] Converted multimodal message: "
                    "%d text parts, %d image parts",
                    sum(1 for p in content_parts if p["type"] == "text"),
                    sum(1 for p in content_parts if p["type"] == "image_url"),
                )
                return {"role": role, "content": content_parts}
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(
                    "[MultimodalChatClient] Failed to parse multimodal content, "
                    "falling back to plain text: %s", e,
                )
                # Strip the prefix and send as plain text
                return {"role": role, "content": text[len(MULTIMODAL_PREFIX):]}

        return {"role": role, "content": text}
