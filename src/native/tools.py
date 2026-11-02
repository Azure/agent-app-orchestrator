"""Optional native retrieval and request-scoped remote MCP tool composition."""

import functools
import json
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
    search_token: str | None = field(default=None, repr=False)

    def __reduce__(self) -> Any:
        raise TypeError("Invocation identity cannot be persisted.")


def required(config: ConfigView, key: str) -> str:
    value = config.get(key)
    if not value:
        raise ValueError(f"{key} is required by the enabled capability.")
    return value


class SourceRegistry:
    """Shared Cosmos source documents; SQL catalogs are owned by the MCP server."""

    def __init__(self, config: ConfigView):
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
    """Shared Search adapter for non-SQL retrieval."""

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
                return result
            finally:
                end_audit_request(token)
    return FunctionTool(name=method.__name__, func=invoke)


def multimodal_tool(config: ConfigView, identity: ToolIdentity, audit_context: Any = None) -> FunctionTool:
    from plugins.retrieval.plugin import RetrievalPlugin
    from plugins.retrieval.retrieval_types import MultimodalVectorIndexRetrievalResult

    # Native document permissions are carried by an already-issued credential.
    # Neither a model argument nor the platform's opaque partition ID supplies ACLs.
    async def multimodal_retrieve(input: str) -> MultimodalVectorIndexRetrievalResult:
        """Retrieve authorized document text, images and captions from the configured index."""
        from azure.identity.aio import DefaultAzureCredential

        if not identity.search_token:
            raise ValueError(
                "Multimodal retrieval requires invocation-local delegated Search authorization (OBO). "
                "Enable user delegation for this application before using this tool."
            )
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
    if config.get("HOSTED_AGENT_AUTH_MODE", "user_delegated") == "service_identity" and identity.search_token:
        raise ValueError("service_identity cannot accept delegated Search credentials or use an OBO fallback.")
    tools: list[Any] = []
    audit_context = new_audit_context(config)
    if enabled(config.get("SEARCH_RETRIEVAL_ENABLED", False, type=None)):
        tools.append(FoundryChatClient.get_azure_ai_search_tool(
            index_connection_id=required(config, "SEARCH_CONNECTION_ID"),
            index_name=required(config, "SEARCH_RAG_INDEX_NAME"),
            top_k=config.get("SEARCH_RAGINDEX_TOP_K", 3, type=int),
        ))
    if enabled(config.get("MULTIMODAL_ENABLED", False, type=None)):
        tools.append(multimodal_tool(config, identity, audit_context))
    if enabled(config.get("MCP_ENABLED", False, type=None)):
        from connectors.mcp_client import open_mcp_tool

        endpoint = https_endpoint(required(config, "MCP_APP_ENDPOINT"), "MCP_APP_ENDPOINT")
        raw_trusted = config.get("MCP_TRUSTED_HOSTS", type=None)
        if isinstance(raw_trusted, str):
            raw_trusted = (
                json.loads(raw_trusted) if raw_trusted.lstrip().startswith("[")
                else raw_trusted.split(",")
            )
        if not isinstance(raw_trusted, list) or not raw_trusted or any(
            not isinstance(host, str) or not host.strip() for host in raw_trusted
        ):
            raise ValueError("MCP_TRUSTED_HOSTS requires a non-empty hostname list.")
        trusted = {host.strip().lower() for host in raw_trusted}
        if urlsplit(endpoint).hostname not in trusted:
            raise ValueError("MCP_APP_ENDPOINT is not in MCP_TRUSTED_HOSTS.")
        mcp_tool = await resources.enter_async_context(open_mcp_tool(
            endpoint=endpoint, transport=config.get("MCP_SERVER_TRANSPORT", "streamable_http"),
            timeout=config.get("MCP_CLIENT_TIMEOUT", 30, type=int),
            user_context=None, api_key=config.get("MCP_API_KEY"),
            auth_resource_scope=config.get("MCP_AUTH_RESOURCE_SCOPE"),
            foundry_call_id=identity.call_id,
        ))
        # The invocation's resource stack owns the connection. Give MAF the
        # discovered functions, not a context manager retained by the Agent.
        tools.extend(mcp_tool.functions)
    return tools
