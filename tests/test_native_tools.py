"""Real MAF FunctionTool invocation reuses the existing NL2SQL/Fabric behavior."""

import pickle
import asyncio
import threading
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message

from native.agent import create_agent
from native.config import ConfigView
from native.tools import ToolIdentity, compose_tools, nl2sql_tools
from test_native_runtime import fake_config
from test_audit_emitter import capture_audit_logs


VALUES = {
    "NL2SQL_ENABLED": True, "NL2SQL_SOURCE_REGISTRY": "cosmos",
    "DATABASE_ACCOUNT_NAME": "synthetic", "DATABASE_NAME": "metadata",
}


@pytest.fixture
def registry():
    return SimpleNamespace(
        list_documents=AsyncMock(return_value=[{
            "id": "sales", "description": "Synthetic sales", "type": "sql_database",
        }]),
        get_document=AsyncMock(return_value={
            "id": "sales", "description": "Synthetic sales", "type": "sql_database",
            "server": "sql.example.invalid", "database": "sales",
        }),
        close=AsyncMock(),
    )


async def configured_tools(resources, registry, values=None, identity=None):
    with patch("native.tools.SourceRegistry", return_value=registry):
        tools = await nl2sql_tools(
            ConfigView({**VALUES, **(values or {})}, MagicMock()),
            identity or ToolIdentity(), resources,
        )
    return {tool.name: tool for tool in tools}


async def test_listing_sources_never_constructs_query_or_embedding_clients(registry):
    with (
        patch("plugins.nl2sql.plugin.SQLDBClient") as sql,
        patch("plugins.nl2sql.plugin.SQLEndpointClient") as fabric,
        patch("native.tools.MetadataSearch.search", side_effect=AssertionError("No Search access")),
        patch("native.tools.Embeddings.get_embeddings", side_effect=AssertionError("No model access")),
    ):
        async with AsyncExitStack() as resources:
            tools = await configured_tools(resources, registry)
            result = await tools["get_all_datasources_info"].invoke(skip_parsing=True)
            assert result.datasources[0].name == "sales"
            sql.assert_not_called()
            fabric.assert_not_called()
    registry.close.assert_awaited_once()


@pytest.mark.parametrize("query", [
    "SELECT 1; DELETE FROM sales", "SELECT * INTO saved FROM sales",
    "SELECT * FROM OPENQUERY(linked, 'SELECT 1')", "EXEC dbo.proc",
])
async def test_native_query_tool_reuses_actual_read_only_controls_before_registry_access(registry, query):
    async with AsyncExitStack() as resources:
        tools = await configured_tools(resources, registry)
        result = await tools["execute_sql_query"].invoke(
            arguments={"datasource": "sales", "query": query}, skip_parsing=True,
        )
        assert result.error and result.results is None
        registry.get_document.assert_not_awaited()


@pytest.mark.parametrize("rows,error", [([(1,)], False), ([(1,), (2,), (3,)], True)])
async def test_native_query_timeout_row_limit_and_cleanup(registry, rows, error):
    cursor = MagicMock()
    cursor.description = [("value",)]
    cursor.fetchmany.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value = cursor
    with patch("plugins.nl2sql.plugin.SQLDBClient", return_value=SimpleNamespace(
        create_connection=AsyncMock(return_value=connection),
    )):
        async with AsyncExitStack() as resources:
            tools = await configured_tools(resources, registry, {"NL2SQL_MAX_ROWS": 2})
            result = await tools["execute_sql_query"].invoke(
                arguments={"datasource": "sales", "query": "SELECT value FROM sales"}, skip_parsing=True,
            )
    assert bool(result.error) is error
    assert connection.timeout == 30
    cursor.fetchmany.assert_called_once_with(3)
    cursor.fetchall.assert_not_called()
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


async def test_native_query_cancellation_never_closes_a_running_odbc_handle(registry):
    executing, release = threading.Event(), threading.Event()
    cursor = MagicMock()
    cursor.description = [("value",)]
    cursor.fetchmany.return_value = [(1,)]

    def execute(query):
        executing.set()
        assert release.wait(5), "The test must release its bounded synthetic ODBC operation."
        cursor.close.assert_not_called()

    cursor.execute.side_effect = execute
    connection = MagicMock()
    connection.cursor.return_value = cursor
    with patch("plugins.nl2sql.plugin.SQLDBClient", return_value=SimpleNamespace(
        create_connection=AsyncMock(return_value=connection),
    )):
        async with AsyncExitStack() as resources:
            tools = await configured_tools(resources, registry)
            task = asyncio.create_task(tools["execute_sql_query"].invoke(
                arguments={"datasource": "sales", "query": "SELECT value FROM sales"}, skip_parsing=True,
            ))
            try:
                assert await asyncio.to_thread(executing.wait, 5)
                task.cancel()
                await asyncio.sleep(0)
                cursor.close.assert_not_called()
                connection.close.assert_not_called()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


