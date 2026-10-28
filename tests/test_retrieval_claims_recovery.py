"""Configured Search authentication challenges, without live Azure."""

import base64
import json
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agent_framework import ChatMessage, Role
from azure.core.exceptions import HttpResponseError

from connectors.obo import HostedRetrievalError
from orchestration.turn import TurnErrorEvent
from strategies import search_context_provider as search_provider


CLAIMS = '{"access_token":{"acrs":{"essential":true,"value":"synthetic-mfa"}}}'
ENCODED = base64.b64encode(CLAIMS.encode()).decode()
HEADER = f'Bearer error="insufficient_claims", claims="{ENCODED}"'


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("claims", [ENCODED, json.dumps(CLAIMS)[1:-1]])
def test_source_challenge_is_bounded_canonical_and_not_an_authority_hint(status, claims):
    error = HostedRetrievalError.from_response(status, [(
        "WWW-Authenticate",
        f'Bearer authorization_uri="https://untrusted.invalid", claims="{claims}"',
    )])
    assert error.code == "interaction_required"
    assert error.claims_challenge == CLAIMS
    assert "untrusted.invalid" not in str(error)
    assert "synthetic-mfa" not in repr(error)
    event = TurnErrorEvent(message=str(error), code=error.code, claims_challenge=CLAIMS)
    assert "synthetic-mfa" not in repr(event)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("headers", [
    [],
    [("WWW-Authenticate", HEADER), ("WWW-Authenticate", HEADER)],
    [("WWW-Authenticate", HEADER + f', claims="{ENCODED}"')],
    [("WWW-Authenticate", HEADER + ', CLAIMS="duplicate"')],
    [("WWW-Authenticate", HEADER + ', Basic realm="another-scheme"')],
    [("WWW-Authenticate", HEADER + "\r\ninjected: value")],
    [("WWW-Authenticate", 'Bearer claims="' + "x" * 17000 + '"')],
    [("WWW-Authenticate", 'Basic claims="' + ENCODED + '"')],
    [("WWW-Authenticate", 'Bearer claims="not-base64-or-json"')],
    [("WWW-Authenticate", 'Bearer claims="' + base64.b64encode(b'{"id_token":{"x":1}}').decode() + '"')],
    [("WWW-Authenticate", 'Bearer claims="' + base64.b64encode(
        b'{"access_token":{"x":1},"access_token":{"x":2}}'
    ).decode() + '"')],
    [("WWW-Authenticate", 'Bearer claims="' + base64.b64encode(
        b'{"access_token":{"x":"' + b"x" * 8200 + b'"}}'
    ).decode() + '"')],
])
def test_ambiguous_malformed_or_unsupported_claims_do_not_start_claims_recovery(status, headers):
    error = HostedRetrievalError.from_response(status, headers)
    assert error.claims_challenge is None
    assert error.code == ("source_denied" if status == 403 else "authentication_required")
    assert "claims" not in str(error).lower()


@pytest.mark.parametrize("status", [200, 206, 400, 404, 429, 500])
def test_only_authentication_statuses_are_challenges(status):
    assert HostedRetrievalError.from_response(status, [("WWW-Authenticate", HEADER)]) is None


@pytest.mark.parametrize("status,challenge", [(401, True), (403, True), (403, False)])
async def test_real_search_sdk_error_preserves_only_safe_authentication_metadata(
    mock_config, status, challenge, caplog,
):
    response = SimpleNamespace(
        status_code=status, reason="forbidden",
        headers={"WWW-Authenticate": HEADER} if challenge else {},
    )
    failure = HttpResponseError(message="synthetic-sensitive-provider-body", response=response)
    boundary = AsyncMock()
    boundary.__aenter__.return_value.search.side_effect = failure
    token = "synthetic-sensitive-user-token"
    provider = search_provider.SearchContextProvider(
        endpoint="https://search.invalid", index_name="documents", credential=None,
        conversation_id="synthetic-conversation",
        get_obo_token=AsyncMock(return_value=token), native_permissions=True,
    )
    with (
        patch.object(search_provider, "SearchClient", return_value=boundary),
        patch.object(search_provider, "get_config", return_value=mock_config),
        pytest.raises(HostedRetrievalError) as raised,
    ):
        await provider.invoking(ChatMessage(role=Role.USER, text="question"))
    assert raised.value.code == ("interaction_required" if challenge else "source_denied")
    assert raised.value.claims_challenge == (CLAIMS if challenge else None)
    boundary.__aenter__.return_value.search.assert_awaited_once()
    safe = "".join(traceback.format_exception(raised.value)) + caplog.text
    assert token not in safe and "synthetic-sensitive-provider-body" not in safe
    assert "synthetic-mfa" not in safe
