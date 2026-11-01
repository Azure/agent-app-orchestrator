"""Isolated real Foundry connector fixtures; no retired runtime imports."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from connectors.foundry_iq import FoundryIQClient


class _FakeResponse:
    def __init__(self, payload, status):
        self.payload = payload
        self.status = status

    async def text(self):
        return json.dumps(self.payload)

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload, status):
        self.payload = payload
        self.status = status
        self.captured = {}

    def post(self, url, headers=None, json=None, data=None):
        self.captured = {"url": url, "headers": headers, "json": json, "data": data}
        return _FakeResponse(self.payload, self.status)


def _source(*, query_headers=None):
    return {
        "name": "monitor-mcp", "serverURL": "https://mcp.contoso.com/mcp",
        "failOnError": True, "maxOutputDocuments": 4,
        "tools": [{
            "name": "query_logs", "maxOutputTokens": 2048,
            "inclusionMode": "reranked",
            "outputParsing": {
                "kind": "json", "jsonParameters": {"documentsPath": "$.results[*]"},
            },
        }],
        "queryHeaders": query_headers or [],
    }


def _build_client(payload=None, *, status=200, enabled=True, sources=None):
    values = {
        "KNOWLEDGE_BASE_ENDPOINT": "https://fake-search.search.windows.net",
        "SEARCH_SERVICE_QUERY_ENDPOINT": "https://fake-search.search.windows.net",
        "KNOWLEDGE_BASE_NAME": "kb-test",
        "FOUNDRY_IQ_API_VERSION": "2026-05-01-preview",
        "FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "",
        "FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": "", "FOUNDRY_IQ_PATTERN": "",
        "FOUNDRY_IQ_FILTER_ADD_ON_ENABLED": False,
        "FOUNDRY_IQ_SECURITY_FIELD_NAME": "metadata_security_id",
        "FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS": 5,
        "FOUNDRY_IQ_FORWARD_SOURCE_AUTH": True,
        "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED": False,
        "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME": "",
        "FOUNDRY_IQ_MAX_RUNTIME_SECONDS": 120, "FOUNDRY_IQ_MCP_ENABLED": enabled,
        "FOUNDRY_IQ_MCP_SOURCES_JSON": json.dumps(sources or [_source()]),
        "FOUNDRY_IQ_MCP_REASONING_EFFORT": "low",
        "FOUNDRY_IQ_MCP_TRUSTED_HOSTS": "mcp.contoso.com",
        "FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS": False,
    }
    config = MagicMock()
    config.get.side_effect = lambda key, default=None, type=str: values.get(key, default)
    config.aiocredential = MagicMock()
    config.aiocredential.get_token = AsyncMock(
        return_value=SimpleNamespace(token="synthetic-service-token"),
    )
    with patch("connectors.foundry_iq.get_config", return_value=config):
        client = FoundryIQClient()
    session = _FakeSession(payload or {"references": [], "activity": []}, status)
    client._get_session = AsyncMock(return_value=session)
    return client, session
