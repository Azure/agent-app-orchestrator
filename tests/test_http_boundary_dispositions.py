"""HTTP, optional startup and configuration failure boundaries without Azure."""

import asyncio
import logging
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import AzureError
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from starlette.requests import Request
from tenacity import RetryError

from api import dashboard, dashboard_config
from api.config_settings import find_spec
from connectors.appconfig import AppConfigClient
from schemas import OrchestratorRequest
from test_dashboard_config import _build_cfg, _make_app
from test_primary_strategy_failure_boundaries import main_module


MARKER = "synthetic-private-http-provider-detail"


@pytest.fixture
def actual_config(monkeypatch):
    monkeypatch.setenv("allow_environment_variables", "false")
    cfg = AppConfigClient.__new__(AppConfigClient)
    cfg.disabled = False
    cfg.client = {}
    return cfg


def _retry_failure():
    future = Future()
    future.set_exception(RuntimeError(MARKER))
    return RetryError(future)


async def _ask(main, authorization=None):
    request = Request({"type": "http", "method": "POST", "path": "/orchestrator", "headers": []})
    return await main.orchestrator_endpoint(
        request, Response(), OrchestratorRequest(ask="test", user_context={}),
        authorization=authorization,
    )


@pytest.mark.parametrize("surface", ["orchestrator", "history"])
async def test_unexpected_auth_configuration_failure_never_downgrades_to_anonymous(
    main_module, actual_config, monkeypatch, surface,
):
    failure = RuntimeError(MARKER)

    def read(name):
        if name.startswith("OAUTH_AZURE_AD_"):
            raise failure
        return None

    monkeypatch.setattr(actual_config, "get_config_with_retry", MagicMock(side_effect=read))
    monkeypatch.setattr(main_module, "cfg", actual_config)
    factory = AsyncMock()
    monkeypatch.setattr(main_module.Orchestrator, "from_turn_request", factory)
    with pytest.raises(RuntimeError) as caught:
        if surface == "orchestrator":
            await _ask(main_module, "Bearer synthetic-token")
        else:
            await main_module.validate_user_access("Bearer synthetic-token", "history")
    assert caught.value is failure
    factory.assert_not_awaited()


@pytest.mark.parametrize("unavailable", ["missing", "retry"])
async def test_actual_optional_config_contract_keeps_anonymous_and_history_gates(
    main_module, actual_config, monkeypatch, unavailable,
):
    if unavailable == "retry":
        monkeypatch.setattr(actual_config, "get_config_with_retry", MagicMock(side_effect=_retry_failure()))
    monkeypatch.setattr(main_module, "cfg", actual_config)
    factory = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(main_module.Orchestrator, "from_turn_request", factory)
    result = await _ask(main_module, "Bearer synthetic-token")
    assert result.status_code == 200
    turn = factory.await_args.args[0]
    assert turn.user_context["principal_id"] == "anonymous"
    assert turn.request_access_token is None
    with pytest.raises(HTTPException) as caught:
        await main_module.validate_user_access("Bearer synthetic-token", "history")
    assert caught.value.status_code == 401


@pytest.mark.parametrize("surface", ["orchestrator", "history", "admin"])
@pytest.mark.parametrize("failure_type", [RuntimeError, AzureError, asyncio.CancelledError])
async def test_auth_translation_is_fail_closed_bounded_and_preserves_cancellation(
    main_module, actual_config, monkeypatch, caplog, surface, failure_type,
):
    actual_config.client.update({
        "OAUTH_AZURE_AD_TENANT_ID": "tenant",
        "OAUTH_AZURE_AD_CLIENT_ID": "client",
    })
    monkeypatch.setattr(main_module, "cfg", actual_config)
    failure = failure_type(MARKER)
    validation = AsyncMock(side_effect=failure)
    monkeypatch.setattr(main_module, "validate_access_token", validation)
    monkeypatch.setattr(dashboard, "validate_access_token", validation)
    if surface == "orchestrator":
        call = _ask(main_module, "Bearer synthetic-token")
    elif surface == "history":
        call = main_module.validate_user_access("Bearer synthetic-token", "history")
    else:
        call = dashboard.require_admin("Bearer synthetic-token", actual_config)
    with pytest.raises(asyncio.CancelledError if failure_type is asyncio.CancelledError else HTTPException) as caught:
        await call
    if failure_type is asyncio.CancelledError:
        assert caught.value is failure
    else:
        assert caught.value.status_code == 401
        assert MARKER not in str(caught.value.detail)
    assert MARKER not in caplog.text


