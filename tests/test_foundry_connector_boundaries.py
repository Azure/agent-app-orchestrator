"""Retained credential/request boundaries, independent of classic execution."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from azure.core.exceptions import ClientAuthenticationError

from connectors.foundry_iq import McpSourceError
from connectors.foundry_iq_mcp import McpCredentialError
from foundry_boundary_fixtures import _build_client, _source


@pytest.mark.parametrize("mcp", [False, True], ids=["legacy", "mcp"])
@pytest.mark.parametrize("mode", ["sdk", "unexpected", "cancelled"])
async def test_service_token_failure_never_reaches_http(mcp, mode, caplog):
    marker = "synthetic-private-service-token-detail"
    cls = {
        "sdk": ClientAuthenticationError, "unexpected": RuntimeError,
        "cancelled": asyncio.CancelledError,
    }[mode]
    failure = cls(marker)
    client, session = _build_client(enabled=mcp)
    client.credential.get_token.side_effect = failure
    expected = McpCredentialError if mcp and mode != "cancelled" else cls
    with pytest.raises(expected) as raised:
        await client.retrieve("question")
    if expected is McpCredentialError:
        assert str(raised.value) == "Failed to acquire the Foundry IQ service token"
        assert raised.value.__cause__ is None
    else:
        assert raised.value is failure
    client._get_session.assert_not_awaited()
    assert not session.captured
    assert marker not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("mode", ["unexpected", "known", "cancelled", "success"])
async def test_actual_mcp_header_resolution_keeps_credential_failure_boundary(mode, caplog):
    marker = "synthetic-private-query-credential-detail"
    failure = {
        "unexpected": RuntimeError, "known": McpCredentialError,
        "cancelled": asyncio.CancelledError, "success": RuntimeError,
    }[mode](marker)
    client, session = _build_client(sources=[_source(query_headers=[{
        "name": "Authorization",
        "valueFrom": {"kind": "keyVaultSecret", "secretName": "test-reference"},
    }])])
    with patch("connectors.keyvault.get_secret", AsyncMock(
        return_value="synthetic-query-key", side_effect=None if mode == "success" else failure,
    )) as secret:
        if mode == "success":
            assert await client.retrieve("question", obo_token="synthetic-delegated") == []
            assert session.captured["headers"]["x-ms-query-source-authorization"] == "synthetic-delegated"
        else:
            expected = asyncio.CancelledError if mode == "cancelled" else McpCredentialError
            with pytest.raises(expected) as raised:
                await client.retrieve("question")
            if mode in {"known", "cancelled"}:
                assert raised.value is failure
            else:
                assert marker not in str(raised.value)
                assert raised.value.__cause__ is failure
            client._get_session.assert_not_awaited()
    secret.assert_awaited_once()
    assert marker not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("mcp", [False, True], ids=["legacy", "mcp"])
@pytest.mark.parametrize("mode", ["transport", "read", "json", "cleanup", "http", "cancelled", "success"])
async def test_foundry_request_translation_keeps_cleanup_and_error_identity(mcp, mode, caplog):
    marker = "synthetic-private-foundry-response-detail"
    failure = asyncio.CancelledError(marker) if mode == "cancelled" else (
        ValueError(marker) if mode == "json" else aiohttp.ClientConnectionError(marker)
    )
    client, _ = _build_client(enabled=mcp)
    response = SimpleNamespace(
        status=503 if mode == "http" else 200,
        text=AsyncMock(return_value=marker, side_effect=failure if mode in {"read", "cancelled"} else None),
        json=AsyncMock(return_value={"references": [], "activity": []}, side_effect=failure if mode == "json" else None),
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response, side_effect=failure if mode == "transport" else None)
    context.__aexit__ = AsyncMock(return_value=False, side_effect=failure if mode == "cleanup" else None)
    session = SimpleNamespace(post=MagicMock(return_value=context))
    client._get_session = AsyncMock(return_value=session)
    if mode == "success":
        assert await client.retrieve("question", obo_token="synthetic-delegated", conversation_id="chat") == []
    else:
        expected = asyncio.CancelledError if mode == "cancelled" else (
            McpSourceError if mcp else RuntimeError if mode == "http" else type(failure)
        )
        with pytest.raises(expected) as raised:
            await client.retrieve("question", obo_token="synthetic-delegated", conversation_id="chat")
        if mode == "http" or (mcp and mode != "cancelled"):
            assert marker not in str(raised.value)
        else:
            assert raised.value is failure
    headers = session.post.call_args.kwargs["headers"]
    assert headers["x-ms-query-source-authorization"] == "synthetic-delegated"
    assert context.__aexit__.await_count == (0 if mode == "transport" else 1)
    if mode == "http":
        response.json.assert_not_awaited()
    assert marker not in caplog.text
    assert not any(record.exc_info for record in caplog.records)
