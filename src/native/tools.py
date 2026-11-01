"""Optional tool composition, reusing NL2SQL's catalog, guards and SQL/Fabric adapters."""

import functools
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from agent_framework import FunctionTool
from agent_framework_foundry import FoundryChatClient

from .config import ConfigView, config_scope, enabled, https_endpoint
from .bootstrap import service_version


@dataclass(frozen=True)
class ToolIdentity:
    """Invocation-local credentials; never include in a business input/checkpoint."""

    call_id: str | None = field(default=None, repr=False)
    dax_token: str | None = field(default=None, repr=False)
    search_token: str | None = field(default=None, repr=False)

    def __reduce__(self) -> Any:
        raise TypeError("Invocation identity cannot be persisted.")


def required(config: ConfigView, key: str) -> str:
    value = config.get(key)
    if not value:
        raise ValueError(f"{key} is required by the enabled capability.")
    return value


class SourceRegistry:
    """The supported existing Cosmos datasource registry, owned only by NL2SQL."""

    def __init__(self, config: ConfigView):
        if required(config, "NL2SQL_SOURCE_REGISTRY") != "cosmos":
            raise ValueError("NL2SQL_SOURCE_REGISTRY supports only the existing cosmos catalog.")
        required(config, "DATABASE_ACCOUNT_NAME")
        required(config, "DATABASE_NAME")
        self.config = config
        self.client: Any = None

    def _client(self) -> Any:
        if self.client is None:
            from connectors.cosmosdb import CosmosDBClient
            with config_scope(self.config):
                self.client = CosmosDBClient()
        return self.client

    async def list_documents(self, container: str) -> list[dict[str, Any]]:
        return await self._client().list_documents(container)

    async def get_document(self, container: str, key: str) -> dict[str, Any] | None:
        return await self._client().get_document(container, key)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()


class MetadataSearch:
    """Metadata-only Search adapter. Listing SQL sources never constructs it."""

    def __init__(self, config: ConfigView):
        self.config = config
        self.client: Any = None

    async def search(
        self, *, index_name: str, body: dict[str, Any], search_user_token: str | None = None,
    ) -> dict[str, Any]:
        if self.client is None:
            from connectors.search import SearchClient
            https_endpoint(required(self.config, "SEARCH_SERVICE_QUERY_ENDPOINT"), "SEARCH_SERVICE_QUERY_ENDPOINT")
            with config_scope(self.config):
                self.client = SearchClient()
        with config_scope(self.config):
            if search_user_token is not None:
                return await self.client.search(index_name, body, search_user_token=search_user_token)
            return await self.client.search(index_name, body)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()


class Embeddings:
    def __init__(self, config: ConfigView):
        self.config = config

    async def get_embeddings(self, text: str) -> list[float]:
        from azure.identity.aio import get_bearer_token_provider
        from openai import AsyncAzureOpenAI

        async with AsyncAzureOpenAI(
            azure_endpoint=https_endpoint(required(self.config, "AI_FOUNDRY_ACCOUNT_ENDPOINT"),
                                          "AI_FOUNDRY_ACCOUNT_ENDPOINT"),
            api_version=self.config.get("OPENAI_API_VERSION", "2025-04-01-preview"),
            azure_ad_token_provider=get_bearer_token_provider(
                self.config.aiocredential, "https://cognitiveservices.azure.com/.default",
            ),
            timeout=30, max_retries=2,
        ) as client:
            result = await client.embeddings.create(
                model=required(self.config, "EMBEDDING_DEPLOYMENT_NAME"), input=text,
            )
        return result.data[0].embedding


def new_audit_context(config: ConfigView) -> Any:
    if not enabled(config.get("AUDIT_EVENTS_ENABLED", False, type=None)):
        return None
    from telemetry.audit import begin_audit_request, end_audit_request
    context, token = begin_audit_request()
    end_audit_request(token)
    return context


def audited_function(method: Any, config: ConfigView, audit_context: Any = None) -> FunctionTool:
    # Preserve the original tool signature/schema; never expose credentials as arguments.
    @functools.wraps(method)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from telemetry.audit import AuditEmitter, begin_audit_request, end_audit_request, invoke_audited_tool
        from telemetry.audit_contract import AuditSettings

        with config_scope(config):
            emitter = AuditEmitter(
                AuditSettings.from_config(config), service_name="agent-app-orchestrator",
                service_version=service_version(),
                environment="unknown",
            )
            _, token = begin_audit_request(context=audit_context)
            try:
                result = await invoke_audited_tool(
                    method.__name__, lambda: method(*args, **kwargs),
                    tool_kind="in_process", sensitive_arguments=kwargs, emitter=emitter,
                )
                if kwargs.get("datasource"):
                    emitter.emit_source(
                        selected=not getattr(result, "error", None),
                        source_type="nl2sql_datasource", source_reference=kwargs["datasource"],
                    )
                return result
            finally:
                end_audit_request(token)
    return FunctionTool(name=method.__name__, func=invoke)


