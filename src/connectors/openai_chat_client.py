"""
OpenAI Chat Client Adapter for Microsoft Agent Framework.

Implements ChatClientProtocol using AsyncAzureOpenAI, enabling ChatAgent
to call Azure OpenAI / Foundry model deployments directly — without going
through Azure AI Foundry Agent Service V2.
"""

import logging
import time
from collections.abc import AsyncIterable, Sequence
from typing import Any

from azure.identity import get_bearer_token_provider
from openai import AsyncAzureOpenAI, BadRequestError
from pydantic.errors import PydanticInvalidForJsonSchema

from agent_framework import (
    ChatMessage,
    ChatResponse,
    ChatResponseUpdate,
)

logger = logging.getLogger(__name__)

# Options keys that map directly to OpenAI API parameters
_PASSTHROUGH_OPTIONS = {
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "seed", "stop", "user",
    "logit_bias", "reasoning_effort",
}

# Parameters that can be safely stripped on BadRequestError (model may not support them)
_OPTIONAL_PARAMS = {"reasoning_effort"}


class OpenAIChatClient:
    """Chat client that wraps AsyncAzureOpenAI and satisfies ChatClientProtocol.

    This allows ``ChatAgent`` to use an Azure OpenAI model deployment directly,
    with no dependency on Azure AI Foundry Agent Service V2.
    """

    additional_properties: dict[str, Any]

    def __init__(
        self,
        *,
        azure_endpoint: str,
        model_deployment_name: str,
        credential: Any,
        api_version: str = "2025-04-01-preview",
        max_retries: int = 10,
    ) -> None:
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default",
        )
        self._client = AsyncAzureOpenAI(
            api_version=api_version,
            azure_endpoint=azure_endpoint,
            azure_ad_token_provider=token_provider,
            max_retries=max_retries,
        )
        self._model = model_deployment_name
        self.additional_properties = {}

    # ------------------------------------------------------------------
    # Options handling
    # ------------------------------------------------------------------
    def _build_api_params(
        self,
        oai_messages: list[dict],
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the kwargs dict for ``completions.create`` from messages + options."""
        opts = dict(options) if options else {}

        # Prepend instructions as a system message
        instructions = opts.pop("instructions", None)
        if instructions:
            oai_messages = [{"role": "system", "content": instructions}] + oai_messages

        # Model — allow per-request override via model_id
        model = opts.pop("model_id", None) or self._model

        params: dict[str, Any] = {"model": model, "messages": oai_messages}

        # Pass through supported API parameters
        for key in _PASSTHROUGH_OPTIONS:
            val = opts.pop(key, None)
            if val is not None:
                params[key] = val

        # response_format (structured output)
        if response_format := opts.pop("response_format", None):
            if isinstance(response_format, dict):
                params["response_format"] = response_format
            else:
                # Pydantic model → OpenAI json_schema format
                try:
                    from openai.lib._pydantic import to_strict_json_schema
                    if not isinstance(response_format, type):
                        raise TypeError("response_format must be a model class or mapping")
                    schema = to_strict_json_schema(response_format)
                    params["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": response_format.__name__,
                            "strict": True,
                            "schema": schema,
                        },
                    }
                except (TypeError, ValueError, PydanticInvalidForJsonSchema) as exc:
                    logger.warning(
                        "[OpenAIChatClient] Unsupported response_format, ignoring (%s)",
                        type(exc).__name__,
                    )

        return params

    # ------------------------------------------------------------------
    # ChatClientProtocol.get_response
    # ------------------------------------------------------------------
    async def get_response(
        self,
        messages: str | ChatMessage | Sequence[str | ChatMessage],
        *,
        options: Any | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        oai_messages = self._to_openai_messages(messages)
        params = self._build_api_params(oai_messages, options)
        params["stream"] = False
        try:
            completion = await self._client.chat.completions.create(**params)
        except BadRequestError:
            stripped = {k for k in _OPTIONAL_PARAMS if params.pop(k, None) is not None}
            if not stripped:
                raise
            logger.info("[OpenAIChatClient] Retrying without %s", stripped)
            completion = await self._client.chat.completions.create(**params)
        choice = completion.choices[0]
        return ChatResponse(
            text=choice.message.content or "",
            model_id=completion.model,
            response_id=completion.id,
        )

    # ------------------------------------------------------------------
    # ChatClientProtocol.get_streaming_response
    # ------------------------------------------------------------------
    def get_streaming_response(
        self,
        messages: str | ChatMessage | Sequence[str | ChatMessage],
        *,
        options: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[ChatResponseUpdate]:
        oai_messages = self._to_openai_messages(messages)
        params = self._build_api_params(oai_messages, options)
        return self._stream(params)

    async def _stream(self, params: dict[str, Any]) -> AsyncIterable[ChatResponseUpdate]:
        t0 = time.time()
        params["stream"] = True
        params.setdefault("max_completion_tokens", 4096)
        try:
            response = await self._client.chat.completions.create(**params)
        except BadRequestError:
            stripped = {k for k in _OPTIONAL_PARAMS if params.pop(k, None) is not None}
            if not stripped:
                raise
            logger.info("[OpenAIChatClient] Retrying stream without %s", stripped)
            response = await self._client.chat.completions.create(**params)
        logger.info("[OpenAIChatClient] completions.create returned in %.2fs", time.time() - t0)
        first_token = True
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = delta.content if delta and delta.content else None
            if text:
                if first_token:
                    logger.info("[OpenAIChatClient] first_token: %.2fs", time.time() - t0)
                    first_token = False
                yield ChatResponseUpdate(
                    text=text,
                    role="assistant",
                    response_id=chunk.id,
                    model_id=chunk.model,
                )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _to_openai_messages(
        messages: str | ChatMessage | Sequence[str | ChatMessage],
    ) -> list[dict]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, ChatMessage):
            return [{"role": messages.role.value, "content": messages.text or ""}]
        result: list[dict] = []
        for m in messages:
            if isinstance(m, str):
                result.append({"role": "user", "content": m})
            else:
                result.append({"role": m.role.value, "content": m.text or ""})
        return result

    # ------------------------------------------------------------------
    # async context manager (optional, for parity with AzureAIAgentClient)
    # ------------------------------------------------------------------
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.close()
