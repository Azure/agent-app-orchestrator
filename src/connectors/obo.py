"""Scope-aware delegated token exchange shared by Search and Foundry IQ."""

import hashlib
import json
import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from collections.abc import Mapping
from collections.abc import Awaitable, Callable
from typing import Any, Optional
from uuid import UUID
from urllib.parse import urlsplit

import aiohttp

from dependencies import get_config


_obo_cache: dict[str, Any] = {}
_MAX_OBO_CACHE_ENTRIES = 256


@dataclass(frozen=True, slots=True)
class DelegatedRetrievalCredentials:
    """Opaque, already-issued credentials. Never serialize or use as API assertions."""

    search_token: str = field(repr=False)
    work_iq_assertion: str | None = field(default=None, repr=False)

    def __reduce__(self) -> Any:
        raise TypeError("Delegated retrieval credentials cannot be persisted")


class HostedRetrievalError(ValueError):
    """Safe HTTP-boundary failure with no credential or provider payload."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


def _config_enabled(cfg: Any, key: str, default: bool = False) -> bool:
    value = cfg.get(key, default, type=bool)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def hosted_search_enabled(cfg: Any) -> bool:
    """Honor intentionally disabled or unconfigured search-only chat deployments."""
    if not _config_enabled(cfg, "SEARCH_RETRIEVAL_ENABLED", True):
        return False
    return bool(
        (cfg.get("SEARCH_SERVICE_QUERY_ENDPOINT") and cfg.get("SEARCH_RAG_INDEX_NAME"))
        or (cfg.get("KNOWLEDGE_BASE_ENDPOINT") and cfg.get("KNOWLEDGE_BASE_NAME"))
    )


def validate_work_iq_configuration(cfg: Any) -> None:
    """Validate published source-auth IDs, never the opaque user assertion.

    The platform owns registration/consent and maps these IDs to the source's
    entraAppAuthentication. The BFF requests api://<application ID>/access_as_user.
    """
    for key in ("WORK_IQ_APPLICATION_ID", "WORK_IQ_TENANT_ID"):
        value = cfg.get(key, "")
        if key == "WORK_IQ_TENANT_ID" and not value:
            continue
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise HostedRetrievalError(f"Invalid or missing {key}", 503) from None
    federation = cfg.get("WORK_IQ_FEDERATED_CREDENTIAL_ID", "")
    if (
        not isinstance(federation, str)
        or not federation
        or len(federation) > 256
        or any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in federation)
    ):
        raise HostedRetrievalError("Invalid or missing WORK_IQ_FEDERATED_CREDENTIAL_ID", 503)


def validate_indexed_sharepoint_configuration(cfg: Any) -> None:
    """Validate secretless ingestion settings; Search's user token owns query ACLs."""
    if cfg.get("FOUNDRY_IQ_API_VERSION", "2026-05-01-preview") != "2026-08-01-preview":
        raise HostedRetrievalError("Protected indexedSharePoint requires 2026-08-01-preview", 503)
    for key in (
        "SHAREPOINT_INDEXED_APPLICATION_ID",
        "SHAREPOINT_INDEXED_FEDERATED_CREDENTIAL_APPLICATION_ID",
        "SHAREPOINT_INDEXED_TENANT_ID",
    ):
        try:
            UUID(cfg.get(key, ""))
        except (ValueError, TypeError, AttributeError):
            raise HostedRetrievalError(f"Invalid or missing {key}", 503) from None
    try:
        raw_site = cfg.get("SHAREPOINT_INDEXED_SITE_URL", "")
        if not isinstance(raw_site, str) or any(char.isspace() for char in raw_site):
            raise ValueError("Invalid site URL")
        site = urlsplit(raw_site)
        valid_site = (
            site.scheme == "https" and bool(site.hostname)
            and site.port in (None, 443)
            and not site.username and not site.password
            and not site.query and not site.fragment
        )
    except ValueError:
        valid_site = False
    if not valid_site:
        raise HostedRetrievalError("Invalid or missing SHAREPOINT_INDEXED_SITE_URL", 503)
    name = cfg.get("SHAREPOINT_INDEXED_KNOWLEDGE_SOURCE_NAME", "") or ""
    alias = cfg.get("SHAREPOINT_INDEXED_KS_NAME", "") or ""
    if (
        not isinstance(name, str) or not isinstance(alias, str)
        or not (name or alias).strip()
        or (name and alias and name.strip() != alias.strip())
    ):
        raise HostedRetrievalError("Missing or conflicting indexedSharePoint knowledge source name", 503)


