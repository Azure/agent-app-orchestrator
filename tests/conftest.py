"""Shared test fixtures for the GPT-RAG Orchestrator test suite."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Make sure ``src/`` is on sys.path (pytest.ini sets pythonpath=["src"] but
# conftest loads before that in some setups).
# ---------------------------------------------------------------------------
_src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
if os.path.isdir(_src) and _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_config():
    """Return a fake AppConfigClient-like object pre-loaded with safe defaults."""
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None, type=str: {  # noqa: A002
        "AI_FOUNDRY_PROJECT_ENDPOINT": "https://fake-project.openai.azure.com",
        "AI_FOUNDRY_ACCOUNT_ENDPOINT": "https://fake-account.openai.azure.com",
        "CHAT_DEPLOYMENT_NAME": "gpt-4o",
        "EMBEDDING_DEPLOYMENT_NAME": "text-embedding-3-large",
        "OPENAI_API_VERSION": "2025-04-01-preview",
        "PROMPT_SOURCE": "file",
        "SEARCH_SERVICE_QUERY_ENDPOINT": "",
        "SEARCH_RAG_INDEX_NAME": "",
        "RETRIEVAL_BACKEND": "ai_search",
        "KNOWLEDGE_BASE_NAME": "",
        "KNOWLEDGE_BASE_ENDPOINT": "",
        "KNOWLEDGE_BASE_CONNECTION_ID": "",
        "FOUNDRY_IQ_API_VERSION": "2026-05-01-preview",
        "FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "",
        "FOUNDRY_IQ_FILTER_ADD_ON_ENABLED": "false",
        "FOUNDRY_IQ_SECURITY_FIELD_NAME": "metadata_security_id",
        "FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS": "5",
        "FOUNDRY_IQ_MAX_RUNTIME_SECONDS": "120",
        "FOUNDRY_IQ_MCP_ENABLED": "false",
        "FOUNDRY_IQ_MCP_SOURCES_JSON": "[]",
        "FOUNDRY_IQ_MCP_REASONING_EFFORT": "low",
        "FOUNDRY_IQ_MCP_TRUSTED_HOSTS": "",
        "FOUNDRY_IQ_MCP_LOG_TOOL_ARGUMENTS": "false",
        "AGENT_STRATEGY": "single_agent_rag",
        "AGENT_ID": "",
        "SEARCH_RETRIEVAL_ENABLED": "true",
        "BING_RETRIEVAL_ENABLED": "false",
        "BING_CONNECTION_ID": "",
        "LOG_LEVEL": "INFO",
        "ALLOW_ANONYMOUS": True if type is bool else "true",
    }.get(key, default)
    cfg.get_value = lambda key, default=None, allow_none=False, type=str: cfg.get(
        key, default, type=type
    )
    cfg.credential = MagicMock()
    cfg.aiocredential = MagicMock()
    cfg.auth_failed = False
    return cfg


@pytest.fixture()
def mock_cosmos():
    """Return a fake CosmosDBClient with async helpers."""
    cosmos = MagicMock()
    cosmos.get_document = AsyncMock(return_value=None)
    cosmos.document_id_exists = AsyncMock(return_value=False)
    cosmos.upsert_document = AsyncMock()
    cosmos.create_document = AsyncMock()
    return cosmos


@pytest.fixture()
def mock_identity_manager():
    """Return a fake identity manager that provides mock credentials."""
    im = MagicMock()
    im.get_credential.return_value = MagicMock()
    im.get_aio_credential.return_value = MagicMock()
    return im


@pytest.fixture()
def patch_dependencies(mock_config, mock_cosmos, mock_identity_manager):
    """Patch retained connector boundaries without importing retired strategies."""
    with (
        patch("dependencies.get_config", return_value=mock_config),
        patch("connectors.cosmosdb.get_cosmosdb_client", return_value=mock_cosmos),
        patch("connectors.identity_manager.get_identity_manager", return_value=mock_identity_manager),
    ):
        yield
