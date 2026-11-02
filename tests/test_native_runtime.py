"""Real published MAF graph/Agent behavior; Azure boundaries remain mocked."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_framework import (
    BaseChatClient, ChatResponse, ChatResponseUpdate, Content,
    InMemoryCheckpointStorage, Message, ResponseStream,
)
from pydantic import ValidationError

from native.agent import create_agent
from native.config import ConfigView, FunctionalSnapshot, NativeConfig, RuntimeSettings, config_scope
from native.contracts import (
    AGENT_NAME, WORKFLOW_NAME, ResearchReviewResult, ResearchReviewStart, ReviewDecision,
)
from native.workflow import build_workflow


BASE = {
    "ORCHESTRATION_HOST": "hosted",
    "AI_FOUNDRY_PROJECT_ENDPOINT": "https://project.example.invalid",
    "CHAT_DEPLOYMENT_NAME": "model",
}
SERVICE = {
    "HOSTED_AGENT_AUTH_MODE": "service_identity",
    "RETRIEVAL_AUTH_MODE": "native_search", "ALLOW_ANONYMOUS": False,
}


class RecordingClient(BaseChatClient):
    STORES_BY_DEFAULT = False
    def __init__(self):
        super().__init__()
        self.calls = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.calls.append((messages, options))
        response = ChatResponse(messages=[Message("assistant", ["synthetic research draft"])])
        if stream:
            async def updates():
                yield ChatResponseUpdate(
                    role="assistant", contents=[Content.from_text(response.text)],
                    message_id="synthetic-message", finish_reason="stop",
                )
            return ResponseStream(updates(), finalizer=lambda _: response)
        async def result():
            return response
        return result()


def fake_config(values=None, mode="workflow", host="hosted"):
    values = {**BASE, **(values or {}), "APPLICATION_MODE": mode, "ORCHESTRATION_HOST": host}
    return SimpleNamespace(
        settings=RuntimeSettings.read(values, {}), sync_credential=None,
        view=AsyncMock(return_value=ConfigView(values)),
    )


@pytest.mark.parametrize("host,mode", [
    ("hosted", "conversational"), ("hosted", "workflow"),
    ("container_apps", "conversational"), ("container_apps", "workflow"), ("container_apps", "both"),
])
def test_native_host_mode_selection(host, mode):
    settings = RuntimeSettings.read({**BASE, "ORCHESTRATION_HOST": host, "APPLICATION_MODE": mode}, {})
    assert settings.host == host and settings.mode == mode


@pytest.mark.parametrize("values", [
    {}, {**BASE, "ORCHESTRATION_HOST": "classic"},
    {**BASE, "APPLICATION_MODE": "both"}, {**BASE, "CHAT_DEPLOYMENT_NAME": ""},
    {**BASE, "APPLICATION_MODE": "rag"},
    {**BASE, "AI_FOUNDRY_PROJECT_ENDPOINT": "http://project.example.invalid"},
])
def test_missing_and_incompatible_configuration_fail_closed(values):
    with pytest.raises((ValueError, ValidationError)):
        RuntimeSettings.read(values, {})


def test_mode_default_and_deployment_contradictions():
    assert RuntimeSettings.read(BASE, {}).mode == "conversational"
    with pytest.raises(ValueError, match="Contradictory"):
        RuntimeSettings.read(BASE, {"ORCHESTRATION_HOST": "container_apps"})


def test_default_obo_and_explicit_service_mode_are_structural():
    assert RuntimeSettings.read(BASE, {}).auth_mode == "user_delegated"
    assert RuntimeSettings.read({**BASE, **SERVICE}, {}).auth_mode == "service_identity"
    with pytest.raises(ValueError, match="Contradictory"):
        RuntimeSettings.read(
            {**BASE, **SERVICE}, {"HOSTED_AGENT_AUTH_MODE": "user_delegated"},
        )
    with pytest.raises(ValueError, match="requires ORCHESTRATION_HOST=hosted"):
        RuntimeSettings.read({**BASE, **SERVICE, "ORCHESTRATION_HOST": "container_apps"}, {})


@pytest.mark.parametrize("override", [
    {"HOSTED_AGENT_AUTH_MODE": "automatic"},
    {"ALLOW_ANONYMOUS": True},
])
def test_service_configuration_retains_authentication_guards(override):
    with pytest.raises(ValueError):
        RuntimeSettings.read({**BASE, **SERVICE, **override}, {})


@pytest.mark.parametrize("override", [
    {},
    {"FOUNDRY_IQ_INGESTION_PERMISSION_OPTIONS": None},
    {"FOUNDRY_IQ_INGESTION_PERMISSION_OPTIONS": ["rbacScope"]},
    {"FOUNDRY_IQ_IS_ADLS_GEN2": True}, {"FOUNDRY_IQ_PATTERN": "searchIndex"},
    {"WORK_IQ_ENABLED": True}, {"MCP_ENABLED": True}, {"MULTIMODAL_ENABLED": True},
    {"SEARCH_RETRIEVAL_ENABLED": True},
])
def test_service_configuration_leaves_source_authorization_to_each_capability(override):
    values = {**BASE, **SERVICE, **override}
    original = dict(values)
    assert RuntimeSettings.read(values, {}).auth_mode == "service_identity"
    assert values == original


async def test_auth_profile_cannot_change_live_or_be_restored_from_checkpoint():
    providers = []
    values = {**BASE, **SERVICE}

    def loader(**kwargs):
        keys = [selector.key_filter for selector in kwargs["selects"]]
        provider = MagicMock()
        provider.values = {k: v for k, v in values.items() if k in keys}
        provider.items.side_effect = lambda: provider.values.items()
        provider.keys.side_effect = lambda: provider.values.keys()
        provider.__getitem__.side_effect = lambda key: provider.values[key]
        providers.append(provider)
        return provider

    config = await NativeConfig("https://config.example.invalid", None, None, loader).open()
    try:
        snapshot = FunctionalSnapshot.capture({**values, "HOSTED_AGENT_AUTH_MODE": "user_delegated"})
        assert "HOSTED_AGENT_AUTH_MODE" not in snapshot.values
        assert (await config.view(snapshot)).get("HOSTED_AGENT_AUTH_MODE") == "service_identity"
        providers[0].values["HOSTED_AGENT_AUTH_MODE"] = "user_delegated"
        with pytest.raises(ValueError, match="Structural"):
            await config.view(snapshot)
    finally:
        await config.close()


async def test_native_connection_binding_cannot_be_hot_swapped():
    providers = []
    values = {
        **BASE, **SERVICE, "FOUNDRY_IQ_ENABLED": True,
        "TOOLBOX_ENDPOINT": BASE["AI_FOUNDRY_PROJECT_ENDPOINT"] + "/toolboxes/common/mcp?api-version=v1",
    }

    def loader(**kwargs):
        keys = [selector.key_filter for selector in kwargs["selects"]]
        provider = MagicMock()
        provider.values = {k: v for k, v in values.items() if k in keys}
        provider.items.side_effect = lambda: provider.values.items()
        provider.keys.side_effect = lambda: provider.values.keys()
        provider.__getitem__.side_effect = lambda key: provider.values[key]
        providers.append(provider)
        return provider

    config = await NativeConfig("https://config.example.invalid", None, None, loader).open()
    try:
        view = await config.view()
        assert view.get("TOOLBOX_ENDPOINT") == values["TOOLBOX_ENDPOINT"]
        providers[1].values["TOOLBOX_ENDPOINT"] = BASE["AI_FOUNDRY_PROJECT_ENDPOINT"] + "/toolboxes/other/mcp?api-version=v1"
        with pytest.raises(ValueError, match="Structural"):
            await config.view()
    finally:
        await config.close()


@pytest.mark.parametrize("mode", ["conversational", "workflow"])
def test_shared_both_mode_can_be_overridden_by_one_hosted_publication(mode):
    settings = RuntimeSettings.read(
        {**BASE, "APPLICATION_MODE": "both"},
        {"ORCHESTRATION_HOST": "hosted", "APPLICATION_MODE": mode},
    )
    assert settings.host == "hosted" and settings.mode == mode


@pytest.mark.parametrize("values,env", [
    ({**BASE, "APPLICATION_MODE": "conversational"}, {"APPLICATION_MODE": "workflow"}),
    ({**BASE, "APPLICATION_MODE": "workflow"}, {"APPLICATION_MODE": "conversational"}),
    ({**BASE, "APPLICATION_MODE": "both", "ORCHESTRATION_HOST": "container_apps"},
     {"APPLICATION_MODE": "workflow"}),
    ({**BASE, "APPLICATION_MODE": "both"}, {"ORCHESTRATION_HOST": "container_apps", "APPLICATION_MODE": "workflow"}),
])
def test_hosted_publication_override_does_not_relax_real_contradictions(values, env):
    with pytest.raises(ValueError, match="Contradictory"):
        RuntimeSettings.read(values, env)


async def test_scoped_selectors_refresh_snapshot_and_secrets():
    providers = []

    def loader(**kwargs):
        keys = [selector.key_filter for selector in kwargs["selects"]]
        assert all(selector.label_filter == "agent-lz" for selector in kwargs["selects"])
        assert "*" not in keys
        assert all(label == "agent-lz" for _, label in kwargs["refresh_on"])
        assert "key_vault_options" not in kwargs  # Disabled tools cannot resolve secrets.
        provider = MagicMock()
        provider.items.side_effect = lambda: provider.values.items()
        provider.keys.side_effect = lambda: provider.values.keys()
        provider.__getitem__.side_effect = lambda k: provider.values[k]
        provider.values = {k: v for k, v in {**BASE, "CHAT_TEMPERATURE": "0.2"}.items() if k in keys}
        providers.append(provider)
        return provider

    config = await NativeConfig("https://config.example.invalid", None, None, loader).open()
    first = await config.view()
    snapshot = FunctionalSnapshot.capture(first.values)
    providers[0].values["CHAT_TEMPERATURE"] = "0.8"
    assert (await config.view()).get("CHAT_TEMPERATURE", type=float) == 0.8
    assert (await config.view(snapshot)).get("CHAT_TEMPERATURE", type=float) == 0.2
    assert len(providers) == 4  # One base plus three request-owned MCP reads.
    assert providers[0].refresh.call_count == 3
    await config.close()
    providers[0].close.assert_called_once()


def test_snapshot_does_not_contain_credential_values():
    snapshot = FunctionalSnapshot.capture({**BASE, "MCP_API_KEY": "synthetic-secret"})
    assert "synthetic-secret" not in snapshot.model_dump_json()
    assert "ORCHESTRATION_HOST" not in snapshot.values


async def test_only_enabled_consumers_select_their_secrets_and_snapshot_keeps_them_current():
    calls, providers = [], []
    all_values = {
        **BASE, "MCP_ENABLED": True, "MCP_API_KEY": "synthetic-first-secret",
        "MCP_APP_ENDPOINT": "https://mcp.example.invalid", "MCP_TRUSTED_HOSTS": "mcp.example.invalid",
    }

    def loader(**kwargs):
        keys = [selector.key_filter for selector in kwargs["selects"]]
        calls.append((keys, "key_vault_options" in kwargs))
        provider = MagicMock()
        provider.items.side_effect = lambda: provider.values.items()
        provider.keys.side_effect = lambda: provider.values.keys()
        provider.__getitem__.side_effect = lambda key: provider.values[key]
        provider.values = {k: v for k, v in all_values.items() if k in keys}
        providers.append(provider)
        return provider

    config = await NativeConfig("https://config.example.invalid", None, None, loader).open()
    try:
        snapshot = FunctionalSnapshot.capture((await config.view()).values)
        assert len(calls) == 3 and calls[0][1] is False and calls[1][1] is False and calls[2][1] is True
        assert "AUDIT_HMAC_KEY" not in calls[1][0]
        all_values["MCP_API_KEY"] = "synthetic-refreshed-secret"
        assert (await config.view(snapshot)).get("MCP_API_KEY") == "synthetic-refreshed-secret"
        assert "synthetic-first-secret" not in snapshot.model_dump_json()
        all_values["MCP_ENABLED"] = False
        assert (await config.view(snapshot)).get("MCP_ENABLED", type=bool) is False
        providers[0].values["MULTIMODAL_ENABLED"] = True
        with pytest.raises(ValueError, match="redeploy"):
            await config.view()
    finally:
        await config.close()


def test_existing_connectors_use_invocation_config_without_classic_bootstrap(monkeypatch):
    import dependencies
    view = ConfigView({"SEARCH_RAG_INDEX_NAME": "documents"})
    with config_scope(view):
        assert dependencies.get_config() is view
        assert dependencies.get_config("refresh") is view


async def test_native_agent_refreshes_generation_settings_per_turn():
    config = fake_config({"CHAT_TEMPERATURE": 0.2}, mode="conversational")
    client = RecordingClient()
    agent = create_agent(config, client=client)
    assert agent.name == AGENT_NAME
    await agent.run("First synthetic question")
    config.view.return_value = ConfigView({**BASE, "CHAT_TEMPERATURE": 0.8})
    await agent.run("Second synthetic question")
    assert [options["temperature"] for _, options in client.calls] == [0.2, 0.8]
    assert config.view.await_count == 2


@pytest.mark.parametrize("decision,status", [("approve", "approved"), ("reject", "rejected")])
async def test_native_graph_pauses_and_fresh_graph_resumes_with_typed_result(decision, status):
    config, client, snapshots = fake_config(), RecordingClient(), []

    @asynccontextmanager
    async def factory(snapshot=None):
        snapshots.append(snapshot)
        yield create_agent(config, snapshot=snapshot, client=client)

    storage = InMemoryCheckpointStorage()
    workflow = build_workflow(config, factory)
    assert workflow.name == WORKFLOW_NAME
    assert set(workflow.executors) == {"research", "human-review", "complete"}
    pending = await workflow.run(ResearchReviewStart(topic="Synthetic topic"), checkpoint_storage=storage)
    requests = pending.get_request_info_events()
    assert len(requests) == 1 and not pending.get_outputs()
    request = requests[0]
    assert request.response_type is ReviewDecision
    assert request.data.draft == "synthetic research draft"
    checkpoints = await storage.list_checkpoints(workflow_name=WORKFLOW_NAME)
    checkpoint = checkpoints[-1]
    resumed = await build_workflow(config, factory).run(
        responses={request.request_id: ReviewDecision(decision=decision, comment="Reviewed")},
        checkpoint_id=checkpoint.checkpoint_id, checkpoint_storage=storage,
    )
    result = resumed.get_outputs()[0]
    assert isinstance(result, ResearchReviewResult)
    assert result.status == status
    assert result.configuration_revision == snapshots[0].revision
    assert len(client.calls) == 1  # Resume never repeats the research/model call.


async def test_native_graph_accepts_the_durable_clients_json_start_contract():
    config = fake_config(host="container_apps")
    model = RecordingClient()

    @asynccontextmanager
    async def factory(snapshot):
        yield create_agent(config, snapshot=snapshot, client=model)

    workflow = build_workflow(config, factory)
    pending = await workflow.run({"topic": "Synthetic JSON input"})
    assert pending.get_request_info_events()[0].data.topic == "Synthetic JSON input"
    with pytest.raises(Exception, match="owner"):
        await build_workflow(config, factory).run({"topic": "Invalid", "owner": "not identity"})
    assert len(model.calls) == 1


@pytest.mark.parametrize("data", [
    {"decision": "maybe"}, {"decision": True}, {"decision": "approve", "owner": "caller"},
])
def test_review_reply_is_typed_and_cannot_supply_identity(data):
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate(data)


def test_start_cannot_inject_snapshot_or_owner():
    with pytest.raises(ValidationError):
        ResearchReviewStart.model_validate({"topic": "question", "snapshot": {}, "owner": "caller"})


def test_executable_contract_matches_names():
    contract = json.loads((Path(__file__).parents[1] / "contracts/native-runtime.json").read_text())
    assert contract["agent_name"] == AGENT_NAME and contract["workflow_name"] == WORKFLOW_NAME
    assert contract["migration_engine"] is False
