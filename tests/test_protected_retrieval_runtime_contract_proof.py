"""Real pinned SDK hosted credential integration, with no live Azure boundaries."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agent_framework import ChatMessage, Role
from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.responses import InMemoryResponseProvider
from fastapi.testclient import TestClient

from strategies import foundry_iq_context_provider as foundry
from strategies import maf_lite_strategy as lite
from strategies import maf_agent_service_strategy as service


_TOKEN = "synthetic-opaque-search-token"
_PLATFORM_USER = "opaque-platform-partition-not-an-entra-object-id"
_CALL_ID = "synthetic-opaque-call-id"


@pytest.fixture(scope="module", autouse=True)
def _load_hosted_without_exporters():
    # Importing this entrypoint constructs its module-level app. Do not start
    # real exporters at collection time or modify other modules' audit setup.
    global hosted
    with patch.dict("os.environ", {"OTEL_SDK_DISABLED": "true"}):
        from api import hosted_entrypoint

        hosted = hosted_entrypoint
        yield


@pytest.fixture()
def protected_host(mock_config, patch_dependencies):
    original = mock_config.get.side_effect
    settings = {
        "AGENT_STRATEGY": "maf_lite",
        "ALLOW_ANONYMOUS": False,
        "RETRIEVAL_BACKEND": "foundry_iq",
        "SEARCH_SERVICE_QUERY_ENDPOINT": "https://search.invalid",
        "SEARCH_RAG_INDEX_NAME": "documents",
        "KNOWLEDGE_BASE_NAME": "protected-kb",
    }
    mock_config.get.side_effect = lambda key, default=None, type=str: settings.get(
        key, original(key, default, type)
    )
    with patch.object(hosted, "get_config", return_value=mock_config):
        app = hosted.create_app(
            store=InMemoryResponseProvider(), configure_observability=None
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def _post(client, protocol, *, headers=None, conversation_id=None, stream=True):
    payload = (
        {"input": "Find the protected document.", "stream": stream, "store": False}
        if protocol == "responses"
        else {"messages": [{"role": "user", "content": "Find the protected document."}]}
    )
    # Neither model/body identity nor platform partition context grants data access.
    payload["metadata"] = {"principal_id": "forged-body-owner"}
    if conversation_id is not None:
        if protocol == "responses":
            payload["metadata"]["conversation_id"] = conversation_id
        else:
            payload["conversation_id"] = conversation_id
    return client.post(f"/{protocol}", json=payload, headers=headers or {})


def _probe_strategy():
    async def flow(_ask):
        yield "synthetic answer"

    return SimpleNamespace(initiate_agent_flow=flow)


def test_real_responses_sdk_exposes_only_prefixed_client_headers(protected_host):
    captured = {}
    original = hosted._responses_events

    async def inspect_context(request, context, cancellation_signal, provider):
        captured["client_headers"] = dict(context.client_headers)
        captured["partition"] = context.platform_context.user_id_key
        captured["call_id"] = context.platform_context.call_id
        current = get_request_context()
        captured["current"] = (current.user_id, current.call_id)
        captured["outbound_platform_headers"] = current.platform_headers()
        async for event in original(request, context, cancellation_signal, provider):
            yield event

    with (
        patch.object(hosted, "_responses_events", inspect_context),
        patch.object(
            hosted.AgentStrategyFactory,
            "get_strategy",
            AsyncMock(return_value=_probe_strategy()),
        ),
    ):
        response = _post(
            protected_host,
            "responses",
            headers={
                "X-Client-Search-Token": _TOKEN,
                "x-agent-user-id": _PLATFORM_USER,
                "x-agent-foundry-call-id": _CALL_ID,
                "x-not-forwarded": "not-a-client-header",
            },
        )

    assert response.status_code == 200
    assert captured == {
        "client_headers": {"x-client-search-token": _TOKEN},
        "partition": _PLATFORM_USER,
        "call_id": _CALL_ID,
        "current": (_PLATFORM_USER, _CALL_ID),
        "outbound_platform_headers": {"x-agent-foundry-call-id": _CALL_ID},
    }
    assert get_request_context().user_id is None


def test_legacy_adapter_requires_explicit_request_header_extraction(protected_host):
    captured = {}
    original = hosted._create_hosted_streaming_response

    async def inspect_request(request, **kwargs):
        captured["token"] = request.headers.get("x-client-search-token")
        captured["platform_user"] = request.headers.get("x-agent-user-id")
        captured["sdk_user"] = get_request_context().user_id
        return await original(request, **kwargs)

    with (
        patch.object(hosted, "_create_hosted_streaming_response", inspect_request),
        patch.object(
            hosted.AgentStrategyFactory,
            "get_strategy",
            AsyncMock(return_value=_probe_strategy()),
        ),
    ):
        response = _post(
            protected_host,
            "invocations",
            headers={
                "X-Client-Search-Token": _TOKEN,
                "x-agent-user-id": _PLATFORM_USER,
            },
        )

    assert response.status_code == 200
    assert captured == {
        "token": _TOKEN,
        "platform_user": _PLATFORM_USER,
        "sdk_user": None,
    }


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
def test_missing_delegated_token_rejected_before_strategy(protected_host, protocol):
    factory = AsyncMock(return_value=_probe_strategy())
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", factory):
        response = _post(
            protected_host,
            protocol,
            headers={
                "x-agent-user-id": _PLATFORM_USER,
                "x-agent-foundry-call-id": _CALL_ID,
            },
        )
    assert response.status_code in {401, 403}
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
@pytest.mark.parametrize("module,kind", [(lite, lite.MafLiteStrategy), (service, service.MafAgentServiceStrategy)])
def test_hosted_maf_lite_passes_exchanged_token_without_obo(
    protected_host, protocol, mock_config, caplog, module, kind
):
    with patch.object(module, "get_config", return_value=mock_config):
        strategy = kind()
    strategy.embedding_deployment = None
    remote = SimpleNamespace(
        mcp_config=None,
        retrieve=AsyncMock(return_value=[{"title": "Synthetic", "content": "evidence"}]),
    )

    async def provider_flow(ask):
        provider = await strategy._create_search_provider()
        result = await provider.invoking(ChatMessage(role=Role.USER, text=ask))
        yield result.messages[0].text

    strategy.initiate_agent_flow = provider_flow
    with (
        patch.object(
            hosted.AgentStrategyFactory, "get_strategy", AsyncMock(return_value=strategy)
        ),
        patch.object(module, "get_retrieval_backend", return_value="foundry_iq"),
        patch.object(foundry, "get_foundry_iq_client", return_value=remote),
        patch.object(module, "acquire_obo_search_token", AsyncMock()) as exchange,
    ):
        response = _post(
            protected_host, protocol, headers={"x-client-search-token": _TOKEN}
        )

    assert response.status_code == 200
    remote.retrieve.assert_awaited_once()
    assert remote.retrieve.await_args.kwargs["obo_token"] == _TOKEN
    exchange.assert_not_awaited()
    assert _TOKEN not in caplog.text


def _override(cfg, **settings):
    previous = cfg.get.side_effect
    cfg.get.side_effect = lambda key, default=None, type=str: (
        settings[key] if key in settings else previous(key, default, type)
    )


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"x-client-search-token": "Bearer synthetic"}, 401),
    ({"x-client-search-token": " "}, 401),
    ({"x-client-search-token": "x" * 8193}, 431),
    ([("x-client-search-token", "one"), ("x-client-search-token", "two")], 401),
])
def test_credential_validation_before_stream(protected_host, protocol, headers, status):
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = _post(protected_host, protocol, headers=headers)
    assert response.status_code == status
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
@pytest.mark.parametrize("setting,value", [
    ("SEARCH_RETRIEVAL_ENABLED", False),
    ("SEARCH_SERVICE_QUERY_ENDPOINT", ""),
])
def test_intentionally_disabled_or_unconfigured_search_allows_chat(
    protected_host, mock_config, protocol, setting, value
):
    _override(mock_config, **{setting: value})
    with patch.object(
        hosted.AgentStrategyFactory, "get_strategy", AsyncMock(return_value=_probe_strategy())
    ):
        response = _post(protected_host, protocol)
    assert response.status_code == 200


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
def test_upload_sidecar_missing_conversation_blocks_before_strategy(
    protected_host, mock_config, protocol
):
    _override(mock_config, FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED=True)
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = _post(protected_host, protocol, headers={"x-client-search-token": _TOKEN})
    assert response.status_code == 422
    assert "conversation ID" in response.text
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
@pytest.mark.parametrize("upload_path", ["sidecar", "primary", "ai_search"])
def test_native_upload_scope_and_token_forward_without_platform_oid(
    protected_host, mock_config, protocol, upload_path
):
    settings = (
        {"FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED": True}
        if upload_path == "sidecar" else
        {"FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": "searchIndex"}
        if upload_path == "primary" else
        {"RETRIEVAL_BACKEND": "ai_search"}
    )
    _override(mock_config, **settings)
    instance = _probe_strategy()
    seen = {}

    async def flow(_ask):
        seen["conversation"] = instance.conversation["id"]
        seen["token"] = instance.delegated_retrieval.search_token
        seen["context"] = instance.user_context
        yield "synthetic evidence"

    instance.initiate_agent_flow = flow
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock(return_value=instance)):
        response = _post(
            protected_host, protocol, conversation_id="authorized-conversation",
            headers={"x-client-search-token": _TOKEN, "x-agent-user-id": _PLATFORM_USER},
        )
    assert response.status_code == 200
    assert seen == {"conversation": "authorized-conversation", "token": _TOKEN, "context": {}}
    assert instance.delegated_retrieval is None


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
@pytest.mark.parametrize("upload_path", ["primary", "ai_search"])
def test_native_primary_upload_missing_conversation_fails_before_strategy(
    protected_host, mock_config, protocol, upload_path
):
    _override(mock_config, **(
        {"FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": "searchIndex"} if upload_path == "primary"
        else {"RETRIEVAL_BACKEND": "ai_search"}
    ))
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = _post(protected_host, protocol, headers={"x-client-search-token": _TOKEN})
    assert response.status_code == 422
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
def test_work_iq_requires_different_forwarded_assertion(
    protected_host, mock_config, protocol
):
    _override(
        mock_config, WORK_IQ_ENABLED=True,
        WORK_IQ_APPLICATION_ID="11111111-1111-4111-8111-111111111111",
        WORK_IQ_FEDERATED_CREDENTIAL_ID="22222222-2222-4222-8222-222222222222",
    )
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = _post(protected_host, protocol, headers={"x-client-search-token": _TOKEN})
    assert response.status_code == 401
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
def test_missing_work_iq_registration_is_configuration_error_before_strategy(
    protected_host, mock_config, protocol
):
    _override(mock_config, WORK_IQ_ENABLED=True)
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = _post(protected_host, protocol, headers={
            "x-client-search-token": _TOKEN,
            "x-client-work-iq-assertion": "synthetic-app-assertion",
        })
    assert response.status_code == 503
    assert "WORK_IQ_APPLICATION_ID" in response.text
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
def test_search_backend_cannot_silently_ignore_enabled_remote_source(
    protected_host, mock_config, protocol
):
    _override(mock_config, RETRIEVAL_BACKEND="ai_search", FABRIC_IQ_ENABLED=True)
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = _post(protected_host, protocol, headers={"x-client-search-token": _TOKEN})
    assert response.status_code == 503
    factory.assert_not_awaited()


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("store_field", [{}, {"store": True}, {"store": False}])
def test_token_bearing_responses_never_enter_sdk_resilient_or_store_path(
    protected_host, stream, store_field, caplog
):
    from azure.ai.agentserver.responses.hosting._resilient_orchestrator import (
        ResilientResponseOrchestrator,
    )

    store = protected_host.app._responses_provider
    with (
        patch.object(
            ResilientResponseOrchestrator, "build_resilient_input",
            side_effect=AssertionError("Resilient input must never be constructed"),
        ) as durable,
        patch.object(store, "create_response", AsyncMock()) as persist,
        patch.object(
            hosted.AgentStrategyFactory, "get_strategy", AsyncMock(return_value=_probe_strategy())
        ),
    ):
        response = protected_host.post(
            "/responses", json={"input": "question", "stream": stream, **store_field},
            headers={"x-client-search-token": _TOKEN},
        )
    assert response.status_code == 200
    durable.assert_not_called()
    persist.assert_not_awaited()
    assert store._entries == {} and store._item_store == {} and store._stream_events == {}
    assert _TOKEN not in response.text and _TOKEN not in caplog.text


def test_background_token_request_is_rejected_without_sdk_task_input(protected_host):
    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock()) as factory:
        response = protected_host.post(
            "/responses", json={"input": "question", "background": True, "store": True},
            headers={"x-client-search-token": _TOKEN},
        )
    assert response.status_code == 422
    factory.assert_not_awaited()


@pytest.mark.parametrize("protocol,stream", [
    ("responses", True), ("responses", False), ("invocations", True),
])
@pytest.mark.parametrize("status,challenge", [(401, True), (403, True), (403, False)])
@pytest.mark.parametrize("partial", [False, True])
def test_search_challenge_through_native_provider_and_actual_hosted_adapter(
    protected_host, mock_config, protocol, stream, status, challenge, partial, caplog,
):
    from test_foundry_iq import _build_client
    from test_retrieval_claims_recovery import CLAIMS, HEADER

    remote, session = _build_client(
        {"error": {"message": "synthetic-sensitive-provider-body"}},
        status=status, config_overrides={"FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "corpus"},
    )
    session.response_headers = {"WWW-Authenticate": HEADER} if challenge else {}
    with patch.object(lite, "get_config", return_value=mock_config):
        strategy = lite.MafLiteStrategy()
    strategy.embedding_deployment = None

    async def provider_flow(ask):
        if partial:
            yield "synthetic partial output"
        provider = await strategy._create_search_provider()
        await provider.invoking(ChatMessage(role=Role.USER, text=ask))
        pytest.fail("A failed required source must not produce an answer")

    strategy.initiate_agent_flow = provider_flow
    with (
        patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock(return_value=strategy)),
        patch.object(lite, "get_retrieval_backend", return_value="foundry_iq"),
        patch.object(foundry, "get_foundry_iq_client", return_value=remote),
    ):
        response = _post(
            protected_host, protocol, headers={"x-client-search-token": _TOKEN}, stream=stream,
        )
    assert response.status_code == 200
    frames = [
        json.loads(line[5:].strip()) for line in response.text.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]
    errors = [
        frame["response"]["error"] if frame["type"] == "response.failed" else frame
        for frame in frames if frame["type"] in {"error", "response.failed"}
    ]
    if protocol == "responses" and not stream:
        assert response.json()["status"] == "failed"
        assert response.json()["output"] == []
        errors = [response.json()["error"]]
    assert len(errors) == 1
    assert errors[0]["code"] == ("interaction_required" if challenge else "source_denied")
    assert errors[0].get("claims_challenge") == (CLAIMS if challenge else None)
    assert not any(frame["type"] == "response.completed" for frame in frames)
    assert ("synthetic partial output" in response.text) == (partial and stream)
    assert strategy.delegated_retrieval is None
    store = protected_host.app._responses_provider
    assert store._entries == {} and store._item_store == {} and store._stream_events == {}
    assert _TOKEN not in response.text + caplog.text
    assert "synthetic-sensitive-provider-body" not in response.text + caplog.text
    assert "synthetic-mfa" not in caplog.text


@pytest.mark.parametrize("protocol", ["responses", "invocations"])
def test_concurrent_and_alternating_requests_keep_credentials_local(
    protected_host, protocol, caplog
):
    instances = []
    seen = []

    def create(*_args, **_kwargs):
        instance = SimpleNamespace()

        async def flow(_ask):
            before = instance.delegated_retrieval.search_token
            await asyncio.sleep(0.02)
            seen.append((before, instance.delegated_retrieval.search_token))
            yield "synthetic evidence"

        instance.initiate_agent_flow = flow
        instances.append(instance)
        return instance

    def invoke(token):
        return _post(protected_host, protocol, headers={"x-client-search-token": token})

    with patch.object(hosted.AgentStrategyFactory, "get_strategy", AsyncMock(side_effect=create)):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(invoke, ["synthetic-user-A", "synthetic-user-B"]))
        responses.append(invoke("synthetic-user-A"))
    assert all(response.status_code == 200 for response in responses)
    assert sorted(seen) == [
        ("synthetic-user-A", "synthetic-user-A"),
        ("synthetic-user-A", "synthetic-user-A"),
        ("synthetic-user-B", "synthetic-user-B"),
    ]
    assert all(instance.delegated_retrieval is None for instance in instances)
    assert "synthetic-user-A" not in caplog.text and "synthetic-user-B" not in caplog.text


def test_http_header_telemetry_sanitization_cannot_be_disabled(monkeypatch):
    import os
    from opentelemetry.util.http import SanitizeValue

    key = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"
    monkeypatch.setenv(key, "existing-secret")
    hosted.create_app(store=InMemoryResponseProvider(), configure_observability=None)
    sanitizer = SanitizeValue(os.environ[key].split(","))
    for header in ("x-client-search-token", "x-client-work-iq-assertion",
                   "x-ms-query-source-authorization"):
        assert sanitizer.sanitize_header_value(header, _TOKEN) == "[REDACTED]"
    assert "existing-secret" in os.environ[key]
