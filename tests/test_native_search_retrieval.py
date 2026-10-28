"""Native Search permission transport and required-source behavior; no live Azure."""

import asyncio
import pickle
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent_framework import ChatMessage, Role

from connectors.obo import DelegatedRetrievalCredentials, HostedRetrievalError
from strategies import foundry_iq_context_provider as provider_module
from test_foundry_iq import _build_client


_TOKEN = "synthetic-opaque-search-credential"
_ASSERTION = "synthetic-opaque-work-iq-app-assertion"
_PRIMARY = {"FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "corpus"}
_WORK_IQ_AUTH = {
    "WORK_IQ_APPLICATION_ID": "11111111-1111-4111-8111-111111111111",
    "WORK_IQ_FEDERATED_CREDENTIAL_ID": "22222222-2222-4222-8222-222222222222",
}
_INDEXED_SHAREPOINT = {
    "SHAREPOINT_INDEXED_ENABLED": True,
    "SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME": "sharepoint",
    "SHAREPOINT_INDEXED_APPLICATION_ID": "11111111-1111-4111-8111-111111111111",
    "SHAREPOINT_INDEXED_FEDERATED_CREDENTIAL_APPLICATION_ID": "22222222-2222-4222-8222-222222222222",
    "SHAREPOINT_INDEXED_TENANT_ID": "33333333-3333-4333-8333-333333333333",
    "SHAREPOINT_INDEXED_SITE_URL": "https://tenant.sharepoint.invalid/sites/knowledge",
    "FOUNDRY_IQ_API_VERSION": "2026-08-01-preview",
}


async def test_mixed_remote_wire_contract_and_official_response_shapes():
    payload = {
        "references": [
            {
                "type": "fabricDataAgent",
                "workspaceId": "workspace",
                "dataAgentId": "agent",
                "sourceData": {
                    "fabricAnswer": "Synthetic answer",
                    "fabricEmbeddedResources": [
                        {"mimeType": "text/csv", "content": "year,count\n2026,47"},
                        {"mimeType": "image/png", "content": "not-model-text"},
                    ],
                },
            },
            {
                "type": "workIQ",
                "sourceData": {
                    "parts": [
                        {"text": "Synthetic M365 evidence"},
                        {
                            "mediaType": "application/vnd.ms-workiq-reference",
                            "data": {"citation": {
                                "targetLink": "https://example.invalid/document",
                                "isSourceFiltered": False,
                            }},
                        },
                    ],
                },
            },
        ],
    }
    client, session = _build_client(payload, config_overrides={
        **_PRIMARY, **_WORK_IQ_AUTH,
        "FOUNDRY_IQ_API_VERSION": "2026-08-01-preview",
        "WORK_IQ_ENABLED": True,
        "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "m365",
        "FABRIC_IQ_ENABLED": True,
        "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME": "ontology",
        "FABRIC_DATA_AGENT_ENABLED": True,
        "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME": "analyst",
    })
    records = await client.retrieve(
        "question", obo_token=_TOKEN, native_permissions=True,
        work_iq_assertion=_ASSERTION,
    )
    headers = session.captured["headers"]
    assert headers["x-ms-query-source-authorization"] == _TOKEN
    assert headers["x-ms-query-work-iq-source-authorization"] == _ASSERTION
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["Authorization"] != f"Bearer {_TOKEN}"
    body = session.captured["json"]
    assert body["retrievalReasoningEffort"] == {"kind": "low"}
    assert "messages" in body and "intents" not in body
    assert {source["knowledgeSourceName"] for source in body["knowledgeSourceParams"]} == {
        "corpus", "m365", "ontology", "analyst",
    }
    assert all(
        source["failOnError"] and source["alwaysQuerySource"]
        for source in body["knowledgeSourceParams"]
    )
    assert records[0]["content"] == "Synthetic answer\n\nyear,count\n2026,47"
    assert records[0].source_type == "fabricDataAgent"
    assert "agent (workspace workspace)" in records[0]["title"]
    assert records[1]["link"] == "https://example.invalid/document"
    assert records[1]["content"] == "Synthetic M365 evidence"
    assert _TOKEN not in repr(body) and _ASSERTION not in repr(body)


@pytest.mark.parametrize("enabled,name", [
    ("WORK_IQ_ENABLED", "WORK_IQ_KNOWLEDGE_SOURCE_NAME"),
    ("FABRIC_IQ_ENABLED", "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME"),
    ("FABRIC_DATA_AGENT_ENABLED", "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME"),
    ("SHAREPOINT_REMOTE_ENABLED", "SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME"),
    ("SHAREPOINT_INDEXED_ENABLED", "SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME"),
    ("ONELAKE_KS_ENABLED", "ONELAKE_KNOWLEDGE_SOURCE_NAME"),
    ("WEB_GROUNDING_ENABLED", "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME"),
])
async def test_enabled_source_without_name_fails_before_transport(enabled, name):
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, enabled: True, name: "",
    })
    with pytest.raises(ValueError, match="provisioned name"):
        await client.retrieve("question", obo_token=_TOKEN, native_permissions=True)
    assert not session.captured
    client.credential.get_token.assert_not_awaited()


