"""Scoped App Configuration loading, official refresh and immutable run settings."""

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Literal, NoReturn
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

LABEL = "agent-lz"
BASE_KEYS = (
    "ORCHESTRATION_HOST", "APPLICATION_MODE", "AI_FOUNDRY_PROJECT_ENDPOINT",
    "CHAT_DEPLOYMENT_NAME", "CHAT_TEMPERATURE", "CHAT_OUTPUT_TOKENS",
    "SEARCH_RETRIEVAL_ENABLED", "FOUNDRY_IQ_ENABLED", "MCP_ENABLED",
    "MULTIMODAL_ENABLED",
    "AUDIT_EVENTS_ENABLED", "LOG_LEVEL", "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "AZURE_LOG_LEVEL", "AZURE_HTTP_LOG_LEVEL", "AZURE_MONITOR_DISABLE_LOGGING",
    "RETRIEVAL_AUTH_MODE", "ALLOW_ANONYMOUS", "HOSTED_AGENT_AUTH_MODE",
    "AGENT_PRESENTATION_SETTINGS",
)
TOOL_KEYS = {
    "SEARCH_RETRIEVAL_ENABLED": (
        "SEARCH_CONNECTION_ID", "SEARCH_RAG_INDEX_NAME", "SEARCH_RAGINDEX_TOP_K",
    ),
    "FOUNDRY_IQ_ENABLED": ("TOOLBOX_ENDPOINT",),
    "MCP_ENABLED": (
        "MCP_APP_ENDPOINT", "MCP_SERVER_TRANSPORT", "MCP_CLIENT_TIMEOUT",
        "MCP_TRUSTED_HOSTS", "MCP_API_KEY", "MCP_AUTH_RESOURCE_SCOPE",
    ),
    "MULTIMODAL_ENABLED": (
        "SEARCH_SERVICE_QUERY_ENDPOINT", "SEARCH_SERVICE_NAME", "SEARCH_RAG_INDEX_NAME",
        "SEARCH_RAGINDEX_TOP_K", "SEARCH_APPROACH", "SEARCH_API_VERSION",
        "SEARCH_USE_SEMANTIC", "SEARCH_SEMANTIC_SEARCH_CONFIG",
        "AI_FOUNDRY_ACCOUNT_ENDPOINT", "EMBEDDING_DEPLOYMENT_NAME", "OPENAI_API_VERSION",
    ),
    "AUDIT_EVENTS_ENABLED": (
        "AUDIT_HMAC_KEY", "AUDIT_HMAC_KEY_ID", "AUDIT_SENSITIVE_CONTENT_ENABLED",
        "AUDIT_SENSITIVE_CONTENT_FIELDS", "AUDIT_SOURCE_EVENT_LIMIT",
        "AUDIT_ADDITIONAL_REDACTED_KEYS", "AUDIT_ACTOR_PSEUDONYM_ENABLED",
    ),
}
# Secret values are never included in a workflow checkpoint/configuration digest.
SECRET_KEYS = frozenset({"MCP_API_KEY", "AUDIT_HMAC_KEY"})
AUTHORIZATION_KEYS = frozenset({
    "RETRIEVAL_AUTH_MODE", "ALLOW_ANONYMOUS", "HOSTED_AGENT_AUTH_MODE",
    "TOOLBOX_ENDPOINT",
})
_DYNAMIC_MCP_KEYS = frozenset({"MCP_ENABLED", *TOOL_KEYS["MCP_ENABLED"]})
_scoped_config: ContextVar["ConfigView | None"] = ContextVar("native_config", default=None)


def enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() not in ("true", "false"):
        raise ValueError("Feature settings must be true or false.")
    return str(value).strip().lower() == "true"


