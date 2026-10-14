import os
import logging
import socket
import ssl
from builtins import type as ValueType
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from azure.core.credentials import TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import RequestsTransport
from azure.appconfiguration import (
    AzureAppConfigurationClient,
    ConfigurationSetting,
)
from azure.appconfiguration.provider import (
    AzureAppConfigurationKeyVaultOptions,
    AzureAppConfigurationProvider,
    load,
    SettingSelector,
)
from connectors.identity_manager import get_identity_manager

from tenacity import retry, wait_random_exponential, stop_after_attempt, RetryError, RetryCallState


def _tls_context(cafile: str | None = None) -> ssl.SSLContext:
    context = (
        ssl.create_default_context(cafile=cafile)
        if cafile is not None
        else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ecdh_curve("prime256v1")
    return context


class _TLSCompatibilityAdapter(HTTPAdapter):
    def __init__(self) -> None:
        self.tls_context = _tls_context(requests.certs.where())
        super().__init__()

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **kwargs: Any
    ) -> None:
        kwargs["socket_options"] = [
            *HTTPConnection.default_socket_options,
            (socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1200),
        ]
        super().init_poolmanager(connections, maxsize, block, **kwargs)

    def build_connection_pool_key_attributes(
        self,
        request: requests.PreparedRequest,
        verify: bool | str,
        cert: str | tuple[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if verify is False:
            raise ValueError("Configuration transport requires certificate verification.")
        host, options = super().build_connection_pool_key_attributes(request, verify, cert)
        # A custom CA must not inherit the default bundle or another caller's roots.
        options["ssl_context"] = _tls_context() if isinstance(verify, str) else self.tls_context
        options["socket_options"] = self.poolmanager.connection_pool_kw["socket_options"]
        return host, options

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        try:
            return super().send(
                request, stream=stream, timeout=timeout, verify=verify, cert=cert,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            logging.getLogger(__name__).error(
                "Configuration TLS compatibility request failed: error_type=%s",
                ValueType(exc).__name__,
            )
            raise


def _create_appconfig_transport(endpoint: str) -> RequestsTransport:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Configuration transport requires a plain HTTPS endpoint.")
    session = requests.Session()
    session.mount(f"https://{parsed.netloc}/", _TLSCompatibilityAdapter())
    return RequestsTransport(session=session, session_owner=True)


def _with_legacy_app_label(
    extra_selector: SettingSelector,
    *,
    transport: RequestsTransport | None = None,
    key_vault_client_configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> Callable[..., AzureAppConfigurationProvider]:
    """Adapt provider selectors and optional bootstrap transport without replacing credentials."""
    def _load(
        *args: Any, selects: Sequence[SettingSelector], **kwargs: Any
    ) -> AzureAppConfigurationProvider:
        merged = list(selects)
        merged.insert(min(2, len(merged)), extra_selector)
        if transport is not None:
            kwargs["transport"] = transport
        if key_vault_client_configs is not None:
            kwargs["key_vault_options"].client_configs = key_vault_client_configs
        return load(*args, selects=merged, **kwargs)
    return _load

class AppConfigClient:

    credential: TokenCredential | None = None
    aiocredential: AsyncTokenCredential | None = None

    def __init__(self) -> None:
        """
        Bulk-loads all keys into an in-memory dict from the most common labels used by Agent Landing Zone:
        - 'orchestrator' (legacy / shared deployments)
        - 'agent-app-orchestrator' (service-specific)
        - 'gpt-rag-orchestrator' (legacy service-specific)
        - 'agent-lz' (base / shared)

        Precedence is determined by the order of selectors (earlier wins for duplicate keys).
        """
        # Defaults
        self.disabled = False
        self.auth_failed = False
        self.client: Mapping[str, Any] = {}

        # ==== Load all config parameters in one place ====
        # NOTE: Do not default to "*" for client_id.
        # Passing an invalid client_id to ManagedIdentityCredential can break auth in Container Apps.
        self.tenant_id = os.environ.get('AZURE_TENANT_ID')
        self.client_id = os.environ.get('AZURE_CLIENT_ID')

        self.allow_env_vars = False
        if "allow_environment_variables" in os.environ:
            # Parse string -> bool safely ("false" -> False)
            self.allow_env_vars = str(os.environ["allow_environment_variables"]).lower() in ("1", "true", "yes")

        endpoint = os.getenv("APP_CONFIG_ENDPOINT")

        # If there's no endpoint configured, skip remote config entirely.
        if not endpoint:
            logging.info("Azure App Configuration skipped: no APP_CONFIG_ENDPOINT set. Remote keys will not be fetched.")
            self.disabled = True
            return

        # Safe endpoint info for troubleshooting
        _endpoint_host = endpoint.replace("https://", "").replace("http://", "").split("/")[0]

        # Prepare credentials for endpoint-based access
        identity_manager = get_identity_manager()
        self.credential = identity_manager.get_credential()
        self.aiocredential = identity_manager.get_aio_credential()

        # Prefer more specific labels first.
        loaded_labels = list(LOADED_LABELS)
        legacy_orchestrator_label_selector = SettingSelector(label_filter='orchestrator', key_filter='*')
        orchestrator_label_selector = SettingSelector(label_filter=APP_LABEL, key_filter='*')
        legacy_app_label_selector = SettingSelector(label_filter=LEGACY_APP_LABEL, key_filter='*')
        base_label_selector = SettingSelector(label_filter=AGENTLZ_LABEL, key_filter='*')
        no_label_selector = SettingSelector(label_filter="\0", key_filter='*')

        tls_compatibility = os.getenv("APP_CONFIG_TLS_COMPATIBILITY", "false").strip().lower()
        if tls_compatibility not in ("true", "false"):
            raise ValueError("APP_CONFIG_TLS_COMPATIBILITY must be true or false.")
        transport = None
        key_vault_client_configs = None
        if tls_compatibility == "true":
            vault_endpoint = os.getenv("KEY_VAULT_URI", "").strip()
            if not vault_endpoint:
                raise ValueError("APP_CONFIG_TLS_COMPATIBILITY requires KEY_VAULT_URI.")
            vault_transport = _create_appconfig_transport(vault_endpoint)
            transport = _create_appconfig_transport(endpoint)
            key_vault_client_configs = {
                vault_endpoint.rstrip("/") + "/": {"transport": vault_transport}
            }
            logging.info(
                "Configuration TLS compatibility is enabled for App Configuration "
                "and the configured Key Vault; certificate verification remains enabled."
            )
        load = _with_legacy_app_label(
            legacy_app_label_selector, transport=transport,
            key_vault_client_configs=key_vault_client_configs,
        )

        logging.info(
            "Azure App Configuration init: endpoint_host=%s identity=%s allow_env_vars=%s labels=%s",
            _endpoint_host,
            "singleton",
            self.allow_env_vars,
            ",".join(loaded_labels),
        )

        # Try to load from Azure App Configuration. If auth fails, don't spam stack traces.
        try:
            self.client = load(
                selects=[legacy_orchestrator_label_selector, orchestrator_label_selector, base_label_selector, no_label_selector],
                endpoint=endpoint,
                credential=self.credential,
                key_vault_options=AzureAppConfigurationKeyVaultOptions(credential=self.credential)
            )
            logging.info(
                "Azure App Configuration loaded successfully (keys_loaded=%d).",
                len(self.client or {}),
            )
        except ClientAuthenticationError:
            # Not logged in / MSI unavailable -> disable remote config, log a friendly message.
            logging.warning(
                "Azure App Configuration disabled: not authenticated (run 'az login' or configure Managed Identity). "
                "Skipping remote configuration and not fetching any keys."
            )
            self.disabled = True
            self.auth_failed = True
            self.client = {}
        except Exception as e:
            # Any other error: disable and keep running without remote config.
            logging.warning(
                "Azure App Configuration unavailable (%s). Skipping remote configuration and not fetching any keys.",
                type(e).__name__
            )
            self.disabled = True
            self.client = {}


    def get(self, key: str, default: Any = None, type: ValueType[Any] | None = str) -> Any:
        return self.get_value(key, default=default, allow_none=False, type=type)

    def get_value(self, key: str, default: Any = None, allow_none: bool = False, type: ValueType[Any] | None = str) -> Any:

        if key is None:
            raise Exception('The key parameter is required for get_value().')

        value: Any = None

        allow_env_vars = False
        if "allow_environment_variables" in os.environ:
            allow_env_vars = str(os.environ["allow_environment_variables"]).lower() in ("1", "true", "yes")

        if allow_env_vars is True:
            for candidate in candidate_keys(key):
                value = os.environ.get(candidate)
                if value is not None:
                    break

        if value is None and not self.disabled:
            for candidate in candidate_keys(key):
                try:
                    value = self.get_config_with_retry(name=candidate)
                except RetryError:
                    value = None
                if value is not None:
                    break

        if value is not None:
            if type is not None:
                if type is bool:
                    if isinstance(value, str):
                        value = value.lower() in ['true', '1', 'yes']
                else:
                    try:
                        value = type(value)
                    except ValueError as e:
                        raise Exception(f'Value for {key} could not be converted to {type.__name__}. Error: {e}')
            return value
        else:
            if default is not None or allow_none is True:
                return default

            raise Exception(f'The configuration variable {key} not found.')

    @staticmethod
    def retry_before_sleep(retry_state: RetryCallState) -> None:
        # Log the outcome of each retry attempt.
        message = f"""Retrying {retry_state.fn}:
                        attempt {retry_state.attempt_number}
                        ended with: {retry_state.outcome}"""
        if retry_state.outcome is not None and retry_state.outcome.failed:
            ex = retry_state.outcome.exception()
            message += f"; Exception: {ex.__class__.__name__}: {ex}"
        if retry_state.attempt_number < 1:
            logging.info(message)
        else:
            logging.warning(message)

    @retry(
        wait=wait_random_exponential(multiplier=1, max=5),
        stop=stop_after_attempt(5),
        before_sleep=retry_before_sleep
    )
    def get_config_with_retry(self, name: str) -> Any:
        if self.disabled or not self.client:
            return None
        try:
            return self.client[name]
        except KeyError:
            return None

    # Helper functions for reading environment variables
    def read_env_variable(self, var_name: str, default: str | None = None) -> str | None:
        value = self.get_value(var_name, default)
        return value.strip() if value else default

    def read_env_list(self, var_name: str) -> list[str]:
        value = self.get_value(var_name, "")
        return [item.strip() for item in value.split(",") if item.strip()]

    def read_env_boolean(self, var_name: str, default: bool = False) -> bool:
        value = self.get_value(var_name, str(default)).strip().lower()
        return value in ['true', '1', 'yes']

    # -----------------------------------------------------------------
    # Write support (used by the admin dashboard Configuration tab)
    # -----------------------------------------------------------------

    def set_value(self, key: str, value: Any, label: str = "agent-app-orchestrator") -> None:
        """Write a single key/value pair back to Azure App Configuration.

        The read-side of this class uses the provider's bulk ``load`` helper,
        which is read-only. For writes we instantiate the raw management
        client per call — that's cheap and avoids holding extra long-lived
        connections.

        Values are persisted as strings (App Configuration is a string store).
        Booleans are normalised to ``"true"`` / ``"false"`` so the same
        precedence rules used by :meth:`get_value` apply on read back.

        Raises:
            RuntimeError: when App Configuration is disabled (no endpoint).
            Exception: re-raises any underlying Azure SDK error so the caller
                can surface it with a clear HTTP response.
        """
        if self.disabled:
            raise RuntimeError(
                "Azure App Configuration is disabled or not configured; "
                "cannot persist setting."
            )

        endpoint = os.getenv("APP_CONFIG_ENDPOINT")
        if not endpoint:
            raise RuntimeError("APP_CONFIG_ENDPOINT is not set.")

        if self.credential is None:
            raise RuntimeError("Azure App Configuration credentials are not initialized.")

        if isinstance(value, bool):
            str_value = "true" if value else "false"
        else:
            str_value = str(value)

        write_client = AzureAppConfigurationClient(
            base_url=endpoint, credential=self.credential
        )
        setting = ConfigurationSetting(key=key, label=label, value=str_value)
        write_client.set_configuration_setting(setting)


# Agent Landing Zone (Azure/agent-landing-zone#695): only the 'agent-lz' base label is read.
AGENTLZ_LABEL = "agent-lz"
APP_LABEL = "agent-app-orchestrator"
LEGACY_APP_LABEL = "gpt-rag-orchestrator"
LOADED_LABELS = ("orchestrator", APP_LABEL, LEGACY_APP_LABEL, AGENTLZ_LABEL, "<no-label>")


def candidate_keys(key: str) -> list[str]:
    """Return lookup keys in precedence order (single read: the key itself)."""
    return [key]