@pytest.mark.parametrize("kind", ["WORK_IQ", "FABRIC_IQ", "FABRIC_DATA_AGENT"])
async def test_remote_requires_august_without_changing_default(kind):
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, f"{kind}_ENABLED": True,
        f"{kind}_KNOWLEDGE_SOURCE_NAME": "remote",
    })
    with pytest.raises(ValueError, match="2026-08-01-preview"):
        await client.retrieve(
            "question", obo_token=_TOKEN, native_permissions=True,
            work_iq_assertion=_ASSERTION,
        )
    assert client.api_version == "2026-05-01-preview"
    assert not session.captured


async def test_work_iq_never_reuses_search_token_as_its_assertion():
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, "FOUNDRY_IQ_API_VERSION": "2026-08-01-preview",
        "WORK_IQ_ENABLED": True, "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "m365",
    })
    with pytest.raises(ValueError, match="app-audience"):
        await client.retrieve("question", obo_token=_TOKEN, native_permissions=True)
    assert not session.captured


@pytest.mark.parametrize("settings", [
    {},
    {**_WORK_IQ_AUTH, "WORK_IQ_APPLICATION_ID": "not-an-app-id"},
    {**_WORK_IQ_AUTH, "WORK_IQ_TENANT_ID": "not-a-tenant-id"},
    {**_WORK_IQ_AUTH, "WORK_IQ_FEDERATED_CREDENTIAL_ID": ""},
])
async def test_invalid_work_iq_auth_configuration_fails_before_transport(settings):
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, **settings, "FOUNDRY_IQ_API_VERSION": "2026-08-01-preview",
        "WORK_IQ_ENABLED": True, "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "m365",
    })
    with pytest.raises(ValueError, match="WORK_IQ_"):
        await client.retrieve(
            "question", obo_token=_TOKEN, native_permissions=True,
            work_iq_assertion=_ASSERTION,
        )
    assert not session.captured
    client.credential.get_token.assert_not_awaited()


async def test_native_search_provider_exception_is_redacted_without_retry(caplog, mock_config):
    from strategies import search_context_provider as search_provider

    provider = search_provider.SearchContextProvider(
        endpoint="https://search.invalid", index_name="documents", credential=None,
        conversation_id="authorized-conversation",
        get_obo_token=AsyncMock(return_value=_TOKEN), native_permissions=True,
    )
    boundary = AsyncMock()
    boundary.__aenter__.return_value.search.side_effect = RuntimeError(_TOKEN)
    with (
        patch.object(search_provider, "SearchClient", return_value=boundary),
        patch.object(search_provider, "get_config", return_value=mock_config),
    ):
        with pytest.raises(RuntimeError, match="Protected Search") as raised:
            await provider.invoking(ChatMessage(role=Role.USER, text="question"))
    assert _TOKEN not in "".join(traceback.format_exception(raised.value))
    assert _TOKEN not in caplog.text
    boundary.__aenter__.return_value.search.assert_awaited_once()


@pytest.mark.parametrize("status,payload", [
    (206, {"references": [{"sourceData": {"content": "partial"}}]}),
    (200, {"activity": [{"type": "workIQ", "error": {"message": _TOKEN}}]}),
    (403, {"error": {"message": _TOKEN}}),
])
async def test_no_partial_success_or_provider_error_leak(status, payload, caplog):
    client, _ = _build_client(payload, status=status, config_overrides=_PRIMARY)
    expected_error = HostedRetrievalError if status == 403 else RuntimeError
    with pytest.raises(expected_error) as raised:
        await client.retrieve("question", obo_token=_TOKEN, native_permissions=True)
    if status == 403:
        assert raised.value.code == "source_denied"
    assert _TOKEN not in str(raised.value)
    assert _TOKEN not in "".join(traceback.format_exception(raised.value))
    assert _TOKEN not in caplog.text


