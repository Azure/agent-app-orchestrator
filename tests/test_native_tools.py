"""Native tools keep non-SQL authorization/audit and consume generic MCP."""

import json
import pickle
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from native.config import BASE_KEYS, TOOL_KEYS, ConfigView, FunctionalSnapshot, NativeConfig
from native.tools import SourceRegistry, ToolIdentity, audited_function, compose_tools
from test_audit_emitter import capture_audit_logs
from test_native_runtime import BASE, RecordingClient


def test_invocation_credentials_cannot_be_checkpointed_or_rendered():
    identity = ToolIdentity(call_id="synthetic-call", search_token="synthetic-token")
    assert "synthetic-token" not in repr(identity)
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(identity)


async def test_disabled_tools_and_obsolete_sql_flag_never_register_direct_sql():
    async with AsyncExitStack() as resources:
        assert await compose_tools(ConfigView({}), ToolIdentity(), resources) == []
        assert await compose_tools(
            ConfigView({"NL2SQL_ENABLED": True}), ToolIdentity(), resources,
        ) == []
    assert "NL2SQL_ENABLED" not in BASE_KEYS
    assert "NL2SQL_ENABLED" not in TOOL_KEYS
    root = Path(__file__).resolve().parents[1]
    for path in (
        "src/plugins/nl2sql/plugin.py", "src/plugins/nl2sql/sql_validation.py",
        "src/connectors/sqldbs.py", "src/connectors/fabric.py",
    ):
        assert not (root / path).exists()


async def test_enabled_tool_missing_configuration_is_not_a_fallback():
    async with AsyncExitStack() as resources:
        with pytest.raises(ValueError, match="SEARCH_CONNECTION_ID"):
            await compose_tools(ConfigView({"SEARCH_RETRIEVAL_ENABLED": True}), ToolIdentity(), resources)


async def test_multimodal_tool_is_optional_and_missing_delegation_does_not_query_search():
    async with AsyncExitStack() as resources:
        tools = await compose_tools(ConfigView({"MULTIMODAL_ENABLED": True}), ToolIdentity(), resources)
        assert [tool.name for tool in tools] == ["multimodal_retrieve"]
        with pytest.raises(ValueError, match="delegated Search"):
            await tools[0].invoke(arguments={"input": "synthetic"}, skip_parsing=True)


async def test_native_multimodal_adapter_retains_text_images_captions_and_user_authorization():
    search = SimpleNamespace(search=AsyncMock(return_value={"value": [{
        "url": "https://documents.example.invalid/synthetic.pdf", "content": "Synthetic text",
        "relatedImages": ["https://documents.example.invalid/synthetic.png"],
        "imageCaptions": "[image]: Synthetic caption",
    }]}), close=AsyncMock())
    with (
        patch("connectors.search.SearchClient", return_value=search),
        patch("native.tools.Embeddings.get_embeddings", new=AsyncMock(return_value=[0.1, 0.2])),
    ):
        async with AsyncExitStack() as resources:
            tools = await compose_tools(
                ConfigView({
                    "MULTIMODAL_ENABLED": True, "SEARCH_RAG_INDEX_NAME": "synthetic-index",
                    "SEARCH_SERVICE_QUERY_ENDPOINT": "https://search.example.invalid",
                }),
                ToolIdentity(search_token="synthetic-delegated-token"), resources,
            )
            result = await tools[0].invoke(arguments={"input": "Synthetic question"}, skip_parsing=True)
    assert not result.error
    assert any("Synthetic text" in text for text in result.texts)
    assert result.images == [["https://documents.example.invalid/synthetic.png"]]
    assert any("Synthetic caption" in caption for captions in result.captions for caption in captions)
    assert search.search.await_args.kwargs["search_user_token"] == "synthetic-delegated-token"
    search.close.assert_awaited_once()


@pytest.mark.parametrize("trusted", [
    ["mcp.example.invalid"], '["mcp.example.invalid"]', "mcp.example.invalid",
])
async def test_generic_mcp_registration_uses_parent_configuration_and_request_identity(trusted):
    captured = {}
    tool = SimpleNamespace(functions=[object(), object()])

    @asynccontextmanager
    async def open_tool(**kwargs):
        captured.update(kwargs)
        yield tool
        captured["closed"] = True

    with patch("connectors.mcp_client.open_mcp_tool", open_tool):
        async with AsyncExitStack() as resources:
            tools = await compose_tools(ConfigView({
                "MCP_ENABLED": True, "MCP_APP_ENDPOINT": "https://mcp.example.invalid",
                "MCP_SERVER_TRANSPORT": "streamable-http", "MCP_CLIENT_TIMEOUT": 90,
                "MCP_TRUSTED_HOSTS": trusted,
                "MCP_AUTH_RESOURCE_SCOPE": "api://synthetic-mcp-api/.default",
            }), ToolIdentity(call_id="synthetic-call", search_token="not-mcp-auth"), resources)
            assert tools == tool.functions
    assert captured == {
        "endpoint": "https://mcp.example.invalid", "transport": "streamable-http",
        "timeout": 90, "user_context": None, "api_key": None,
        "auth_resource_scope": "api://synthetic-mcp-api/.default",
        "foundry_call_id": "synthetic-call", "closed": True,
    }


