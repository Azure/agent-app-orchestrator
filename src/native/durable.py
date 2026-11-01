"""Standalone Container Apps worker using the official Durable Extension and DTS."""

import asyncio
import os
import signal
import threading
from typing import Any
from urllib.parse import urlsplit

from agent_framework_durabletask import DurableAIAgentWorker
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

from .agent import agent_factory, create_agent
from .bootstrap import configure_worker_telemetry
from .config import NativeConfig
from .contracts import AGENT_NAME
from .workflow import build_workflow


def _scheduler_settings(value: str) -> tuple[dict[str, str], str]:
    """Validate deployment MI or the explicit, separate local credential chain."""
    fields: dict[str, str] = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        key, separator, content = item.partition("=")
        key, content = key.strip(), content.strip()
        if not separator or key in fields or key not in ("Endpoint", "TaskHub", "Authentication"):
            raise ValueError("Invalid nonsecret DTS binding.")
        fields[key] = content
    if set(fields) != {"Endpoint", "TaskHub", "Authentication"}:
        raise ValueError("DTS binding requires Endpoint, TaskHub and Authentication.")
    if fields["Authentication"] not in ("ManagedIdentity", "DefaultAzure"):
        raise ValueError("DTS requires ManagedIdentity or explicit local DefaultAzure authentication.")
    endpoint = urlsplit(fields["Endpoint"])
    if (
        endpoint.scheme != "https" or not endpoint.hostname or endpoint.path not in ("", "/")
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        or not fields["TaskHub"] or any(c.isspace() for c in fields["TaskHub"])
    ):
        raise ValueError("Invalid provisioned DTS endpoint or task hub.")
    return (
        {"host_address": fields["Endpoint"].rstrip("/"), "taskhub": fields["TaskHub"]},
        fields["Authentication"],
    )


def scheduler_binding(value: str) -> dict[str, str]:
    """Translate a validated nonsecret binding to official Python SDK inputs.

    Python 1.12.0 has explicit host_address/taskhub arguments, not a
    from_connection_string factory. The platform/UI deployment uses
    ManagedIdentity. DefaultAzure is a separate local worker choice, not a
    deployment fallback. Never infer an endpoint from resource names.
    """
    return _scheduler_settings(value)[0]


def build_worker(config: NativeConfig, worker: Any) -> DurableAIAgentWorker:
    if config.settings is None or config.settings.host != "container_apps":
        raise ValueError("The DTS worker requires ORCHESTRATION_HOST=container_apps.")
    # This is a new, isolated native task hub, not an upgrade/migration engine.
    runtime = DurableAIAgentWorker(worker, deployment_mode="isolated_v2", process_legacy_entities=False)
    if config.settings.mode in ("conversational", "both"):
        runtime.add_agent(create_agent(config), entity_id=AGENT_NAME)
    if config.settings.mode in ("workflow", "both"):
        runtime.configure_workflow(build_workflow(config, agent_factory(config)))
    return runtime


def main() -> None:
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    binding, authentication = _scheduler_settings(
        os.environ.get("DURABLE_TASK_SCHEDULER_CONNECTION_STRING", ""),
    )
    with (
        DefaultAzureCredential() as credential,
        (ManagedIdentityCredential(
            client_id=os.environ.get("AZURE_CLIENT_ID") or None,
        ) if authentication == "ManagedIdentity" else DefaultAzureCredential()) as scheduler_credential,
    ):
        config = NativeConfig(os.environ.get("APP_CONFIG_ENDPOINT", ""), credential, credential)
        runtime = None
        try:
            asyncio.run(config.open())
            configure_worker_telemetry(asyncio.run(config.view()))
            runtime = build_worker(config, DurableTaskSchedulerWorker(
                host_address=binding["host_address"], taskhub=binding["taskhub"],
                token_credential=scheduler_credential, secure_channel=True,
            ))
            runtime.start()
            stopped.wait()
        finally:
            if runtime is not None:
                runtime.stop()
            asyncio.run(config.close())


if __name__ == "__main__":
    main()