async def test_native_index_does_not_build_manual_group_authorization():
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, "FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": "searchIndex",
        "FOUNDRY_IQ_FILTER_ADD_ON_ENABLED": True,
        "FOUNDRY_IQ_API_VERSION": "2026-08-01-preview",
    })
    await client.retrieve(
        "question", obo_token=_TOKEN, native_permissions=True,
        user_context={"groups": ["untrusted-group"]}, conversation_id="untrusted-chat",
    )
    clause = session.captured["json"]["knowledgeSourceParams"][0]["filterAddOn"]
    assert "conversationId eq 'untrusted-chat'" in clause
    assert "metadata_security_user_ids/any()" in clause
    assert "not metadata_security_group_ids/any()" in clause
    assert "metadata_security_rbac_scope eq ''" in clause
    assert "untrusted-group" not in clause
    assert "any(u:" not in clause


@pytest.mark.parametrize("api_version", ["2026-05-01-preview", "2026-08-01-preview"])
async def test_upload_sidecar_uses_native_acl_without_extracting_owner(api_version):
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED": True,
        "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME": "uploads",
        "FOUNDRY_IQ_API_VERSION": api_version,
    })
    await client.retrieve(
        "question", obo_token=_TOKEN, native_permissions=True,
        conversation_id="authorized-conversation",
        user_context={"user_id": "opaque-platform-partition"},
    )
    params = session.captured["json"]["knowledgeSourceParams"]
    sidecar = next(item for item in params if item["knowledgeSourceName"] == "uploads")
    assert sidecar["failOnError"] and sidecar["alwaysQuerySource"]
    assert "conversationId eq 'authorized-conversation'" in sidecar["filterAddOn"]
    assert "NaN" not in sidecar["filterAddOn"] and "conversationId eq null" not in sidecar["filterAddOn"]
    assert "metadata_security_user_ids/any()" in sidecar["filterAddOn"]
    assert "opaque-platform-partition" not in sidecar["filterAddOn"]
    assert session.captured["headers"]["x-ms-query-source-authorization"] == _TOKEN
    assert _TOKEN not in repr(session.captured["json"])


async def test_august_mixed_sources_keep_required_upload_sidecar():
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, **_WORK_IQ_AUTH, **_INDEXED_SHAREPOINT,
        "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED": True,
        "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME": "uploads",
        "WORK_IQ_ENABLED": True, "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "m365",
        "FABRIC_IQ_ENABLED": True, "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME": "ontology",
        "FABRIC_DATA_AGENT_ENABLED": True, "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME": "analyst",
    })
    await client.retrieve(
        "question", obo_token=_TOKEN, native_permissions=True,
        work_iq_assertion=_ASSERTION, conversation_id="authorized-conversation",
        user_context={"user_id": "opaque-platform-partition"},
    )
    body = session.captured["json"]
    sources = body["knowledgeSourceParams"]
    assert {source["knowledgeSourceName"] for source in sources} == {
        "corpus", "uploads", "m365", "ontology", "analyst", "sharepoint",
    }
    assert all(source["failOnError"] and source["alwaysQuerySource"] for source in sources)
    assert body["retrievalReasoningEffort"] == {"kind": "low"}
    assert "conversationId eq 'authorized-conversation'" in next(
        source["filterAddOn"] for source in sources if source["knowledgeSourceName"] == "uploads"
    )
    assert session.captured["headers"]["x-ms-query-source-authorization"] == _TOKEN
    assert session.captured["headers"]["x-ms-query-work-iq-source-authorization"] == _ASSERTION


@pytest.mark.parametrize("kind", ["azureBlob", "searchIndex"])
@pytest.mark.parametrize("conversation", [None, "", " ", "NaN"])
async def test_native_upload_missing_conversation_fails_before_transport(kind, conversation):
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, "FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": kind,
        "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED": True,
        "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME": "uploads",
    })
    with pytest.raises(ValueError, match="conversation ID"):
        await client.retrieve(
            "question", obo_token=_TOKEN, native_permissions=True,
            conversation_id=conversation, user_context={"user_id": "opaque-platform-partition"},
        )
    assert not session.captured
    client.credential.get_token.assert_not_awaited()


