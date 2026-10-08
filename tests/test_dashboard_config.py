"""Tests for the admin dashboard Configuration tab API (#512).

Coverage:

* Admin gate is enforced (without an override the gated endpoints return 401/403).
* GET returns the expected section + setting shape (every allowlisted key present).
* PUT rejects an enum value outside the allowlist (422 with per-key error).
* PUT rejects a numeric value outside the spec's range (422 with per-key error).
* PUT rejects a denylist key even when the request body contains it (422 + denylist message).
* PUT writes accepted values via ``cfg.set_value`` and returns the refreshed state.
* The apply endpoint is a soft restart that refreshes the cache and returns ``status="applied"``.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_cfg(initial: Dict[str, Any] | None = None) -> MagicMock:
    """Return a MagicMock standing in for AppConfigClient with read+write."""
    store: Dict[str, Any] = dict(initial or {})
    cfg = MagicMock()

    def _get_value(key: str, default: Any = None, allow_none: bool = False, type: Any = str):  # noqa: A002
        if key in store:
            return store[key]
        if allow_none:
            return None
        return default

    def _set_value(key: str, value: Any, label: str = "gpt-rag-orchestrator"):
        store[key] = str(value).lower() if isinstance(value, bool) else str(value)

    cfg.get_value = MagicMock(side_effect=_get_value)
    cfg.set_value = MagicMock(side_effect=_set_value)
    cfg._store = store
    return cfg


def _allow_admin():  # used to bypass require_admin in most tests
    return None


def _make_app(cfg, admin_override=_allow_admin) -> FastAPI:
    from api.dashboard_config import router
    from api.dashboard import require_admin
    from dependencies import get_config

    app = FastAPI()
    app.include_router(router)
    if admin_override is not None:
        app.dependency_overrides[require_admin] = admin_override
    app.dependency_overrides[get_config] = lambda: cfg
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_config_endpoints_require_admin_when_auth_on():
    """Without the admin override the real gate runs and rejects unauthenticated requests."""
    cfg = _build_cfg({"AGENT_STRATEGY": "single_agent_rag"})
    # The require_admin gate reads AAD_TENANT_ID via the same `cfg` dep, so
    # returning a truthy value here switches the gate on.
    cfg.get_value = MagicMock(return_value="tenant-id")

    app = _make_app(cfg, admin_override=None)
    client = TestClient(app)
    r = client.get("/api/dashboard/config")
    assert r.status_code == 401


def test_get_config_returns_every_allowlisted_key():
    from api.config_settings import ALLOWED_KEYS, SECTIONS

    cfg = _build_cfg({"AGENT_STRATEGY": "maf_lite", "SEARCH_RETRIEVAL_ENABLED": "false"})
    app = _make_app(cfg)
    client = TestClient(app)
    r = client.get("/api/dashboard/config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "gpt-rag-orchestrator"

    returned_keys: set[str] = set()
    returned_section_ids: set[str] = set()
    for section in body["sections"]:
        returned_section_ids.add(section["id"])
        for field in section["settings"]:
            returned_keys.add(field["key"])

    assert returned_keys == set(ALLOWED_KEYS)
    assert returned_section_ids == {s.id for s in SECTIONS}

    # Spot-check coercion of read values.
    agent_field = next(
        f for s in body["sections"] for f in s["settings"] if f["key"] == "AGENT_STRATEGY"
    )
    assert agent_field["value"] == "maf_lite"
    search_field = next(
        f for s in body["sections"] for f in s["settings"] if f["key"] == "SEARCH_RETRIEVAL_ENABLED"
    )
    assert search_field["value"] is False


def test_put_config_rejects_enum_outside_allowlist():
    cfg = _build_cfg({"AGENT_STRATEGY": "single_agent_rag"})
    app = _make_app(cfg)
    client = TestClient(app)
    r = client.put(
        "/api/dashboard/config",
        json={"settings": [{"key": "AGENT_STRATEGY", "value": "not_a_strategy"}]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"][0]["key"] == "AGENT_STRATEGY"
    assert "one of" in detail["errors"][0]["error"].lower()
    cfg.set_value.assert_not_called()


def test_put_config_rejects_numeric_out_of_range():
    cfg = _build_cfg({"CHAT_TEMPERATURE": "0.7"})
    app = _make_app(cfg)
    client = TestClient(app)
    r = client.put(
        "/api/dashboard/config",
        json={"settings": [{"key": "CHAT_TEMPERATURE", "value": 5.0}]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"][0]["key"] == "CHAT_TEMPERATURE"
    assert "<=" in detail["errors"][0]["error"]
    cfg.set_value.assert_not_called()


def test_put_config_rejects_denylist_key():
    """Defense in depth: writes to KEY_VAULT_URI etc. are rejected even if requested."""
    cfg = _build_cfg({})
    app = _make_app(cfg)
    client = TestClient(app)
    r = client.put(
        "/api/dashboard/config",
        json={"settings": [{"key": "MCP_APP_APIKEY", "value": "x"}]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"][0]["key"] == "MCP_APP_APIKEY"
    assert "not allowed" in detail["errors"][0]["error"].lower()
    cfg.set_value.assert_not_called()


def test_put_config_rejects_audit_hmac_key():
    cfg = _build_cfg({})
    app = _make_app(cfg)
    client = TestClient(app)
    response = client.put(
        "/api/dashboard/config",
        json={"settings": [{"key": "AUDIT_HMAC_KEY", "value": "secret"}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["key"] == "AUDIT_HMAC_KEY"
    cfg.set_value.assert_not_called()


def test_put_config_happy_path_writes_and_refreshes_cache():
    cfg = _build_cfg({"AGENT_STRATEGY": "single_agent_rag", "CHAT_TEMPERATURE": "0.7"})
    refreshed_cfg = _build_cfg({"AGENT_STRATEGY": "maf_lite", "CHAT_TEMPERATURE": "1.2"})
    app = _make_app(cfg)

    # The PUT handler calls `get_config("refresh")` after writes; the request
    # path mocks need to return our refreshed instance so we can assert the
    # response reflects post-write state.
    with patch("api.dashboard_config.get_config", return_value=refreshed_cfg) as refresh_call:
        client = TestClient(app)
        r = client.put(
            "/api/dashboard/config",
            json={
                "settings": [
                    {"key": "AGENT_STRATEGY", "value": "maf_lite"},
                    {"key": "CHAT_TEMPERATURE", "value": 1.2},
                ]
            },
        )

    assert r.status_code == 200, r.text
    refresh_call.assert_called_once_with("refresh")
    # Both writes happened against the original cfg with the orchestrator label.
    assert cfg.set_value.call_count == 2
    written = {call.args[0]: call.args[1] for call in cfg.set_value.call_args_list}
    assert written["AGENT_STRATEGY"] == "maf_lite"
    assert written["CHAT_TEMPERATURE"] == 1.2
    for call in cfg.set_value.call_args_list:
        assert call.args[2] == "gpt-rag-orchestrator"

    body = r.json()
    agent_field = next(
        f for s in body["sections"] for f in s["settings"] if f["key"] == "AGENT_STRATEGY"
    )
    assert agent_field["value"] == "maf_lite"


def test_apply_endpoint_refreshes_cache_and_reports_status():
    cfg = _build_cfg({})
    app = _make_app(cfg)
    with patch("api.dashboard_config.get_config", return_value=cfg) as refresh_call:
        client = TestClient(app)
        r = client.post("/api/dashboard/config/apply")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "applied"
    assert "cache" in body["detail"].lower()
    refresh_call.assert_called_once_with("refresh")


# ---------------------------------------------------------------------------
# REASONING_EFFORT round-trip (#241 Bug 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["minimal", "low", "medium", "high"])
def test_reasoning_effort_round_trip_accepts_canonical_lowercase(value):
    """The REASONING_EFFORT validator must accept every wire value the
    Configuration dropdown emits. Bug 2 surfaced because operators saw
    ``Validation failed for REASONING_EFFORT`` when picking ``Low`` — the
    dropdown had been pushing uppercase labels as the wire value while the
    backend (and the downstream Azure OpenAI Responses API ``reasoning.effort``
    parameter) only accepts lowercase.
    """
    cfg = _build_cfg({"REASONING_EFFORT": "medium"})
    refreshed = _build_cfg({"REASONING_EFFORT": value})
    app = _make_app(cfg)
    with patch("api.dashboard_config.get_config", return_value=refreshed):
        client = TestClient(app)
        r = client.put(
            "/api/dashboard/config",
            json={"settings": [{"key": "REASONING_EFFORT", "value": value}]},
        )
    assert r.status_code == 200, r.text
    cfg.set_value.assert_called_once()
    assert cfg.set_value.call_args.args[0] == "REASONING_EFFORT"
    assert cfg.set_value.call_args.args[1] == value


def test_reasoning_effort_rejects_uppercase_label():
    """The historical regression: posting the display label rather than the
    canonical lowercase wire value must produce the per-key 422 error that
    the SPA renders as ``Validation failed for REASONING_EFFORT``."""
    cfg = _build_cfg({"REASONING_EFFORT": "medium"})
    app = _make_app(cfg)
    client = TestClient(app)
    r = client.put(
        "/api/dashboard/config",
        json={"settings": [{"key": "REASONING_EFFORT", "value": "Low"}]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"][0]["key"] == "REASONING_EFFORT"
    cfg.set_value.assert_not_called()
