"""Local US2 settings: fresh turns, frozen runs, live authorization."""

import json
from unittest.mock import MagicMock

import pytest

from native.agent import create_agent
from native.config import AUTHORIZATION_KEYS, FunctionalSnapshot, NativeConfig
from native.presentation import settings
from test_native_runtime import BASE, RecordingClient, fake_config
from test_native_presentation_multi_agent import ToolClient, producer


def configuration(agents):
    return json.dumps({"schemaVersion": 1, "agents": agents})


@pytest.mark.parametrize("value", [None, configuration({}), configuration({"a": {"enabled": False}})])
async def test_disabled_agent_has_one_normal_source_call_and_no_optional_work(monkeypatch, value):
    counts = {}
    client = ToolClient("9.750")
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": value})
    _, source = producer(config, "a", 0, client, counts)

    async def composed(*args):
        return [source]

    monkeypatch.setattr("native.agent.compose_tools", composed)
    monkeypatch.setattr("native.presentation_middleware.normalize",
                        lambda *args: pytest.fail("Disabled agent must not normalize"))
    async with create_agent(config, client=client, definition_id="a") as agent:
        result = await agent.run("question")
    assert result.text == "authorized narration"
    assert counts == {"a": 1}
    assert client.calls == 2  # Ordinary tool request plus ordinary narration.
    assert client.guidance == []  # No structured selection call.


async def test_one_agent_opt_in_does_not_enable_another(monkeypatch):
    counts = {}
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": configuration({
        "a": {"enabled": True, "guidance": "a-only"}, "b": {"enabled": False},
    })})
    for definition in ("a", "b", "absent"):
        client = ToolClient("9.750")
        _, source = producer(config, definition, 0, client, counts)

        async def composed(*args):
            return [source]

        monkeypatch.setattr("native.agent.compose_tools", composed)
        async with create_agent(config, client=client, definition_id=definition) as agent:
            result = await agent.run("question")
        assert ("agent_lz_presentation" in result.text) is (definition == "a")
        assert len(client.guidance) == (1 if definition == "a" else 0)
        if client.guidance:
            assert "a-only" in client.guidance[0]
    assert counts == {"a": 1, "b": 1, "absent": 1}


@pytest.mark.parametrize("value", [
    '{"schemaVersion":2,"agents":{}}',
    '{"schemaVersion":1,"schemaVersion":1,"agents":{}}',
    configuration({"a": {"enabled": "true"}}),
])
async def test_invalid_present_errors_before_model_or_tools(value):
    client = RecordingClient()
    async with create_agent(fake_config({"AGENT_PRESENTATION_SETTINGS": value}), client=client) as agent:
        with pytest.raises(ValueError, match="AGENT_PRESENTATION_SETTINGS"):
            await agent.run("question")
    assert client.calls == []


async def test_fresh_turn_bypasses_throttle_and_workflow_retains_snapshot_with_live_auth(monkeypatch):
    live = {**BASE, "AGENT_PRESENTATION_SETTINGS": configuration({"a": {"enabled": True}}),
            "RETRIEVAL_AUTH_MODE": "obo", "ALLOW_ANONYMOUS": "false", "MCP_ENABLED": "false"}
    providers = []

    def loader(**kwargs):
        keys = [selector.key_filter for selector in kwargs["selects"]]
        assert kwargs["refresh_interval"] == 30
        assert all(selector.label_filter == "agent-lz" for selector in kwargs["selects"])
        provider = MagicMock()
        # Cached provider refresh deliberately does nothing: simulate throttle.
        provider.values = {k: v for k, v in live.items() if k in keys}
        provider.items.side_effect = lambda: provider.values.items()
        provider.keys.side_effect = lambda: provider.values.keys()
        provider.__getitem__.side_effect = lambda k: provider.values[k]
        providers.append(provider)
        return provider

    config = await NativeConfig("https://config.example.invalid", None, None, loader).open()
    try:
        first = await config.view()
        snapshot = FunctionalSnapshot.capture(first.values)
        assert not AUTHORIZATION_KEYS.intersection(snapshot.values)
        counts = {}
        client = ToolClient("9.750")
        _, source = producer(config, "a", 0, client, counts)
        observed_auth = []

        async def composed(view, *args):
            observed_auth.append(view.get("RETRIEVAL_AUTH_MODE"))
            return [source]

        monkeypatch.setattr("native.agent.compose_tools", composed)
        agent = create_agent(config, client=client, definition_id="a")
        enabled = await agent.run("first")
        assert "agent_lz_presentation" in enabled.text
        live.pop("AGENT_PRESENTATION_SETTINGS")  # Removed before the next turn (<30s).
        providers[0].values.update(RETRIEVAL_AUTH_MODE="application", ALLOW_ANONYMOUS="true")
        # Current native admission rejects anonymous mode, including resume.
        # A frozen functional snapshot must not bypass that live restriction.
        with pytest.raises(ValueError, match="authenticated user admission"):
            await config.view(snapshot)
        providers[0].values["ALLOW_ANONYMOUS"] = "false"
        live["MCP_AUTH_RESOURCE_SCOPE"] = "new-live-scope"
        fresh = await config.view()
        assert settings(fresh.get("AGENT_PRESENTATION_SETTINGS", None, type=None), "a") is None
        disabled = await agent.run("next")
        assert "agent_lz_presentation" not in disabled.text
        assert len(client.guidance) == 1
        frozen_agent = create_agent(config, snapshot=snapshot, client=client, definition_id="a")
        resumed = await frozen_agent.run("workflow")
        assert "agent_lz_presentation" in resumed.text
        assert len(client.guidance) == 2
        assert counts == {"a": 3}  # Display/selection never repeats the source.
        assert observed_auth == ["obo", "application", "application"]
        frozen = await config.view(snapshot)
        assert settings(frozen.get("AGENT_PRESENTATION_SETTINGS", None, type=None), "a") is not None
        assert frozen.get("RETRIEVAL_AUTH_MODE") == "application"
        assert frozen.get("ALLOW_ANONYMOUS", type=bool) is False
        assert frozen.get("MCP_AUTH_RESOURCE_SCOPE") == "new-live-scope"
        with pytest.raises(TypeError, match="immutable"):
            snapshot.values["AGENT_PRESENTATION_SETTINGS"] = configuration({})
        assert snapshot == FunctionalSnapshot.model_validate_json(snapshot.model_dump_json())
        for provider in providers[1:]:
            provider.close.assert_called_once()
    finally:
        await config.close()
