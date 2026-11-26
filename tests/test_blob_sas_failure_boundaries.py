"""Citation signing remains optional without leaking dependency diagnostics."""

import asyncio
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from azure.core.exceptions import AzureError

from util import blob_sas


ACCOUNT = "exampleaccount"
URL = f"https://{ACCOUNT}.blob.core.windows.net/documents/private-document.txt"
CANARY = "synthetic-sensitive-signing-error"


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    blob_sas.reset_delegation_key_cache()
    monkeypatch.setattr(blob_sas, "_key_lock", asyncio.Lock())
    yield
    blob_sas.reset_delegation_key_cache()


@pytest.mark.parametrize("stage", ["config", "account"])
def test_unavailable_configuration_preserves_unsigned_citation(monkeypatch, caplog, stage):
    caplog.set_level(logging.DEBUG)
    config = types.SimpleNamespace(get=lambda *args: (_ for _ in ()).throw(RuntimeError(CANARY)))
    dependencies = types.ModuleType("dependencies")
    dependencies.get_config = (
        (lambda: (_ for _ in ()).throw(RuntimeError(CANARY)))
        if stage == "config" else lambda: config
    )
    monkeypatch.setitem(sys.modules, "dependencies", dependencies)
    assert asyncio.run(blob_sas.sign_blob_url(URL)) == URL
    assert "unavailable" in caplog.text
    assert CANARY not in caplog.text


@pytest.mark.parametrize("stage", ["acquire", "sign", "cleanup"])
@pytest.mark.parametrize("failure_type", [AzureError, RuntimeError])
def test_optional_signing_failure_is_unsigned_safe_and_closes_client(
    monkeypatch, caplog, stage, failure_type,
):
    failure = failure_type(CANARY)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_user_delegation_key.return_value = object()
    if stage == "acquire":
        client.get_user_delegation_key.side_effect = failure
    elif stage == "cleanup":
        client.__aexit__.side_effect = failure
    monkeypatch.setattr(blob_sas, "_storage_account_name", lambda: ACCOUNT)
    monkeypatch.setattr("azure.storage.blob.aio.BlobServiceClient", lambda **kwargs: client)
    monkeypatch.setattr(
        "connectors.identity_manager.get_identity_manager",
        lambda: types.SimpleNamespace(get_aio_credential=object),
    )

    def generate(**kwargs):
        if stage == "sign":
            raise failure
        return "sig=synthetic"

    monkeypatch.setattr("azure.storage.blob.generate_blob_sas", generate)
    assert asyncio.run(blob_sas.sign_blob_url(URL)) == URL
    client.__aexit__.assert_awaited_once()
    assert CANARY not in caplog.text
    assert "private-document.txt" not in caplog.text
    assert "unsigned" in caplog.text
    if stage != "sign":
        assert blob_sas._cached_key is None


@pytest.mark.parametrize("stage", ["acquire", "cleanup"])
def test_cancellation_propagates_and_does_not_cache_unconfirmed_key(monkeypatch, stage):
    failure = asyncio.CancelledError(CANARY)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_user_delegation_key.return_value = object()
    if stage == "acquire":
        client.get_user_delegation_key.side_effect = failure
    else:
        client.__aexit__.side_effect = failure
    monkeypatch.setattr(blob_sas, "_storage_account_name", lambda: ACCOUNT)
    monkeypatch.setattr("azure.storage.blob.aio.BlobServiceClient", lambda **kwargs: client)
    monkeypatch.setattr(
        "connectors.identity_manager.get_identity_manager",
        lambda: types.SimpleNamespace(get_aio_credential=object),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(blob_sas.sign_blob_url(URL))
    assert caught.value is failure
    client.__aexit__.assert_awaited_once()
    assert blob_sas._cached_key is None


def test_malformed_url_is_not_fatal_or_logged_verbatim(monkeypatch, caplog):
    monkeypatch.setattr(blob_sas, "_storage_account_name", lambda: ACCOUNT)
    url = f"https://[invalid-{CANARY}/documents/file"
    assert asyncio.run(blob_sas.sign_blob_url(url)) == url
    assert CANARY not in caplog.text


@pytest.mark.parametrize("expired", [False, True])
def test_cache_never_reuses_another_account_or_near_expiry_key(monkeypatch, expired):
    blob_sas._cached_key = "old-key"
    blob_sas._cached_key_account = ACCOUNT if expired else "otheraccount"
    blob_sas._cached_key_expiry = datetime.now(timezone.utc) + timedelta(minutes=5 if expired else 60)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_user_delegation_key.return_value = "new-key"
    monkeypatch.setattr("azure.storage.blob.aio.BlobServiceClient", lambda **kwargs: client)
    monkeypatch.setattr(
        "connectors.identity_manager.get_identity_manager",
        lambda: types.SimpleNamespace(get_aio_credential=object),
    )
    assert asyncio.run(blob_sas._get_user_delegation_key(ACCOUNT)) == "new-key"
    client.get_user_delegation_key.assert_awaited_once()
    assert blob_sas._cached_key_account == ACCOUNT
