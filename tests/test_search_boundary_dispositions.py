"""Search's optional probes and strict/anonymous retrieval are separate contracts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from connectors import search
from connectors.foundry_iq import McpSourceError
from connectors.foundry_iq_mcp import McpConfigurationError, McpCredentialError
from boundary_fixtures import actual_config, _retry_failure, search_client


@pytest.mark.parametrize("name", [
    "OAUTH_AZURE_AD_TENANT_ID", "OAUTH_AZURE_AD_CLIENT_ID", "OAUTH_AZURE_AD_CLIENT_SECRET",
])
@pytest.mark.parametrize("mode", ["missing", "retry", "unexpected", "conversion", "cancelled", "configured"])
async def test_obo_optional_reads_do_not_hide_provider_bugs(search_client, actual_config, monkeypatch, name, mode):
    selected_name = name
    failure = asyncio.CancelledError("synthetic cancellation") if mode == "cancelled" else RuntimeError("synthetic provider bug")

    class InvalidValue:
        def __str__(self):
            raise failure

    values = {
        "OAUTH_AZURE_AD_TENANT_ID": " tenant ",
        "OAUTH_AZURE_AD_CLIENT_ID": " client ",
        "OAUTH_AZURE_AD_CLIENT_SECRET": " synthetic-client-value ",
    }
    actual_config.client.update(values)
    if mode == "missing":
        actual_config.client.pop(name)
    elif mode == "conversion":
        actual_config.client[name] = InvalidValue()
    read = actual_config.get_config_with_retry

    def configured_read(name):
        if name == selected_name and mode in {"retry", "unexpected", "cancelled"}:
            raise _retry_failure() if mode == "retry" else failure
        return read(name)

    monkeypatch.setattr(actual_config, "get_config_with_retry", configured_read)
    search_client.cfg = actual_config
    response = SimpleNamespace(status=200, text=AsyncMock(return_value=json.dumps({
        "access_token": "synthetic-delegated-token", "expires_in": 60,
    })))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    session = SimpleNamespace(post=MagicMock(return_value=request))
    search_client._get_session = AsyncMock(return_value=session)
    if mode in {"unexpected", "conversion", "cancelled"}:
        with pytest.raises(type(failure)) as raised:
            await search_client._acquire_search_user_token_via_obo("synthetic-api-token")
        assert raised.value is failure
    else:
        token = await search_client._acquire_search_user_token_via_obo("synthetic-api-token")
        assert token == ("synthetic-delegated-token" if mode == "configured" else None)
    if mode == "configured":
        session.post.assert_called_once_with(
            "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            data={
                "client_id": "client", "client_secret": "synthetic-client-value",
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "requested_token_use": "on_behalf_of",
                "scope": "https://search.azure.com/user_impersonation",
                "assertion": "synthetic-api-token",
            },
        )
        assert search_client._cached_search_user_token == token
        request.__aexit__.assert_awaited_once()
    else:
        search_client._get_session.assert_not_awaited()


@pytest.mark.parametrize("mode", ["failure", "cancelled", "success"])
async def test_model_initialization_fallback_keeps_real_term_retrieval(mock_config, patch_dependencies, mode, caplog):
    marker = "synthetic-private-model-initialization"
    failure = asyncio.CancelledError(marker) if mode == "cancelled" else RuntimeError(marker)
    mock_config.get.side_effect = lambda key, default=None, type=str: {
        "SEARCH_SERVICE_QUERY_ENDPOINT": "https://search.invalid",
        "SEARCH_RAG_INDEX_NAME": "documents",
        "SEARCH_APPROACH": "hybrid",
    }.get(key, default)
    model = SimpleNamespace(get_embeddings=AsyncMock(return_value=[0.2]))
    with (
        patch("connectors.search.get_config", return_value=mock_config),
        patch("connectors.aifoundry.get_genai_client", side_effect=failure if mode != "success" else None, return_value=model),
    ):
        if mode == "cancelled":
            with pytest.raises(asyncio.CancelledError) as raised:
                search.SearchClient()
            assert raised.value is failure
            return
        client = search.SearchClient()
    client.set_request_context(api_access_token="synthetic-api", allow_anonymous=False, conversation_id="chat'quoted")
    client._get_search_user_token_for_trimming = AsyncMock(return_value="synthetic-delegated")
    client.search = AsyncMock(return_value={"value": [{"title": "Document", "content": "grounding", "filepath": "file.pdf"}]})
    with patch("connectors.search.get_retrieval_backend", return_value="ai_search"):
        result = json.loads(await client.search_knowledge_base("question"))
    assert result == {"results": [{"title": "Document", "link": "file.pdf", "content": "grounding"}], "query": "question"}
    sent = client.search.await_args.kwargs
    assert sent["search_user_token"] == "synthetic-delegated"
    assert sent["body"]["search"] == "question"
    assert sent["body"]["filter"] == search.build_conversation_filter("chat'quoted")
    assert ("vectorQueries" in sent["body"]) == (mode == "success")
    assert marker not in caplog.text


@pytest.mark.parametrize("anonymous", [False, True], ids=["strict", "anonymous"])
@pytest.mark.parametrize("stage", ["embedding", "obo"])
async def test_failure_before_token_assignment_preserves_original_retrieval_contract(
    search_client, anonymous, stage, caplog,
):
    marker = "synthetic-private-early-retrieval-error"
    failure = RuntimeError(marker)
    search_client._allow_anonymous = anonymous
    search_client.search_approach = "hybrid"
    search_client.aoai_client = SimpleNamespace(get_embeddings=AsyncMock(
        return_value=[0.1], side_effect=failure if stage == "embedding" else None,
    ))
    search_client._get_search_user_token_for_trimming = AsyncMock(
        side_effect=failure if stage == "obo" else None,
    )
    search_client.search = AsyncMock()
    with patch("connectors.search.get_retrieval_backend", return_value="ai_search"):
        if anonymous:
            result = json.loads(await search_client.search_knowledge_base("question"))
            assert result == {"results": [], "query": "question", "error": "search_failed"}
        else:
            with pytest.raises(RuntimeError) as raised:
                await search_client.search_knowledge_base("question")
            assert raised.value is failure
    search_client.search.assert_not_awaited()
    assert marker not in caplog.text


@pytest.mark.parametrize("mode", ["failure", "malformed", "cancelled", "empty", "populated"])
async def test_index_probe_failure_never_caches_a_false_empty_bypass(search_client, monkeypatch, mode, caplog):
    marker = "synthetic-private-probe-error"
    cache = {}
    monkeypatch.setattr(search, "_global_index_empty_cache", cache)
    failure = asyncio.CancelledError(marker) if mode == "cancelled" else RuntimeError(marker)
    result = {"value": [{}] if mode == "populated" else []}
    if mode == "malformed":
        result = {"value": None}
    search_client.search = AsyncMock(return_value=result, side_effect=failure if mode in {"failure", "cancelled"} else None)
    if mode == "cancelled":
        with pytest.raises(asyncio.CancelledError) as raised:
            await search_client.is_index_empty()
        assert raised.value is failure
    else:
        assert await search_client.is_index_empty() is (mode == "empty")
    if mode in {"empty", "populated"}:
        assert cache[search_client.index_name]["is_empty"] is (mode == "empty")
        await search_client.is_index_empty()
        search_client.search.assert_awaited_once()
    else:
        assert cache == {}
    assert marker not in caplog.text


@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize("anonymous", [False, True], ids=["strict", "anonymous"])
@pytest.mark.parametrize("mode", ["runtime", "auth", "configuration", "credential", "source", "malformed", "cancelled", "success"])
async def test_foundry_wrapper_keeps_exact_failure_and_credential_contract(search_client, mcp, anonymous, mode, caplog):
    marker = "synthetic-private-foundry-detail"
    failure = {
        "runtime": RuntimeError(marker),
        "auth": RuntimeError(f"HTTP 403 {marker}"),
        "configuration": McpConfigurationError(marker),
        "credential": McpCredentialError(marker),
        "source": McpSourceError(marker),
        "cancelled": asyncio.CancelledError(marker),
    }.get(mode)
    records = [{"title": "Document", "content": "grounding", "link": "file.pdf"}]
    if mode == "malformed":
        records.append({"title": "Invalid", "content": {"detail": marker}})
    provider = SimpleNamespace(mcp_config=SimpleNamespace(enabled=mcp),
                               retrieve=AsyncMock(return_value=records, side_effect=failure))
    search_client.set_request_context(
        api_access_token="synthetic-incoming", allow_anonymous=anonymous,
        conversation_id="chat", user_context={"principal_id": "principal"},
    )
    search_client._get_search_user_token_for_trimming = AsyncMock(return_value="synthetic-delegated")
    obo = AsyncMock(return_value="synthetic-delegated")
    should_raise = (
        mode != "success" and (not anonymous or mode in {"configuration", "cancelled"}
                              or (mcp and mode in {"credential", "source"}))
    )
    with (
        patch("connectors.search.get_retrieval_backend", return_value="foundry_iq"),
        patch("connectors.search.get_foundry_iq_client", return_value=provider),
        patch("connectors.search.acquire_obo_search_token", obo),
    ):
        if should_raise:
            with pytest.raises(ValidationError if mode == "malformed" else type(failure)) as raised:
                await search_client.search_knowledge_base("question")
            if mode != "malformed":
                assert raised.value is failure
        else:
            result = json.loads(await search_client.search_knowledge_base("question"))
            assert result == (
                {"results": records, "query": "question"} if mode == "success"
                else {"results": [], "query": "question", "error": "search_failed"}
            )
    expected = {
        "obo_token": "synthetic-delegated", "conversation_id": "chat",
        "user_context": {"principal_id": "principal"},
    }
    if mcp:
        expected["incoming_token"] = "synthetic-incoming"
        obo.assert_awaited_once_with("synthetic-incoming", allow_anonymous=anonymous)
    else:
        obo.assert_not_awaited()
        search_client._get_search_user_token_for_trimming.assert_awaited_once()
    provider.retrieve.assert_awaited_once_with("question", **expected)
    assert marker not in caplog.text


@pytest.mark.parametrize("mode", ["failure", "cancelled", "missing", "empty-field", "success"])
async def test_optional_filepath_helper_retains_nullable_result(search_client, mode, caplog):
    marker = "synthetic-private-filepath-error"
    failure = asyncio.CancelledError(marker) if mode == "cancelled" else RuntimeError(marker)
    document = None if mode == "missing" else {"filepath": "" if mode == "empty-field" else "document.pdf"}
    search_client.get_document = AsyncMock(return_value=document, side_effect=failure if mode in {"failure", "cancelled"} else None)
    if mode == "cancelled":
        with pytest.raises(asyncio.CancelledError) as raised:
            await search_client.fetch_filepath_from_index("document")
        assert raised.value is failure
    else:
        assert await search_client.fetch_filepath_from_index("document") == ("document.pdf" if mode == "success" else None)
    search_client.get_document.assert_awaited_once_with(
        index_name=search_client.index_name, document_id="document", select_fields=["filepath", "title"],
    )
    assert marker not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_real_filepath_http_failure_never_logs_provider_body(search_client, status, caplog):
    marker = "synthetic-private-http-response"
    response = SimpleNamespace(status=status, text=AsyncMock(return_value=marker))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    session = SimpleNamespace(get=MagicMock(return_value=request))
    search_client._get_session = AsyncMock(return_value=session)
    search_client.credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="synthetic-token")))
    assert await search_client.fetch_filepath_from_index("document") is None
    request.__aexit__.assert_awaited_once()
    assert marker not in caplog.text