async def test_native_search_sdk_provider_queries_conversation_with_opaque_partition(mock_config):
    from strategies import search_context_provider as search_provider

    async def documents():
        yield {"id": "chunk", "content": "Authorized upload", "title": "Upload", "url": "https://example.invalid/upload"}

    boundary = AsyncMock()
    boundary.__aenter__.return_value.search.return_value = documents()
    provider = search_provider.SearchContextProvider(
        endpoint="https://search.invalid", index_name="documents", credential=None,
        conversation_id="authorized-conversation", owner_id="opaque-platform-partition",
        get_obo_token=AsyncMock(return_value=_TOKEN), native_permissions=True,
    )
    with (
        patch.object(search_provider, "SearchClient", return_value=boundary),
        patch.object(search_provider, "get_config", return_value=mock_config),
    ):
        context = await provider.invoking(ChatMessage(role=Role.USER, text="question"))
    params = boundary.__aenter__.return_value.search.await_args.kwargs
    assert params["x_ms_query_source_authorization"] == _TOKEN
    assert "conversationId eq 'authorized-conversation'" in params["filter"]
    assert "metadata_security_user_ids/any()" in params["filter"]
    assert "opaque-platform-partition" not in params["filter"]
    assert "Authorized upload" in context.messages[0].text
    assert _TOKEN not in repr(context.messages)


async def test_native_provider_never_mints_blob_download_grants_or_leaks_headers():
    remote = SimpleNamespace(
        mcp_config=None,
        retrieve=AsyncMock(return_value=[{
            "title": "Synthetic", "content": "Authorized evidence",
            "link": "https://storage.invalid/protected/document.pdf",
        }]),
    )
    credentials = DelegatedRetrievalCredentials(_TOKEN, _ASSERTION)
    provider = provider_module.FoundryIQContextProvider(
        delegated_retrieval=credentials,
        get_obo_token=AsyncMock(return_value=_TOKEN), allow_anonymous=False,
    )
    with (
        patch.object(provider_module, "get_foundry_iq_client", return_value=remote),
        patch.object(provider_module, "sign_blob_url", AsyncMock()) as signer,
    ):
        context = await provider.invoking(ChatMessage(role=Role.USER, text="question"))
    signer.assert_not_awaited()
    assert "Authorized evidence" in context.messages[0].text
    assert "?sig=" not in context.messages[0].text
    assert _TOKEN not in repr(context.messages)
    assert _ASSERTION not in repr(context.messages)


@pytest.mark.parametrize("failure", [
    RuntimeError(_TOKEN), TimeoutError(_TOKEN), asyncio.CancelledError(),
])
async def test_native_provider_failure_and_cancellation_never_retry(failure, caplog):
    remote = SimpleNamespace(mcp_config=None, retrieve=AsyncMock(side_effect=failure))
    provider = provider_module.FoundryIQContextProvider(
        delegated_retrieval=DelegatedRetrievalCredentials(_TOKEN),
        get_obo_token=AsyncMock(return_value=_TOKEN), allow_anonymous=False,
    )
    with patch.object(provider_module, "get_foundry_iq_client", return_value=remote):
        expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else RuntimeError
        with pytest.raises(expected) as raised:
            await provider.invoking(ChatMessage(role=Role.USER, text="question"))
    remote.retrieve.assert_awaited_once()
    assert _TOKEN not in "".join(traceback.format_exception(raised.value))
    assert _TOKEN not in caplog.text


def test_credentials_are_immutable_redacted_and_not_picklable():
    credentials = DelegatedRetrievalCredentials(_TOKEN, _ASSERTION)
    assert _TOKEN not in repr(credentials) and _ASSERTION not in repr(credentials)
    with pytest.raises(AttributeError):
        credentials.search_token = "different"
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(credentials)