def https_endpoint(value: str, key: str) -> str:
    endpoint = urlsplit(value)
    if (
        endpoint.scheme != "https" or not endpoint.hostname
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
    ):
        raise ValueError(f"{key} requires an HTTPS endpoint without credentials.")
    return value


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: Literal["hosted", "container_apps"]
    mode: Literal["conversational", "workflow", "both"] = "conversational"
    project_endpoint: str
    model: str
    auth_mode: Literal["user_delegated", "service_identity"] = "user_delegated"
    toolbox_endpoint: str | None = None

    @classmethod
    def read(cls, values: Mapping[str, Any], env: Mapping[str, str] | None = None) -> "RuntimeSettings":
        env = os.environ if env is None else env
        structural: dict[str, Any] = {}
        for key in ("ORCHESTRATION_HOST", "APPLICATION_MODE", "HOSTED_AGENT_AUTH_MODE"):
            configured, deployment = values.get(key), env.get(key)
            hosted_publication_override = (
                key == "APPLICATION_MODE" and configured == "both"
                and deployment in ("conversational", "workflow")
                and structural.get("ORCHESTRATION_HOST") == "hosted"
            )
            if configured and deployment and configured != deployment and not hosted_publication_override:
                raise ValueError(f"Contradictory {key}; update the publication configuration.")
            structural[key] = deployment or configured
        result = cls(
            host=structural["ORCHESTRATION_HOST"],
            mode=structural["APPLICATION_MODE"] or "conversational",
            project_endpoint=https_endpoint(
                str(values.get("AI_FOUNDRY_PROJECT_ENDPOINT") or ""), "AI_FOUNDRY_PROJECT_ENDPOINT"
            ),
            model=str(values.get("CHAT_DEPLOYMENT_NAME") or ""),
            auth_mode=structural["HOSTED_AGENT_AUTH_MODE"] or "user_delegated",
            toolbox_endpoint=values.get("TOOLBOX_ENDPOINT") if enabled(values.get("FOUNDRY_IQ_ENABLED", False)) else None,
        )
        if not result.model:
            raise ValueError("CHAT_DEPLOYMENT_NAME is required.")
        if enabled(values.get("FOUNDRY_IQ_ENABLED", False)) and (
            not result.toolbox_endpoint or not re.fullmatch(
                re.escape(result.project_endpoint.rstrip("/")) + r"/toolboxes/[A-Za-z0-9_-]+/mcp\?api-version=v1",
                result.toolbox_endpoint,
            )
        ):
            raise ValueError("TOOLBOX_ENDPOINT must bind the managed KB toolbox in the configured Foundry project.")
        if result.host == "hosted" and result.mode == "both":
            raise ValueError("Hosted requires one application mode per publication.")
        if enabled(values.get("ALLOW_ANONYMOUS", False)):
            raise ValueError("Native runtime requires authenticated user admission; ALLOW_ANONYMOUS=false.")
        if result.auth_mode == "service_identity":
            if result.host != "hosted":
                raise ValueError("service_identity requires ORCHESTRATION_HOST=hosted.")
        return result


