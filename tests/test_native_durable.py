"""Product graph registration against the real published standalone worker."""

import inspect
from unittest.mock import MagicMock

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
    assert AGENT_NAME == "alz-conversation" and WORKFLOW_NAME == "alz-research-review"


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
    assert data["durable_session_id"].startswith("@dafx-v2-alz-conversation@")
    assert "session" in inspect.signature(agent.run).parameters


def test_worker_uses_provisioned_binding_and_identity_only():
    assert scheduler_binding(
        "Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication=ManagedIdentity"
    ) == {"host_address": "https://scheduler.example.invalid", "taskhub": "synthetic"}


@pytest.mark.parametrize("binding", [
    "", "Endpoint=http://scheduler.example.invalid;TaskHub=synthetic;Authentication=ManagedIdentity",
    "Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication=Secret",
    "Endpoint=https://scheduler.example.invalid;TaskHub=one;TaskHub=two;Authentication=ManagedIdentity",
    "Endpoint=https://scheduler.example.invalid;TaskHub=synthetic;Authentication=ManagedIdentity;Password=x",
])
def test_worker_rejects_missing_ambiguous_or_secret_binding(binding):
    with pytest.raises(ValueError):
        scheduler_binding(binding)
