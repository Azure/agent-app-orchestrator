"""Retained Search authorization, cancellation and sensitive logging contracts.

The classic strategy/context-provider tests have been retired under ADR-0034.
These connector contracts still apply to the optional native Search tool.
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from azure.core.exceptions import ClientAuthenticationError

from boundary_fixtures import search_client
from connectors.search import (
    _RETRIEVAL_AUTH_FAILURE_MARKER,
    _RETRIEVAL_ERROR_MARKER,
    build_conversation_filter,
)


@pytest.mark.parametrize("operation", ["search", "get_document"])
@pytest.mark.parametrize(
    "failure_type", [ClientAuthenticationError, RuntimeError, asyncio.CancelledError]
)
async def test_low_level_search_token_failure_propagates_without_request_or_raw_log(
    search_client, operation, failure_type, caplog
):
    marker = "synthetic-private-token-provider-detail"
    failure = failure_type(marker)
    search_client.credential = SimpleNamespace(get_token=AsyncMock(side_effect=failure))
    search_client._get_session = AsyncMock()
    call = (
        search_client.search("documents", {"search": "question"})
        if operation == "search"
        else search_client.get_document("documents", "document-1")
    )
    with pytest.raises(failure_type) as raised:
        await call
    assert raised.value is failure
    search_client.credential.get_token.assert_awaited_once_with(
        "https://search.azure.com/.default"
    )
    search_client._get_session.assert_not_awaited()
    assert marker not in caplog.text
    if failure_type is ClientAuthenticationError:
        assert "failed to acquire token" in caplog.text


async def test_403_emits_auth_failure_marker_at_error(search_client, caplog):
    caplog.set_level(logging.WARNING)
    with patch.object(
        search_client, "search", AsyncMock(side_effect=RuntimeError("HTTP 403 Forbidden"))
    ):
        await search_client.search_knowledge_base("hello")
    matches = [
        record for record in caplog.records
        if _RETRIEVAL_AUTH_FAILURE_MARKER in record.getMessage()
    ]
    assert matches
    assert all(record.levelno == logging.ERROR for record in matches)


async def test_generic_error_emits_error_marker_at_warning(search_client, caplog):
    caplog.set_level(logging.WARNING)
    with patch.object(
        search_client, "search", AsyncMock(side_effect=RuntimeError("connection reset"))
    ):
        await search_client.search_knowledge_base("hello")
    matches = [
        record for record in caplog.records
        if _RETRIEVAL_ERROR_MARKER in record.getMessage()
    ]
    assert matches
    assert all(record.levelno == logging.WARNING for record in matches)
    assert not any(
        _RETRIEVAL_AUTH_FAILURE_MARKER in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("anonymous", [False, True], ids=["strict", "anonymous"])
async def test_connector_failure_preserves_full_error_contract_and_trimming_inputs(
    anonymous, search_client, caplog
):
    search_client._allow_anonymous = anonymous
    search_client._conversation_id = "conversation'quoted"
    token = "synthetic-delegated-token"
    search_client._get_search_user_token_for_trimming = AsyncMock(return_value=token)
    marker = "synthetic-connector-error-detail"
    failure = RuntimeError(marker)
    search_client.search = AsyncMock(side_effect=failure)
    if anonymous:
        response = json.loads(await search_client.search_knowledge_base("policy"))
        assert response == {"results": [], "query": "policy", "error": "search_failed"}
        assert marker not in json.dumps(response)
        assert token not in json.dumps(response)
    else:
        with pytest.raises(RuntimeError) as caught:
            await search_client.search_knowledge_base("policy")
        assert caught.value is failure
    search_client._get_search_user_token_for_trimming.assert_awaited_once()
    sent = search_client.search.await_args.kwargs
    assert sent["index_name"] == search_client.index_name
    assert sent["body"]["search"] == "policy"
    assert sent["body"]["filter"] == build_conversation_filter("conversation'quoted")
    assert sent["search_user_token"] == token
    assert marker not in caplog.text
    assert token not in caplog.text