@pytest.mark.parametrize("surface", ["orchestrator", "history", "admin"])
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_known_auth_http_status_is_preserved(
    main_module, actual_config, monkeypatch, caplog, surface, status,
):
    actual_config.client.update({
        "OAUTH_AZURE_AD_TENANT_ID": "tenant",
        "OAUTH_AZURE_AD_CLIENT_ID": "client",
    })
    monkeypatch.setattr(main_module, "cfg", actual_config)
    failure = HTTPException(status, "Existing public auth detail")
    validation = AsyncMock(side_effect=failure)
    monkeypatch.setattr(main_module, "validate_access_token", validation)
    monkeypatch.setattr(dashboard, "validate_access_token", validation)
    with pytest.raises(HTTPException) as caught:
        if surface == "orchestrator":
            await _ask(main_module, "Bearer synthetic-token")
        elif surface == "history":
            await main_module.validate_user_access("Bearer synthetic-token", "history")
        else:
            await dashboard.require_admin("Bearer synthetic-token", actual_config)
    assert caught.value is failure


@pytest.mark.parametrize("stage", ["identity", "token", "search", "agent"])
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_optional_prewarm_stays_nonfatal_bounded_but_cancellable(
    main_module, monkeypatch, caplog, stage, failure_type,
):
    failure = failure_type(MARKER)
    identity = MagicMock()
    identity.get_aio_credential.return_value.get_token = AsyncMock(
        side_effect=failure if stage == "token" else None,
    )
    identity_factory = MagicMock(
        return_value=identity, side_effect=failure if stage == "identity" else None,
    )
    search = SimpleNamespace(is_index_empty=AsyncMock(side_effect=failure if stage == "search" else None))
    monkeypatch.setattr("connectors.identity_manager.get_identity_manager", identity_factory)
    monkeypatch.setattr("connectors.search.get_search_client", lambda: search)
    monkeypatch.setattr("connectors.cosmosdb.get_cosmosdb_client", MagicMock())
    monkeypatch.setattr(
        "startup_warmup.prewarm_agents_for_strategy",
        AsyncMock(side_effect=failure if stage == "agent" else None),
    )
    with (
        patch.object(main_module.AuditEmitter, "configure"),
        patch.object(main_module.Telemetry, "configure_monitoring"),
    ):
        if failure_type is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError) as caught:
                async with main_module.lifespan(main_module.app):
                    pytest.fail("Cancelled startup must not enter application")
            assert caught.value is failure
        else:
            async with main_module.lifespan(main_module.app):
                pass
            assert "Startup" in caplog.text
    assert MARKER not in caplog.text


@pytest.mark.parametrize("kind", ["os", "unicode", "unexpected"])
def test_banner_recovers_only_from_expected_file_errors(main_module, monkeypatch, caplog, kind):
    error_type = {"os": OSError, "unicode": UnicodeError, "unexpected": RuntimeError}[kind]
    failure = error_type(MARKER)
    source = SimpleNamespace(exists=lambda: True, read_text=MagicMock(side_effect=failure))
    monkeypatch.setattr(main_module, "VERSION_FILE", source)
    if kind == "unexpected":
        with pytest.raises(RuntimeError) as caught:
            main_module._startup_banner()
        assert caught.value is failure
    else:
        main_module._startup_banner()
        assert "version" in caplog.text.lower()
    assert MARKER not in caplog.text


@pytest.mark.parametrize("outcome", ["missing", "retry", "unexpected"])
def test_dashboard_reads_use_actual_provider_optional_contract(
    actual_config, monkeypatch, caplog, outcome,
):
    spec = find_spec("CHAT_TEMPERATURE")
    failure = RuntimeError(MARKER) if outcome == "unexpected" else _retry_failure()
    if outcome != "missing":
        monkeypatch.setattr(actual_config, "get_config_with_retry", MagicMock(side_effect=failure))
    if outcome == "unexpected":
        with pytest.raises(RuntimeError) as caught:
            dashboard_config._read_current_value(actual_config, spec)
        assert caught.value is failure
    else:
        assert dashboard_config._read_current_value(actual_config, spec) == spec.default
    assert MARKER not in caplog.text


def test_dashboard_bad_value_fallback_does_not_log_raw_value(caplog):
    spec = find_spec("CHAT_TEMPERATURE")
    assert dashboard_config._coerce_for_type(MARKER, spec) == spec.default
    assert MARKER not in caplog.text


