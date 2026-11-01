"""Product graph registration against the real published standalone worker."""

import inspect
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("agent_framework_durabletask")

from agent_framework_durabletask import DurableAIAgentClient, DurableAIAgentWorker, DurableWorkflowClient
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

from native.durable import build_worker, scheduler_binding
from native.contracts import AGENT_NAME, WORKFLOW_NAME
from test_native_runtime import fake_config


@pytest.mark.parametrize("mode", ["conversational", "workflow", "both"])
def test_product_registers_with_real_durable_worker_without_http(mode):
    config = fake_config(mode=mode, host="container_apps")
    config.sync_credential = MagicMock()
    worker = DurableTaskSchedulerWorker(
        host_address="https://scheduler.example.invalid", taskhub="synthetic", token_credential=MagicMock(),
    )
    runtime = build_worker(config, worker)
    assert isinstance(runtime, DurableAIAgentWorker)
    # No start: registration exercises SDK validation/serialization but never calls DTS.
    assert "workflow" in inspect.signature(runtime.configure_workflow).parameters
    assert AGENT_NAME == "agent-lz-conversation" and WORKFLOW_NAME == "agent-lz-research-review"


def test_real_native_clients_have_required_wire_shapes():
    assert "workflow_name" in inspect.signature(DurableWorkflowClient).parameters
    for method in ("start_workflow", "get_pending_hitl_requests", "send_hitl_response", "await_workflow_output"):
        assert hasattr(DurableWorkflowClient, method)
    assert "agent_name" in inspect.signature(DurableAIAgentClient.get_agent).parameters


def test_isolated_v2_agent_client_uses_native_serializable_session_handles():
    client = DurableAIAgentClient(MagicMock(), entity_version=2)
    agent = client.get_agent(AGENT_NAME)
    session = agent.create_session()
    data = session.to_dict()
    assert data["durable_entity_version"] == 2
    assert data["durable_session_id"].startswith("@dafx-v2-agent-lz-conversation@")
    assert "session" in inspect.signature(agent.run).parameters


def test_worker_uses_provisioned_binding_and_identity_only():
    assert scheduler_binding(
        "Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication=ManagedIdentity"
    ) == {"host_address": "https://scheduler.example.invalid", "taskhub": "synthetic"}


@pytest.mark.parametrize("authentication", ["ManagedIdentity", "DefaultAzure"])
def test_worker_entrypoint_honors_explicit_identity_with_real_sdk_constructor(monkeypatch, authentication):
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    from native import durable

    monkeypatch.setenv(
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        f"Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication={authentication}",
    )
    client_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("AZURE_CLIENT_ID", client_id)
    config = MagicMock(open=AsyncMock(), close=AsyncMock(), view=AsyncMock())
    monkeypatch.setattr(durable, "NativeConfig", lambda *_: config)
    monkeypatch.setattr(durable, "configure_worker_telemetry", lambda *_: None)
    # Only replace the product's event factory, not threading used by the SDK.
    stopped = threading.Event()
    stopped.set()
    monkeypatch.setattr(durable, "threading", SimpleNamespace(Event=lambda: stopped))
    monkeypatch.setattr(durable.signal, "signal", lambda *_: None)
    credential_options = []
    worker_credentials = []

    def managed_credential(**options):
        credential_options.append(options)
        return ManagedIdentityCredential(**options)

    def scheduler_worker(**options):
        worker_credentials.append(options["token_credential"])
        assert set(options) == {"host_address", "taskhub", "token_credential", "secure_channel"}
        assert options["host_address"] == "https://scheduler.example.invalid"
        assert options["taskhub"] == "synthetic"
        assert options["secure_channel"] is True
        # The published constructor is unchanged; the transport starts only on start().
        return DurableTaskSchedulerWorker(**options)

    runtime = MagicMock()
    registered_workers = []

    def register(actual_config, worker):
        assert actual_config is config
        registered_workers.append(worker)
        return runtime

    monkeypatch.setattr(durable, "ManagedIdentityCredential", managed_credential)
    monkeypatch.setattr(durable, "DurableTaskSchedulerWorker", scheduler_worker)
    monkeypatch.setattr(durable, "build_worker", register)
    durable.main()

    assert credential_options == ([{"client_id": client_id}] if authentication == "ManagedIdentity" else [])
    assert len(worker_credentials) == 1
    expected_credential = ManagedIdentityCredential if authentication == "ManagedIdentity" else DefaultAzureCredential
    assert isinstance(worker_credentials[0], expected_credential)
    assert isinstance(registered_workers[0], DurableTaskSchedulerWorker)
    runtime.start.assert_called_once_with()
    runtime.stop.assert_called_once_with()
    config.open.assert_awaited_once_with()
    config.close.assert_awaited_once_with()


@pytest.mark.parametrize("binding", [
    "", "Endpoint=http://scheduler.example.invalid;TaskHub=synthetic;Authentication=ManagedIdentity",
    "Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication=Secret",
    "Endpoint=https://scheduler.example.invalid;TaskHub=one;TaskHub=two;Authentication=ManagedIdentity",
    "Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication=ManagedIdentity;Password=x",
])
def test_worker_rejects_missing_ambiguous_or_secret_binding(binding):
    with pytest.raises(ValueError):
        scheduler_binding(binding)
