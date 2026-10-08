import os
import logging
from typing import Any
from azure.core.exceptions import ClientAuthenticationError
from azure.appconfiguration import (
    AzureAppConfigurationClient,
    ConfigurationSetting,
)
from azure.appconfiguration.provider import (
    AzureAppConfigurationKeyVaultOptions,
    load,
    SettingSelector,
)
from connectors.identity_manager import get_identity_manager

from tenacity import retry, wait_random_exponential, stop_after_attempt, RetryError

class AppConfigClient:

    credential = None
    aiocredential = None

    def __init__(self):
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
        self.client = {}

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
        # Legacy labels load before the current app label so the new label overrides them.
        legacy_orchestrator_label_selector = SettingSelector(label_filter=f'orchestrator,{LEGACY_APP_LABEL}', key_filter='*')
        orchestrator_label_selector = SettingSelector(label_filter=APP_LABEL, key_filter='*')
        base_label_selector = SettingSelector(label_filter=AGENTLZ_LABEL, key_filter='*')
        no_label_selector = SettingSelector(label_filter=None, key_filter='*')

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


    def get(self, key: str, default: Any = None, type: type = str) -> Any:
        return self.get_value(key, default=default, allow_none=False, type=type)
    
    def get_value(self, key: str, default: str = None, allow_none: bool = False, type: type = str) -> str:

        if key is None:
            raise Exception('The key parameter is required for get_value().')

        value = None

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
    def retry_before_sleep(retry_state):
        # Log the outcome of each retry attempt.
        message = f"""Retrying {retry_state.fn}:
                        attempt {retry_state.attempt_number}
                        ended with: {retry_state.outcome}"""
        if retry_state.outcome.failed:
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
    def get_config_with_retry(self, name):
        if self.disabled or not self.client:
            return None
        try:
            return self.client[name]
        except KeyError:
            return None

    # Helper functions for reading environment variables
    def read_env_variable(self, var_name, default=None):
        value = self.get_value(var_name, default)
        return value.strip() if value else default

    def read_env_list(self, var_name):
        value = self.get_value(var_name, "")
        return [item.strip() for item in value.split(",") if item.strip()]

    def read_env_boolean(self, var_name, default=False):
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
