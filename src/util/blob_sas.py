"""Signing helper for blob citation links.

Foundry IQ's ``azureBlob`` base corpus emits citation hrefs as fully-qualified
blob URLs. They point at our own storage account but carry no SAS token, so any
client that follows one hits the raw blob and fails with
``PublicAccessNotPermitted`` whenever public blob access is disabled — which is
the secure default and is commonly enforced by tenant Azure Policy.

The Chainlit frontend solves this at render time (Azure/agent-app-ui#79), but that
hook only exists for the frontend. Surfaces that consume the hosted agent
directly — the Foundry portal Playground, "Call agent", or any custom client —
render the model's markdown verbatim and have nowhere to sign the href. Signing
the link before it reaches the prompt makes citations resolvable on every
surface.

Signing is best effort: every failure path returns the original URL so a
citation is never dropped from an otherwise good answer.

Introduced for Azure/agent-landing-zone#660.
"""

import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# How long a citation link stays valid. Matches the frontend's default so a
# link behaves the same regardless of which surface produced it.
DEFAULT_EXPIRY_HOURS = 1

# User delegation keys are reusable across signatures, and requesting one costs
# a round trip to storage. Cache a single key and renew it slightly before it
# expires so a long-lived process does not sign with a key that dies mid-flight.
_KEY_LIFETIME_HOURS = 4
_KEY_RENEWAL_MARGIN = timedelta(minutes=10)

_cached_key = None
_cached_key_expiry: Optional[datetime] = None
_cached_key_account: Optional[str] = None
_key_lock = asyncio.Lock()


def _storage_account_name() -> str:
    """Return the configured storage account name, or an empty string."""
    try:
        from dependencies import get_config

        return (get_config().get("STORAGE_ACCOUNT_NAME", "") or "").strip()
    except Exception:
        logger.debug("Storage account name unavailable; blob links stay unsigned")
        return ""


def _blob_host(account: str) -> str:
    return f"{account}.blob.core.windows.net".lower()


def _split_container_and_blob(path: str) -> tuple[str, str]:
    """Split a blob URL path into its container and blob name."""
    decoded = urllib.parse.unquote(path.replace("\\", "/")).lstrip("/")
    container, _, blob = decoded.partition("/")
    return container, blob


async def _get_user_delegation_key(account: str):
    """Return a cached user delegation key, requesting a new one when stale."""
    global _cached_key, _cached_key_expiry, _cached_key_account

    now = datetime.now(timezone.utc)
    async with _key_lock:
        still_valid = (
            _cached_key is not None
            and _cached_key_account == account
            and _cached_key_expiry is not None
            and now < _cached_key_expiry - _KEY_RENEWAL_MARGIN
        )
        if still_valid:
            return _cached_key

        from azure.storage.blob.aio import BlobServiceClient

        from connectors.identity_manager import get_identity_manager

        credential = get_identity_manager().get_aio_credential()
        expiry = now + timedelta(hours=_KEY_LIFETIME_HOURS)
        async with BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=credential,
        ) as service:
            key = await service.get_user_delegation_key(
                key_start_time=now - timedelta(minutes=5),
                key_expiry_time=expiry,
            )

        _cached_key = key
        _cached_key_expiry = expiry
        _cached_key_account = account
        return key


async def sign_blob_url(url: str, expiry_hours: int = DEFAULT_EXPIRY_HOURS) -> str:
    """Append a read-only user delegation SAS to one of our own blob URLs.

    The input is returned unchanged when it is not an absolute URL, does not
    belong to the configured storage account, already carries a SAS token, or
    when signing fails for any reason. Callers can therefore treat this as a
    transparent enrichment step.
    """
    href = (url or "").strip()
    if not href:
        return url

    try:
        split = urllib.parse.urlsplit(href)
    except ValueError:
        logger.warning("Malformed citation link; leaving it unsigned")
        return url
    if not split.scheme or not split.netloc:
        # Relative reference: the consuming surface resolves it against its own
        # origin, so there is nothing for us to sign here.
        return url

    account = _storage_account_name()
    if not account or split.netloc.lower() != _blob_host(account):
        # External link, or a blob in an account we hold no delegation over.
        return url

    if "sig=" in (split.query or "").lower():
        return url

    container, blob = _split_container_and_blob(split.path)
    if not container or not blob:
        return url

    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        key = await _get_user_delegation_key(account)
        token = generate_blob_sas(
            account_name=account,
            container_name=container,
            blob_name=blob,
            user_delegation_key=key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        )
    except Exception:
        logger.warning(
            "Could not sign citation link; leaving it unsigned",
        )
        return url

    signed = urllib.parse.urlunsplit(
        (split.scheme, split.netloc, split.path, token, "")
    )
    if split.fragment:
        return f"{signed}#{split.fragment}"
    return signed


def reset_delegation_key_cache() -> None:
    """Drop the cached delegation key. Intended for tests."""
    global _cached_key, _cached_key_expiry, _cached_key_account
    _cached_key = None
    _cached_key_expiry = None
    _cached_key_account = None