async def test_single_agent_hosted_clients_do_not_share_request_credentials(
    mock_config, patch_dependencies,
):
    from connectors.search import SearchClient
    from strategies import single_agent_rag_strategy_v2 as single
    from strategies.base_agent_strategy import hosted_runtime_construction

    singleton = SearchClient.__new__(SearchClient)
    singleton._request_api_access_token = None
    singleton._delegated_retrieval = None
    with (
        patch.object(single, "get_config", return_value=mock_config),
        patch.object(single, "get_search_client", return_value=singleton),
        patch.object(single, "get_genai_client", return_value=MagicMock()),
        hosted_runtime_construction(),
    ):
        strategies = [single.SingleAgentRAGStrategyV2() for _ in range(2)]
    for strategy, token in zip(strategies, ["synthetic-user-A", "synthetic-user-B"]):
        strategy.set_context("authorized-conversation")
        strategy.delegated_retrieval = DelegatedRetrievalCredentials(token)
        strategy._apply_search_request_context()

    assert strategies[0].search_client is not strategies[1].search_client
    assert all(strategy.search_client is not singleton for strategy in strategies)
    assert singleton._delegated_retrieval is None
    assert await strategies[0].search_client._get_search_user_token_for_trimming() == "synthetic-user-A"
    assert await strategies[1].search_client._get_search_user_token_for_trimming() == "synthetic-user-B"
    assert all(not strategy.search_client._allow_anonymous for strategy in strategies)

    strategies[0].search_client.set_request_context = MagicMock(side_effect=RuntimeError(_TOKEN))
    with pytest.raises(RuntimeError, match="Protected Search request context") as raised:
        strategies[0]._apply_search_request_context()
    assert _TOKEN not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("projection", [
    {"content": "Synthetic indexed evidence", "webUrl": "https://tenant.sharepoint.invalid/sites/knowledge/policy.docx",
     "title": "Policy"},
    {"content": "Synthetic indexed evidence",
     "metadata_spo_item_weburi": "https://tenant.sharepoint.invalid/sites/knowledge/policy.docx",
     "metadata_spo_item_name": "Policy"},
])
async def test_indexed_sharepoint_native_wire_response_and_unsigned_citation(projection):
    client, session = _build_client({"references": [
        {"type": "indexedSharePoint", "sourceData": projection},
    ]}, config_overrides={**_PRIMARY, **_INDEXED_SHAREPOINT})
    provider = provider_module.FoundryIQContextProvider(
        delegated_retrieval=DelegatedRetrievalCredentials(_TOKEN),
        get_obo_token=AsyncMock(return_value=_TOKEN), allow_anonymous=False,
    )
    with (
        patch.object(provider_module, "get_foundry_iq_client", return_value=client),
        patch.object(provider_module, "sign_blob_url", AsyncMock()) as signer,
    ):
        context = await provider.invoking(ChatMessage(role=Role.USER, text="question"))
    signer.assert_not_awaited()
    assert "[Policy](https://tenant.sharepoint.invalid/sites/knowledge/policy.docx)" in context.messages[0].text
    assert "Synthetic indexed evidence" in context.messages[0].text
    params = session.captured["json"]["knowledgeSourceParams"]
    indexed = next(source for source in params if source["kind"] == "indexedSharePoint")
    assert indexed["knowledgeSourceName"] == "sharepoint"
    assert indexed["failOnError"] and indexed["alwaysQuerySource"]
    assert "filterAddOn" not in indexed
    assert session.captured["headers"]["x-ms-query-source-authorization"] == _TOKEN
    assert _TOKEN not in repr(context.messages)
    records = client._normalize_references({"references": [
        {"type": "indexedSharePoint", "sourceData": projection},
    ]})
    assert records[0].source_type == "indexedSharePoint"


@pytest.mark.parametrize("key,value", [
    ("FOUNDRY_IQ_API_VERSION", "2026-05-01-preview"),
    ("SHAREPOINT_INDEXED_APPLICATION_ID", ""),
    ("SHAREPOINT_INDEXED_FEDERATED_CREDENTIAL_APPLICATION_ID", "invalid"),
    ("SHAREPOINT_INDEXED_SITE_URL", "http://tenant.sharepoint.invalid/sites/knowledge"),
    ("SHAREPOINT_INDEXED_TENANT_ID", ""),
    ("SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME", ""),
    ("SHAREPOINT_INDEXED_KS_NAME", "conflicting-name"),
])
async def test_indexed_sharepoint_incomplete_native_contract_rejected_before_transport(key, value):
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, **_INDEXED_SHAREPOINT, key: value,
    })
    with pytest.raises(ValueError):
        await client.retrieve("question", obo_token=_TOKEN, native_permissions=True)
    assert not session.captured
    client.credential.get_token.assert_not_awaited()


async def test_indexed_sharepoint_never_uses_ingestion_identity_without_user_token():
    client, session = _build_client({}, config_overrides={**_PRIMARY, **_INDEXED_SHAREPOINT})
    with pytest.raises(ValueError, match="delegated Search token"):
        await client.retrieve("question", native_permissions=True)
    assert not session.captured
    client.credential.get_token.assert_not_awaited()


async def test_indexed_sharepoint_ks_name_alias_preserves_existing_key():
    client, session = _build_client({}, config_overrides={
        **_PRIMARY, **_INDEXED_SHAREPOINT,
        "SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME": "",
        "SHAREPOINT_INDEXED_KS_NAME": "sharepoint",
    })
    await client.retrieve("question", obo_token=_TOKEN, native_permissions=True)
    assert any(
        source["knowledgeSourceName"] == "sharepoint"
        for source in session.captured["json"]["knowledgeSourceParams"]
    )