async def nl2sql_tools(
    config: ConfigView, identity: ToolIdentity, resources: AsyncExitStack, audit_context: Any = None,
) -> list[FunctionTool]:
    from azure.identity.aio import DefaultAzureCredential
    from plugins.nl2sql.plugin import NL2SQLPlugin
    from plugins.nl2sql.nl2sql_types import ExecuteQueryResult

    credential = await resources.enter_async_context(DefaultAzureCredential())
    view = ConfigView(config.values, config.credential, credential)
    registry = SourceRegistry(view)
    resources.push_async_callback(registry.close)
    search = MetadataSearch(view)
    resources.push_async_callback(search.close)
    plugin = NL2SQLPlugin(
        config=view, source_registry=registry, search=search, model=Embeddings(view),
        query_timeout_seconds=config.get("NL2SQL_QUERY_TIMEOUT_SECONDS", 30, type=int),
        max_rows=config.get("NL2SQL_MAX_ROWS", 1000, type=int),
    )
    audit_context = audit_context if audit_context is not None else new_audit_context(config)

    async def execute_dax_query(datasource: str, query: str) -> ExecuteQueryResult:
        """Query a configured semantic model using invocation-local delegated authorization."""
        if not identity.dax_token:
            return ExecuteQueryResult(error="Delegated DAX authorization is unavailable for this invocation.")
        return await plugin.execute_dax_query(datasource, query, identity.dax_token)

    return [
        audited_function(method, view, audit_context) for method in (
            plugin.get_all_datasources_info, plugin.get_all_tables_info, plugin.get_schema_info,
            plugin.tables_retrieval, plugin.measures_retrieval, plugin.queries_retrieval,
            plugin.validate_sql_query, plugin.execute_sql_query, execute_dax_query,
        )
    ]


def multimodal_tool(config: ConfigView, identity: ToolIdentity, audit_context: Any = None) -> FunctionTool:
    from plugins.retrieval.plugin import RetrievalPlugin
    from plugins.retrieval.retrieval_types import MultimodalVectorIndexRetrievalResult

    # Native document permissions are carried by an already-issued credential.
    # Neither a model argument nor the platform's opaque partition ID supplies ACLs.
    async def multimodal_retrieve(input: str) -> MultimodalVectorIndexRetrievalResult:
        """Retrieve authorized document text, images and captions from the configured index."""
        from azure.identity.aio import DefaultAzureCredential

        if not identity.search_token:
            raise ValueError("Multimodal retrieval requires invocation-local delegated Search authorization.")
        delegated_token = identity.search_token

        class AuthorizedMultimodal(RetrievalPlugin):
            async def _get_azure_search_token(self) -> str:
                return delegated_token

            async def _perform_search(self, url: str, headers: dict, body: dict) -> dict:
                # Native Search owns permissions; never replace a missing user token
                # with a group filter or retry after stripping source authorization.
                body.pop("filter", None)
                return await search.search(
                    index_name=view.get("SEARCH_RAG_INDEX_NAME", "ragindex"), body=body,
                    search_user_token=delegated_token,
                )

        async with DefaultAzureCredential() as credential:
            view = ConfigView(config.values, config.credential, credential)
            search = MetadataSearch(view)
            try:
                with config_scope(view):
                    plugin = AuthorizedMultimodal(config=view, model=Embeddings(view))
                    return await plugin.multimodal_vector_index_retrieve(input)
            finally:
                await search.close()

    return audited_function(multimodal_retrieve, config, audit_context)


async def compose_tools(
    config: ConfigView, identity: ToolIdentity, resources: AsyncExitStack,
) -> list[Any]:
    tools: list[Any] = []
    audit_context = new_audit_context(config)
    if enabled(config.get("NL2SQL_ENABLED", False, type=None)):
        tools.extend(await nl2sql_tools(config, identity, resources, audit_context))
    if enabled(config.get("SEARCH_RETRIEVAL_ENABLED", False, type=None)):
        tools.append(FoundryChatClient.get_azure_ai_search_tool(
            index_connection_id=required(config, "SEARCH_CONNECTION_ID"),
            index_name=required(config, "SEARCH_RAG_INDEX_NAME"),
            top_k=config.get("SEARCH_RAGINDEX_TOP_K", 3, type=int),
        ))
    if enabled(config.get("MULTIMODAL_ENABLED", False, type=None)):
        tools.append(multimodal_tool(config, identity, audit_context))
    if enabled(config.get("FOUNDRY_IQ_ENABLED", False, type=None)):
        # Provisioning owns the KB/Toolbox connection, source grants and source auth.
        # A connection ID is never invented from a user or request metadata.
        tools.append(FoundryChatClient.get_mcp_tool(
            name="foundry-iq", project_connection_id=required(config, "KNOWLEDGE_BASE_CONNECTION_ID"),
            approval_mode="never_require",
        ))
    if enabled(config.get("MCP_ENABLED", False, type=None)):
        from connectors.mcp_client import open_mcp_tool

        endpoint = https_endpoint(required(config, "MCP_APP_ENDPOINT"), "MCP_APP_ENDPOINT")
        trusted = {s.strip().lower() for s in required(config, "MCP_TRUSTED_HOSTS").split(",")}
        if urlsplit(endpoint).hostname not in trusted:
            raise ValueError("MCP_APP_ENDPOINT is not in MCP_TRUSTED_HOSTS.")
        tools.append(await resources.enter_async_context(open_mcp_tool(
            endpoint=endpoint, transport=config.get("MCP_SERVER_TRANSPORT", "streamable_http"),
            timeout=config.get("MCP_CLIENT_TIMEOUT", 30, type=int),
            user_context=None, api_key=config.get("MCP_API_KEY"),
            foundry_call_id=identity.call_id,
        )))
    return tools
