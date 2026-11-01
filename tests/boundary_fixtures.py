"""Shared retained connector fixtures, independent of the retired HTTP runtime."""

from concurrent.futures import Future
from unittest.mock import AsyncMock, patch

import pytest
from tenacity import RetryError

from connectors.appconfig import AppConfigClient
from connectors.search import SearchClient


@pytest.fixture
def actual_config(monkeypatch):
    monkeypatch.setenv("allow_environment_variables", "false")
    config = AppConfigClient.__new__(AppConfigClient)
    config.disabled = False
    config.client = {}
    return config


def _retry_failure():
    future = Future()
    future.set_exception(RuntimeError("synthetic-private-http-provider-detail"))
    return RetryError(future)


@pytest.fixture
def search_client(patch_dependencies, mock_config):
    """Retain the actual Search connector fixture without strategy imports."""
    mock_config.get.side_effect = lambda key, default=None, type=str: {
        "SEARCH_SERVICE_QUERY_ENDPOINT": "https://fake-search.search.windows.net",
        "SEARCH_RAG_INDEX_NAME": "ragindex",
        "SEARCH_APPROACH": "term",
        "ALLOW_ANONYMOUS": "true",
    }.get(key, default)
    with patch("connectors.search.get_config", return_value=mock_config):
        client = SearchClient()
    client._get_search_user_token_for_trimming = AsyncMock(return_value=None)
    return client
