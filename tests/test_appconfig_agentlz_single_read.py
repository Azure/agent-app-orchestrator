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
    assert labels == ["orchestrator", "agent-app-orchestrator", "gpt-rag-orchestrator", "agent-lz", None]


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