def resolve_hosted_retrieval(
    cfg: Any, headers: Mapping[str, str],
    conversation_id: str | None = None, *, validate_conversation: bool = True,
) -> DelegatedRetrievalCredentials | None:
    """Validate forwarded credentials before streaming, without decoding tokens.

    The BFF owns acquisition, consent, and lifetime. Foundry transports x-client-*
    headers; these are credentials, not platform-signed principal claims.
    """
    if not hosted_search_enabled(cfg):
        return None
    mode = cfg.get(
        "RETRIEVAL_AUTH_MODE",
        "native_search" if not _config_enabled(cfg, "ALLOW_ANONYMOUS", True) else "",
    )
    supplied = any(key.lower() == "x-client-search-token" for key in headers)
    if not mode and not supplied:
        return None
    if mode not in ("", "native_search"):
        raise HostedRetrievalError("Unsupported hosted retrieval authorization mode", 503)
    if any(
        _config_enabled(cfg, key)
        for key in (
            "WORK_IQ_ENABLED", "FABRIC_IQ_ENABLED", "FABRIC_DATA_AGENT_ENABLED",
            "SHAREPOINT_REMOTE_ENABLED", "SHAREPOINT_INDEXED_ENABLED",
            "ONELAKE_KS_ENABLED", "WEB_GROUNDING_ENABLED",
        )
    ) and str(cfg.get("RETRIEVAL_BACKEND", "ai_search")).strip().lower() != "foundry_iq":
        raise HostedRetrievalError("Configured knowledge sources require foundry_iq retrieval", 503)
    if _config_enabled(cfg, "WORK_IQ_ENABLED"):
        validate_work_iq_configuration(cfg)
    if _config_enabled(cfg, "SHAREPOINT_INDEXED_ENABLED"):
        validate_indexed_sharepoint_configuration(cfg)
    if _config_enabled(cfg, "FOUNDRY_IQ_MCP_ENABLED"):
        raise HostedRetrievalError(
            "Protected MCP sources require a verified source-specific delegation contract", 503
        )

    def read(name: str, required: bool) -> str | None:
        values = [value for key, value in headers.items() if key.lower() == name]
        if not values and not required:
            return None
        if len(values) != 1:
            raise HostedRetrievalError("Required delegated retrieval credential unavailable")
        value = values[0]
        if len(value.encode("utf-8")) > 8192:
            raise HostedRetrievalError("Delegated retrieval credential exceeds header limit", 431)
        if not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise HostedRetrievalError("Malformed delegated retrieval credential")
        return value

    search = read("x-client-search-token", True)
    work_iq = read(
        "x-client-work-iq-assertion", _config_enabled(cfg, "WORK_IQ_ENABLED")
    )
    kind = cfg.get("FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND") or cfg.get("FOUNDRY_IQ_PATTERN")
    upload_index = (
        _config_enabled(cfg, "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED")
        or cfg.get("RETRIEVAL_BACKEND", "ai_search") == "ai_search"
        or kind == "searchIndex"
        or (not kind and _config_enabled(cfg, "FOUNDRY_IQ_FILTER_ADD_ON_ENABLED"))
    )
    if validate_conversation and upload_index and (
        not isinstance(conversation_id, str)
        or not conversation_id.strip() or conversation_id.strip() == "NaN"
    ):
        raise HostedRetrievalError("Protected upload retrieval requires a conversation ID", 422)
    assert search is not None
    return DelegatedRetrievalCredentials(search, work_iq)


class RetrievalAuthorizationMode(str, Enum):
    """Internal request policy; service identity never substitutes for a user."""

    USER_REQUIRED = "user_required"
    SERVICE_ONLY = "service_only"


def resolve_retrieval_authorization(
    assertion: Optional[str], allow_anonymous: bool, source_requires_user: bool = False,
) -> RetrievalAuthorizationMode:
    if not isinstance(allow_anonymous, bool) or not isinstance(source_requires_user, bool):
        raise ValueError("Invalid retrieval authorization policy")
    if assertion is not None and not isinstance(assertion, str):
        raise ValueError("Invalid retrieval assertion")
    if (assertion and assertion.strip()) or not allow_anonymous or source_requires_user:
        return RetrievalAuthorizationMode.USER_REQUIRED
    return RetrievalAuthorizationMode.SERVICE_ONLY


