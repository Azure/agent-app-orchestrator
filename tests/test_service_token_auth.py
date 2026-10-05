"""Tests for keyless Entra service-to-service auth (ADR-0019)."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

import dependencies
from dependencies import validate_auth

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "api://agent-lz-orchestrator"
CALLER = "22222222-2222-2222-2222-222222222222"
KID = "test-kid"


def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_SIGNING_KEY = _keypair()
_OTHER_KEY = _keypair()


def _jwk(private_key, kid=KID):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    return jwk


def _token(key=_SIGNING_KEY, kid=KID, **overrides):
    now = int(time.time())
    claims = {
        "iss": f"https://sts.windows.net/{TENANT}/",
        "aud": AUDIENCE,
        "oid": CALLER,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class _NoConfig:
    def get_value(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("DISABLE_AUTH", "APP_API_TOKEN", "DAPR_API_TOKEN", "OAUTH_AZURE_AD_TENANT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)
    monkeypatch.setenv("ORCHESTRATOR_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("ORCHESTRATOR_ALLOWED_CALLER_IDS", CALLER)
    monkeypatch.setattr(dependencies, "get_config", lambda *a, **k: _NoConfig())

    async def _keys(tenant_id, jwks_url=None):
        return {"keys": [_jwk(_SIGNING_KEY)]}

    monkeypatch.setattr(dependencies, "_get_cached_public_keys", _keys)
    monkeypatch.setattr(dependencies, "_force_refresh_jwks_cache", lambda *a, **k: None)


async def _call(header):
    return await validate_auth(dapr_api_token=None, x_api_key=None, x_service_authorization=header)


async def test_valid_service_token_accepted():
    assert await _call(f"Bearer {_token()}") is True


async def test_audience_without_api_prefix_accepted():
    assert await _call(f"Bearer {_token(aud='agent-lz-orchestrator')}") is True


async def test_v2_issuer_and_azp_accepted():
    token = _token(iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", oid="other", azp=CALLER)
    assert await _call(f"Bearer {token}") is True


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda: _token(aud="api://someone-else"),
        lambda: _token(exp=int(time.time()) - 60),
        lambda: _token(iss="https://sts.windows.net/another-tenant/"),
        lambda: _token(key=_OTHER_KEY),
        lambda: _token(kid="unknown-kid"),
        lambda: "not-a-jwt",
    ],
    ids=["wrong-audience", "expired", "wrong-issuer", "bad-signature", "unknown-kid", "malformed"],
)
async def test_invalid_service_token_rejected(token_factory):
    with pytest.raises(HTTPException) as exc:
        await _call(f"Bearer {token_factory()}")
    assert exc.value.status_code == 401


async def test_caller_not_allowed_rejected():
    with pytest.raises(HTTPException) as exc:
        await _call(f"Bearer {_token(oid='33333333-3333-3333-3333-333333333333')}")
    assert exc.value.status_code == 401
    assert exc.value.detail == "Caller not allowed"


@pytest.mark.parametrize("missing", ["AZURE_TENANT_ID", "ORCHESTRATOR_AUTH_AUDIENCE", "ORCHESTRATOR_ALLOWED_CALLER_IDS"])
async def test_not_configured_rejected(monkeypatch, missing):
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(HTTPException) as exc:
        await _call(f"Bearer {_token()}")
    assert exc.value.status_code == 401
    assert exc.value.detail == "Service authentication not configured"


async def test_service_token_takes_precedence_over_api_key(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_APP_APIKEY", "secret")
    with pytest.raises(HTTPException):
        await validate_auth(dapr_api_token=None, x_api_key="secret", x_service_authorization="Bearer bad")


async def test_api_key_fallback_still_works(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_APP_APIKEY", "secret")
    assert await validate_auth(dapr_api_token=None, x_api_key="secret", x_service_authorization=None) is True