class FrozenValues(dict[str, Any]):
    """JSON-serializable immutable values, including nested functional settings."""
    def _immutable(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("Functional snapshot values are immutable")

    def __setitem__(self, key: str, value: Any) -> NoReturn:
        self._immutable()

    def __delitem__(self, key: str) -> NoReturn:
        self._immutable()

    def clear(self) -> NoReturn:
        self._immutable()

    def pop(self, key: str, default: Any = None) -> NoReturn:
        self._immutable()

    def popitem(self) -> NoReturn:
        self._immutable()

    def setdefault(self, key: str, default: Any = None) -> NoReturn:
        self._immutable()

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._immutable()

    # In-place union is deliberately forbidden; dict's mutable union overloads
    # cannot express this raising operation on an immutable subclass.
    def __ior__(self, other: Any) -> "FrozenValues":  # type: ignore[override,misc]
        self._immutable()

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenValues":
        return self


def freeze_values(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenValues({key: freeze_values(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_values(item) for item in value)
    return value


class FunctionalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str
    values: dict[str, Any]

    @field_validator("values")
    @classmethod
    def immutable_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        return freeze_values(value)

    @classmethod
    def capture(cls, values: Mapping[str, Any]) -> "FunctionalSnapshot":
        safe = {
            key: value for key, value in values.items()
            if key not in SECRET_KEYS | AUTHORIZATION_KEYS | _DYNAMIC_MCP_KEYS
            and key not in ("ORCHESTRATION_HOST", "APPLICATION_MODE")
        }
        payload = json.dumps(safe, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return cls(revision=hashlib.sha256(payload.encode()).hexdigest(), values=json.loads(payload))


class ConfigView:
    """The existing connector getter shape without constructing the classic provider."""

    def __init__(self, values: Mapping[str, Any], credential: Any = None, aiocredential: Any = None):
        self.values = dict(values)
        self.credential = credential
        self.aiocredential = aiocredential

    def get(self, key: str, default: Any = None, type: Any = str) -> Any:
        value = self.values.get(key, default)
        if value is None or type is None:
            return value
        return enabled(value) if type is bool else type(value)

    def get_value(self, key: str, default: Any = None, allow_none: bool = False, type: Any = str) -> Any:
        value = self.get(key, default, type)
        if value is None and not allow_none:
            raise ValueError(f"Missing required configuration: {key}")
        return value


def current_config() -> ConfigView | None:
    return _scoped_config.get()


@contextmanager
def config_scope(config: ConfigView) -> Iterator[None]:
    token = _scoped_config.set(config)
    try:
        yield
    finally:
        _scoped_config.reset(token)


class NativeConfig:
    """No wildcard selectors, legacy labels, global Cosmos, or eager KV secrets."""

    def __init__(self, endpoint: str, credential: Any, sync_credential: Any, loader: Any = None):
        self.endpoint = https_endpoint(endpoint, "APP_CONFIG_ENDPOINT")
        self.credential = credential
        self.sync_credential = sync_credential
        self.loader = loader
        self.providers: list[Any] = []
        self.settings: RuntimeSettings | None = None
        self._lock = threading.Lock()

    async def _load(self, keys: tuple[str, ...], secrets: bool = False) -> Any:
        kwargs: dict[str, Any] = {"keys": keys, "secrets": secrets}
        def load_selected() -> Any:
            return self._load_provider(**kwargs)
        return await asyncio.to_thread(load_selected)

    def _load_provider(self, keys: tuple[str, ...], secrets: bool = False) -> Any:
        from azure.appconfiguration.provider import AzureAppConfigurationKeyVaultOptions, SettingSelector, load

        kwargs: dict[str, Any] = {
            "endpoint": self.endpoint, "credential": self.credential,
            "selects": [SettingSelector(key_filter=k, label_filter=LABEL) for k in keys],
            "refresh_on": [(k, LABEL) for k in keys],
            "refresh_interval": 30,
        }
        if secrets:
            kwargs["key_vault_options"] = AzureAppConfigurationKeyVaultOptions(credential=self.credential)
        # The synchronous provider is intentional: DTS workflow activities may
        # execute on different event loops. Never share an aio transport between them.
        return (self.loader or load)(**kwargs)

    async def open(self) -> "NativeConfig":
        self.providers.append(await self._load(BASE_KEYS))
        values = dict(self.providers[0])
        for flag, keys in TOOL_KEYS.items():
            if flag == "MCP_ENABLED":
                # This capability is read fresh per invocation after parent
                # identity setup, not pinned by startup or the refresh timer.
                continue
            if enabled(values.get(flag, False)):
                self.providers.append(await self._load(keys, bool(SECRET_KEYS.intersection(keys))))
        # Unrelated capability composition remains structural; MCP stays live.
        self._flags = {
            flag: enabled(values.get(flag, False)) for flag in TOOL_KEYS if flag != "MCP_ENABLED"
        }
        self.settings = RuntimeSettings.read({
            key: value for provider in self.providers for key, value in provider.items()
        })
        return self

    async def view(self, snapshot: FunctionalSnapshot | None = None) -> ConfigView:
        return await asyncio.to_thread(self._view, snapshot)

    def _view(self, snapshot: FunctionalSnapshot | None) -> ConfigView:
        with self._lock:
            settings = self.settings
            if settings is None:
                raise ValueError("Native configuration must be opened before reading settings.")
            for provider in self.providers:
                provider.refresh()
            values = {key: value for provider in self.providers for key, value in provider.items()}
            if RuntimeSettings.read(values) != settings:
                raise ValueError("Structural runtime configuration changed; redeploy.")
            values["HOSTED_AGENT_AUTH_MODE"] = settings.auth_mode
            if any(enabled(values.get(k, False)) != v for k, v in self._flags.items()):
                raise ValueError("Enabled capabilities changed; redeploy.")
            mcp_provider = self._load_provider(tuple(
                key for key in ("MCP_ENABLED", *TOOL_KEYS["MCP_ENABLED"],
                                *(("AGENT_PRESENTATION_SETTINGS",) if snapshot is None else ()))
                if key != "MCP_API_KEY"
            ))
            try:
                # Public refresh() is throttled. A selected, request-owned
                # provider guarantees the next invocation reads publication
                # enablement, host list, endpoint and resource scope together.
                values = {
                    **{k: v for k, v in values.items() if k not in _DYNAMIC_MCP_KEYS
                       and (snapshot is not None or k != "AGENT_PRESENTATION_SETTINGS")},
                    **dict(mcp_provider),
                }
            finally:
                mcp_provider.close()
            if enabled(values.get("MCP_ENABLED", False)) and values.get("MCP_AUTH_RESOURCE_SCOPE") is None:
                # Resolve external API-key references only for the active consumer.
                # Bearer mode must not depend on a stale/unavailable API key.
                api_key_provider = self._load_provider(("MCP_API_KEY",), secrets=True)
                try:
                    values.update(dict(api_key_provider))
                finally:
                    api_key_provider.close()
            if snapshot is not None:
                # MCP and authorization stay live; other functional parameters stay with this run.
                values = {
                    **{k: v for k, v in snapshot.values.items() if k not in _DYNAMIC_MCP_KEYS},
                    **{k: v for k, v in values.items() if k in SECRET_KEYS | AUTHORIZATION_KEYS | _DYNAMIC_MCP_KEYS},
                }
            return ConfigView(values, self.sync_credential, self.credential)

    async def close(self) -> None:
        for provider in reversed(self.providers):
            await asyncio.to_thread(provider.close)