async def test_dax_never_accepts_model_supplied_tokens_and_missing_delegation_fails_closed(registry):
    async with AsyncExitStack() as resources:
        tools = await configured_tools(resources, registry)
        schema = tools["execute_dax_query"].input_model.model_json_schema()
        assert set(schema["properties"]) == {"datasource", "query"}
        result = await tools["execute_dax_query"].invoke(
            arguments={"datasource": "sales", "query": "EVALUATE Sales"}, skip_parsing=True,
        )
        assert result.error and "authorization" in result.error
        registry.get_document.assert_not_awaited()


async def test_dax_adapter_keeps_invocation_local_user_token(registry):
    registry.get_document.return_value = {
        "id": "sales", "description": "Synthetic", "type": "semantic_model",
        "organization": "synthetic", "dataset": "dataset", "workspace": "workspace",
        "tenant_id": "tenant", "client_id": "client",
    }
    adapter = SimpleNamespace(execute_restapi_dax_query=AsyncMock(return_value=[{"value": 1}]))
    with patch("plugins.nl2sql.plugin.SemanticModelClient", return_value=adapter):
        async with AsyncExitStack() as resources:
            tools = await configured_tools(resources, registry, identity=ToolIdentity(dax_token="synthetic-token"))
            result = await tools["execute_dax_query"].invoke(
                arguments={"datasource": "sales", "query": "EVALUATE Sales"}, skip_parsing=True,
            )
    assert result.results == [{"value": 1}]
    adapter.execute_restapi_dax_query.assert_awaited_once_with(
        dax_query="EVALUATE Sales", user_token="synthetic-token",
    )


def test_invocation_credentials_cannot_be_checkpointed():
    with pytest.raises(TypeError, match="cannot be persisted"):
        pickle.dumps(ToolIdentity(dax_token="synthetic-token"))


async def test_disabled_tools_do_not_construct_optional_dependencies():
    with patch("native.tools.nl2sql_tools") as sql:
        async with AsyncExitStack() as resources:
            assert await compose_tools(ConfigView({}), ToolIdentity(), resources) == []
        sql.assert_not_called()


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


async def test_native_agent_really_invokes_the_reused_source_tool(registry):
    class ToolCallingClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(messages=[Message("assistant", [
                    Content.from_function_call("synthetic-call", "get_all_datasources_info", arguments={}),
                ])])
            assert any(
                content.type == "function_result" and "sales" in str(content.result)
                for message in messages for content in message.contents
            )
            return ChatResponse(messages=[Message("assistant", ["The configured source is sales."])])

    model = ToolCallingClient()
    config = fake_config(VALUES, mode="conversational")
    with patch("native.tools.SourceRegistry", return_value=registry):
        async with create_agent(config, client=model) as agent:
            response = await agent.run("List configured data sources.")
    assert response.text == "The configured source is sales."
    assert model.calls == 2
    registry.close.assert_awaited_once()


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


async def test_native_audit_uses_current_enabled_settings_without_exporting_sql_or_hmac(registry):
    async with AsyncExitStack() as resources:
        tools = await configured_tools(resources, registry, {
            "AUDIT_EVENTS_ENABLED": True, "AUDIT_HMAC_KEY": "78" * 32,
            "AUDIT_HMAC_KEY_ID": "synthetic-key-id",
        })
        with capture_audit_logs() as capture:
            await tools["execute_sql_query"].invoke(
                arguments={"datasource": "sales", "query": "DELETE FROM synthetic-private-table"},
                skip_parsing=True,
            )
    assert {record.event_type for record in capture.records} >= {
        "tool.invocation.started", "tool.invocation.completed", "grounding.source.rejected",
    }
    assert all("synthetic-private-table" not in str(vars(record)) for record in capture.records)
    assert all("78" * 32 not in str(vars(record)) for record in capture.records)


async def test_native_tool_set_shares_the_existing_audit_budget(registry):
    async with AsyncExitStack() as resources:
        tools = await configured_tools(resources, registry, {
            "AUDIT_EVENTS_ENABLED": True, "AUDIT_HMAC_KEY": "78" * 32,
        })
        with capture_audit_logs() as capture:
            for _ in range(20):
                await tools["get_all_datasources_info"].invoke(skip_parsing=True)
    assert sum(record.event_type == "tool.invocation.started" for record in capture.records) == 16
    assert sum(record.event_type == "tool.invocation.completed" for record in capture.records) == 16