@pytest.mark.parametrize("failure_type", [AzureError, RuntimeError])
def test_dashboard_partial_writes_preserve_500_per_key_and_skip_refresh(caplog, failure_type):
    cfg = _build_cfg()
    writes = []

    def write(key, value, label):
        if key == "CHAT_TEMPERATURE":
            raise failure_type(MARKER)
        writes.append((key, value, label))

    cfg.set_value.side_effect = write
    with patch.object(dashboard_config, "get_config") as refresh:
        response = TestClient(_make_app(cfg)).put("/api/dashboard/config", json={
            "settings": [
                {"key": "AGENT_STRATEGY", "value": "maf_lite"},
                {"key": "CHAT_TEMPERATURE", "value": 0.7},
                {"key": "REASONING_EFFORT", "value": "low"},
            ],
        })
    assert response.status_code == 500
    assert [item[0] for item in writes] == ["AGENT_STRATEGY", "REASONING_EFFORT"]
    assert all(item[2] == "gpt-rag-orchestrator" for item in writes)
    assert response.json()["detail"]["errors"][0]["key"] == "CHAT_TEMPERATURE"
    assert response.json()["detail"]["errors"][0]["error"]
    assert MARKER not in response.text + caplog.text
    refresh.assert_not_called()


async def test_dashboard_write_cancellation_preserves_identity_and_skips_refresh():
    cfg = _build_cfg()
    failure = asyncio.CancelledError(MARKER)
    cfg.set_value.side_effect = failure
    body = dashboard_config.DashboardConfigUpdateRequest(
        settings=[{"key": "CHAT_TEMPERATURE", "value": 0.7}],
    )
    with patch.object(dashboard_config, "get_config") as refresh:
        with pytest.raises(asyncio.CancelledError) as caught:
            await dashboard_config.update_config(body, cfg)
    assert caught.value is failure
    refresh.assert_not_called()


async def test_request_debug_failure_is_optional_without_raw_diagnostic(main_module, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(main_module, "_format_request_debug", MagicMock(side_effect=RuntimeError(MARKER)))
    monkeypatch.setattr(main_module.Orchestrator, "from_turn_request", AsyncMock(return_value=SimpleNamespace()))
    response = await _ask(main_module)
    assert response.status_code == 200
    assert "Failed to render request debug info" in caplog.text
    assert MARKER not in caplog.text


async def test_request_context_logging_failure_is_reported_without_breaking_turn(
    main_module, monkeypatch, caplog,
):
    original = logging.info

    def info(message, *args, **kwargs):
        if message.startswith("[Orchestrator] Request context:"):
            raise RuntimeError(MARKER)
        return original(message, *args, **kwargs)

    monkeypatch.setattr(logging, "info", info)
    monkeypatch.setattr(main_module.Orchestrator, "from_turn_request", AsyncMock(return_value=SimpleNamespace()))
    response = await _ask(main_module)
    assert response.status_code == 200
    assert "Failed to render request context" in caplog.text
    assert MARKER not in caplog.text


@pytest.mark.parametrize("failure_stage", ["write", "refresh"])
def test_actual_appconfig_write_consumer_never_reports_failed_update_as_applied(
    actual_config, monkeypatch, caplog, failure_stage,
):
    monkeypatch.setenv("APP_CONFIG_ENDPOINT", "https://example.invalid")
    actual_config.credential = MagicMock()
    sdk = MagicMock()
    sdk.set_configuration_setting.side_effect = AzureError(MARKER) if failure_stage == "write" else None
    with (
        patch("connectors.appconfig.AzureAppConfigurationClient", return_value=sdk),
        patch.object(dashboard_config, "get_config", side_effect=RuntimeError(MARKER)) as refresh,
    ):
        response = TestClient(_make_app(actual_config), raise_server_exceptions=False).put(
            "/api/dashboard/config",
            json={"settings": [{"key": "CHAT_TEMPERATURE", "value": 0.7}]},
        )
    assert response.status_code == 500
    sdk.set_configuration_setting.assert_called_once()
    setting = sdk.set_configuration_setting.call_args.args[0]
    assert (setting.key, setting.value, setting.label) == ("CHAT_TEMPERATURE", "0.7", "gpt-rag-orchestrator")
    if failure_stage == "write":
        refresh.assert_not_called()
        assert response.json()["detail"]["errors"] == [
            {"key": "CHAT_TEMPERATURE", "error": "Unable to persist setting"},
        ]
    else:
        refresh.assert_called_once_with("refresh")
    assert MARKER not in response.text + caplog.text