async def require_retrieval_token(
    mode: RetrievalAuthorizationMode,
    callback: Optional[Callable[[], Awaitable[Optional[str]]]],
) -> Optional[str]:
    """Preflight before document access; errors and cancellation propagate."""
    if not isinstance(mode, RetrievalAuthorizationMode):
        raise ValueError("Invalid retrieval authorization mode")
    logging.info("retrieval_authorization_mode=%s", mode.value)
    if mode is RetrievalAuthorizationMode.SERVICE_ONLY:
        return None
    token = await callback() if callback is not None else None
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Required retrieval authorization unavailable")
    return token


def classify_retrieval_error(error: Any) -> tuple[int, str]:
    """Return the existing safe auth/error log level and marker."""
    msg = str(error) if error is not None else ""
    if "401" in msg or "403" in msg:
        return logging.ERROR, "[Retrieval][AUTH_FAILURE]"
    return logging.WARNING, "[Retrieval][ERROR]"


async def acquire_obo_token(
    api_access_token: Optional[str],
    scope: str,
    allow_anonymous: bool = False,
) -> Optional[str]:
    """Acquire a delegated token; preserve strict and anonymous caller semantics."""
    scope = (scope or "").strip()
    if not scope:
        raise ValueError("OBO scope must not be empty")
    if not api_access_token:
        if allow_anonymous:
            return None
        raise RuntimeError("Missing incoming user access token for OBO exchange")

    fp = hashlib.sha256(api_access_token.encode()).hexdigest()
    cache_key = f"{fp}:{scope}"
    cached = _obo_cache.get(cache_key)
    if cached and time.time() < cached.get("expires_at", 0):
        return cached["token"]

    cfg = get_config()
    tenant_id = (cfg.get_value("OAUTH_AZURE_AD_TENANT_ID", allow_none=True) or "").strip() or None
    client_id = (cfg.get_value("OAUTH_AZURE_AD_CLIENT_ID", allow_none=True) or "").strip() or None
    client_secret = (cfg.get_value("OAUTH_AZURE_AD_CLIENT_SECRET", allow_none=True) or "").strip() or None

    if not tenant_id or not client_id or not client_secret:
        logging.warning("[OBO] Missing Entra config for OBO (tenant=%s client=%s secret=%s)",
                        "set" if tenant_id else "missing", "set" if client_id else "missing", "set" if client_secret else "missing")
        if allow_anonymous:
            return None
        raise RuntimeError("OBO configuration is incomplete")

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "requested_token_use": "on_behalf_of",
        "scope": scope,
        "assertion": api_access_token,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=form) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                level, marker = classify_retrieval_error(resp.status)
                logging.log(
                    level,
                    "%s OBO token exchange failed (status=%d scope_fp=%s)",
                    marker,
                    resp.status,
                    hashlib.sha256(scope.encode()).hexdigest()[:12],
                    extra={
                        "retrieval_status": resp.status,
                        "retrieval_credential_type": "obo",
                    },
                )
                if allow_anonymous:
                    return None
                raise RuntimeError(f"OBO token exchange failed: status={resp.status}")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                logging.error("[OBO] Non-JSON response from token endpoint")
                if allow_anonymous:
                    return None
                raise RuntimeError(
                    "OBO token endpoint returned an invalid response"
                ) from exc
            token = data.get("access_token")
            if token:
                ttl = int(data.get("expires_in", 0))
                now = time.time()
                expired_keys = [
                    key
                    for key, entry in _obo_cache.items()
                    if now >= entry.get("expires_at", 0)
                ]
                for key in expired_keys:
                    _obo_cache.pop(key, None)
                while len(_obo_cache) >= _MAX_OBO_CACHE_ENTRIES:
                    _obo_cache.pop(next(iter(_obo_cache)))
                _obo_cache[cache_key] = {
                    "token": token,
                    "expires_at": now + max(0, ttl - 30),
                }
                logging.info("[OBO] Acquired delegated token")
                return token
            if allow_anonymous:
                return None
            raise RuntimeError("OBO token endpoint response missing access_token")