@pytest.mark.parametrize("trusted", [None, [], '["other.example.invalid"]', "[1]", {}])
async def test_untrusted_or_invalid_hosts_fail_before_mcp_connection(trusted):
    with patch("connectors.mcp_client.open_mcp_tool") as open_tool:
        async with AsyncExitStack() as resources:
            with pytest.raises(ValueError, match="MCP_TRUSTED_HOSTS"):
                await compose_tools(ConfigView({
                    "MCP_ENABLED": True, "MCP_APP_ENDPOINT": "https://mcp.example.invalid",
                    "MCP_TRUSTED_HOSTS": trusted,
                }), ToolIdentity(), resources)
        open_tool.assert_not_called()


async def test_source_registry_remains_available_for_non_sql_documents():
    client = SimpleNamespace(
        list_documents=AsyncMock(return_value=[{"id": "document-source"}]),
        get_document=AsyncMock(return_value={"id": "document-source"}),
        close=AsyncMock(),
    )
    with patch("connectors.cosmosdb.CosmosDBClient", return_value=client):
        registry = SourceRegistry(ConfigView({
            "DATABASE_ACCOUNT_NAME": "synthetic", "DATABASE_NAME": "sources",
        }))
        assert await registry.list_documents("document-sources") == [{"id": "document-source"}]
        assert await registry.get_document("document-sources", "document-source") == {"id": "document-source"}
        await registry.close()
    client.close.assert_awaited_once()


async def test_native_audit_preserves_schema_budget_and_sensitive_argument_redaction():
    async def retrieve(input: str) -> str:
        """Retrieve a synthetic document."""
        return "synthetic result"

    config = ConfigView({
        "AUDIT_EVENTS_ENABLED": True, "AUDIT_HMAC_KEY": "78" * 32,
        "AUDIT_HMAC_KEY_ID": "synthetic-key-id",
    })
    from native.tools import new_audit_context
    tool = audited_function(retrieve, config, new_audit_context(config))
    assert set(tool.input_model.model_json_schema()["properties"]) == {"input"}
    with capture_audit_logs() as capture:
        for _ in range(20):
            assert await tool.invoke(
                arguments={"input": "synthetic-private-question"}, skip_parsing=True,
            ) == "synthetic result"
    assert sum(record.event_type == "tool.invocation.started" for record in capture.records) == 16
    assert sum(record.event_type == "tool.invocation.completed" for record in capture.records) == 16
    assert all("synthetic-private-question" not in json.dumps(vars(record), default=str) for record in capture.records)
    assert all("78" * 32 not in str(vars(record)) for record in capture.records)


async def test_same_native_agent_activates_mcp_after_identity_setup_and_keeps_snapshot_live():
    from agent_framework import FunctionTool
    from native.agent import create_agent

    remote = {**BASE, "MCP_ENABLED": False}
    providers, selections, connections = [], [], []

    def loader(**kwargs):
        keys = [selector.key_filter for selector in kwargs["selects"]]
        selections.append(keys)
        assert "key_vault_options" not in kwargs  # Disabled/bearer mode never resolves API keys.
        provider = MagicMock()
        provider.values = {}
        def refresh():
            provider.values = {k: v for k, v in remote.items() if k in keys}
        refresh()
        provider.refresh.side_effect = refresh
        provider.items.side_effect = lambda: provider.values.items()
        provider.keys.side_effect = lambda: provider.values.keys()
        provider.__getitem__.side_effect = lambda k: provider.values[k]
        providers.append(provider)
        return provider

    async def query(source_id: str, question: str) -> str:
        return "synthetic result"
    tool = SimpleNamespace(functions=[FunctionTool(name="nl2sql.query", func=query)])

    @asynccontextmanager
    async def open_tool(**kwargs):
        connections.append(kwargs)
        yield tool

    config = await NativeConfig("https://config.example.invalid", None, None, loader).open()
    snapshot = FunctionalSnapshot.capture((await config.view()).values)
    assert "MCP_ENABLED" not in snapshot.values
    # Old serialized snapshots must not pin a stale false or old endpoint.
    old_snapshot = FunctionalSnapshot(revision=snapshot.revision, values={
        **snapshot.values, "MCP_ENABLED": False, "MCP_APP_ENDPOINT": "https://old.example.invalid",
    })
    client = RecordingClient()
    try:
        with patch("connectors.mcp_client.open_mcp_tool", open_tool):
            async with create_agent(config, client=client, snapshot=old_snapshot) as agent:
                await agent.run("Before identity setup")
                assert not connections
                remote.update({
                    "MCP_ENABLED": True, "MCP_APP_ENDPOINT": "https://mcp.example.invalid",
                    "MCP_TRUSTED_HOSTS": ["mcp.example.invalid"],
                    "MCP_SERVER_TRANSPORT": "streamable-http", "MCP_CLIENT_TIMEOUT": 90,
                    "MCP_AUTH_RESOURCE_SCOPE": "api://synthetic-mcp-api/.default",
                })
                await agent.run("After identity setup")
                assert len(connections) == 1
                assert connections[0]["endpoint"] == "https://mcp.example.invalid"
                assert connections[0]["auth_resource_scope"] == "api://synthetic-mcp-api/.default"
                assert [t.name for t in client.calls[-1][1]["tools"]] == ["nl2sql.query"]
                remote["MCP_ENABLED"] = False
                await agent.run("After disabling")
                assert len(connections) == 1
                assert not client.calls[-1][1].get("tools")
        assert len(selections) == 5 and all("MCP_API_KEY" not in keys for keys in selections)
    finally:
        await config.close()
    assert providers[0].refresh.call_count == 4
    assert all(provider.close.call_count == 1 for provider in providers)
