"""Agent Landing Zone single read (Azure/GPT-RAG#695): only the agent-lz label and AGENTLZ_ keys are read."""

from unittest.mock import patch

import ast
from pathlib import Path

import pytest

import constants
from connectors.appconfig import AppConfigClient, candidate_keys


def _client(monkeypatch, mock_identity_manager, values, env_enabled=False):
    monkeypatch.setenv("APP_CONFIG_ENDPOINT", "https://config.example.invalid")
    monkeypatch.setenv("allow_environment_variables", str(env_enabled).lower())
    with (
        patch("connectors.appconfig.get_identity_manager", return_value=mock_identity_manager),
        patch("connectors.appconfig.load", return_value=values) as load,
    ):
        return AppConfigClient(), load


def test_label_selectors_read_agent_lz_only(monkeypatch, mock_identity_manager):
    _, load = _client(monkeypatch, mock_identity_manager, {})
    labels = [s.label_filter for s in load.call_args.kwargs["selects"]]
    assert labels == ["orchestrator", "agent-app-orchestrator", "gpt-rag-orchestrator", "agent-lz", "\0"]


def test_default_transport_does_not_require_a_key_vault_uri(monkeypatch, mock_identity_manager):
    monkeypatch.delenv("APP_CONFIG_TLS_COMPATIBILITY", raising=False)
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)
    with patch("connectors.appconfig.create_appconfig_transport") as transport:
        _, load = _client(monkeypatch, mock_identity_manager, {})
    transport.assert_not_called()
    assert "transport" not in load.call_args.kwargs


def test_opt_in_uses_verified_transports_for_both_endpoints(monkeypatch, mock_identity_manager):
    monkeypatch.setenv("APP_CONFIG_TLS_COMPATIBILITY", "true")
    monkeypatch.setenv("KEY_VAULT_URI", "https://vault.example.invalid/")
    with patch("connectors.appconfig.create_appconfig_transport") as transport:
        _, load = _client(monkeypatch, mock_identity_manager, {})
    assert [call.args[0] for call in transport.call_args_list] == [
        "https://vault.example.invalid/",
        "https://config.example.invalid",
    ]
    assert load.call_args.kwargs["transport"] is transport.return_value
    vault_options = load.call_args.kwargs["key_vault_options"]
    assert vault_options.client_configs == {
        "https://vault.example.invalid/": {"transport": transport.return_value}
    }
    assert vault_options.credential is mock_identity_manager.get_credential.return_value


@pytest.mark.parametrize("value", ["", "yes", "typo"])
def test_invalid_transport_option_fails_before_loading(monkeypatch, mock_identity_manager, value):
    monkeypatch.setenv("APP_CONFIG_TLS_COMPATIBILITY", value)
    monkeypatch.setenv("APP_CONFIG_ENDPOINT", "https://config.example.invalid")
    with (
        patch("connectors.appconfig.get_identity_manager", return_value=mock_identity_manager),
        patch("connectors.appconfig.load") as load,
        pytest.raises(ValueError, match="must be true or false"),
    ):
        AppConfigClient()
    load.assert_not_called()


def test_opt_in_requires_explicit_key_vault_uri(monkeypatch, mock_identity_manager):
    monkeypatch.setenv("APP_CONFIG_TLS_COMPATIBILITY", "true")
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)
    monkeypatch.setenv("APP_CONFIG_ENDPOINT", "https://config.example.invalid")
    with (
        patch("connectors.appconfig.get_identity_manager", return_value=mock_identity_manager),
        patch("connectors.appconfig.load") as load,
        pytest.raises(ValueError, match="requires KEY_VAULT_URI"),
    ):
        AppConfigClient()
    load.assert_not_called()


@pytest.mark.parametrize("key", ["AGENTLZ_FOO", "GPT_RAG_FOO", "SEARCH_SERVICE_NAME"])
def test_candidate_keys_is_exact(key):
    assert candidate_keys(key) == [key]


def test_appconfig_reads_agentlz_key(monkeypatch, mock_identity_manager):
    cfg, _ = _client(monkeypatch, mock_identity_manager, {"AGENTLZ_FOO": "new"})
    assert cfg.get("AGENTLZ_FOO") == "new"


def test_legacy_gpt_rag_key_is_not_a_fallback(monkeypatch, mock_identity_manager):
    cfg, _ = _client(monkeypatch, mock_identity_manager, {"GPT_RAG_FOO": "old"})
    assert cfg.get("AGENTLZ_FOO", default="d") == "d"
    with pytest.raises(Exception, match="AGENTLZ_FOO not found"):
        cfg.get("AGENTLZ_FOO")


def test_env_reads_exact_key_only(monkeypatch, mock_identity_manager):
    cfg, _ = _client(monkeypatch, mock_identity_manager, {}, env_enabled=True)
    monkeypatch.setenv("GPT_RAG_BAR", "old")
    assert cfg.get("AGENTLZ_BAR", default="d") == "d"
    monkeypatch.setenv("AGENTLZ_BAR", "new")
    assert cfg.get("AGENTLZ_BAR") == "new"


def test_missing_key_still_raises(monkeypatch, mock_identity_manager):
    cfg, _ = _client(monkeypatch, mock_identity_manager, {})
    assert cfg.get("AGENTLZ_MISSING", default="d") == "d"
    with pytest.raises(Exception, match="AGENTLZ_MISSING not found"):
        cfg.get("AGENTLZ_MISSING")


def test_telemetry_service_name_uses_agentlz_prefix():
    main_src = Path(__file__).resolve().parents[1] / "src" / "main.py"
    tree = ast.parse(main_src.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "configure_monitoring"
    ]
    assert len(calls) == 1
    service_name = calls[0].args[2]
    assert isinstance(service_name, ast.Constant) and service_name.value == "agentlz.orchestrator"
    assert constants.APP_NAME == "agent-app-orchestrator"