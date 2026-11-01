"""Request-scoped MCP clients for the local Microsoft Agent Framework strategy."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx2 as httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework._mcp import MCPTool
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity.aio import ManagedIdentityCredential, get_bearer_token_provider
from mcp.client.sse import sse_client
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from util.foundry_platform import FOUNDRY_CALL_ID_HEADER

MCPTransport = Literal["sse", "streamable_http"]
_SUPPORTED_TRANSPORTS = frozenset({"sse", "streamable_http"})
_TRANSPORT_PATHS: dict[MCPTransport, str] = {
    "sse": "sse",
    "streamable_http": "mcp",
}


class _ManagedIdentityAuth(httpx.Auth):
    """Acquire/refresh only the MCP resource token before each HTTP request."""

    def __init__(self, credential: ManagedIdentityCredential, scope: str) -> None:
        # Azure's async policy caches valid tokens and refreshes before expiry.
        # The policy and credential belong to this invocation, not a global cache.
        self._token_provider = get_bearer_token_provider(credential, scope)

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        try:
            token = await self._token_provider()
        except AzureError:
            # No request, API-key retry, or provider/token details on failure.
            raise ClientAuthenticationError("MCP managed identity token acquisition failed.") from None
        if not token:
            raise ClientAuthenticationError("MCP managed identity returned an empty token.")
        request.headers.pop("X-API-KEY", None)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class _TraceContextOnlyAsyncTransport(httpx.AsyncBaseTransport):
    """Delegate HTTPX calls while excluding ambient OpenTelemetry baggage."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        request.headers.pop("baggage", None)
        request.headers.pop("traceparent", None)
        request.headers.pop("tracestate", None)
        TraceContextTextMapPropagator().inject(request.headers)
        # The global HTTPX instrumentor would otherwise inject the default
        # composite propagator, including baggage, into the delegated request.
        with suppress_http_instrumentation():
            return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _trace_context_only_http_client_factory(
    *args: Any,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Create the SSE HTTP client without composite baggage propagation."""
    kwargs["transport"] = _TraceContextOnlyAsyncTransport()
    return httpx.AsyncClient(*args, **kwargs)


def normalize_mcp_transport(value: str | None) -> MCPTransport:
    """Validate and normalize the configured MCP transport."""

    transport = (value or "sse").strip().lower().replace("-", "_")
    if transport not in _SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"Invalid MCP_SERVER_TRANSPORT '{value}'. "
            "Supported values: sse, streamable_http."
        )
    return transport  # type: ignore[return-value]


def resolve_mcp_endpoint(endpoint: str | None, transport: str | None) -> str:
    """Append the transport path once and reject a conflicting explicit path."""

    normalized_transport = normalize_mcp_transport(transport)
    endpoint_value = (endpoint or "").strip()
    if not endpoint_value:
        raise ValueError("MCP_APP_ENDPOINT is required when the MCP strategy is enabled.")

    parsed = urlsplit(endpoint_value)
    path = parsed.path.rstrip("/")
    final_segment = path.rsplit("/", 1)[-1].lower() if path else ""
    expected_segment = _TRANSPORT_PATHS[normalized_transport]
    conflicting_segment = "mcp" if expected_segment == "sse" else "sse"

    if final_segment == conflicting_segment:
        raise ValueError(
            f"MCP_APP_ENDPOINT '{endpoint_value}' conflicts with transport "
            f"'{normalized_transport}'. Use '/{expected_segment}' for "
            f"{normalized_transport}."
        )

    if final_segment == expected_segment:
        parent_path = path.rsplit("/", 1)[0]
        path = (
            f"{parent_path}/{expected_segment}"
            if parent_path
            else f"/{expected_segment}"
        )
    else:
        path = f"{path}/{expected_segment}" if path else f"/{expected_segment}"

    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def build_mcp_headers(
    user_context: Mapping[str, Any] | None,
    api_key: str | None,
    foundry_call_id: str | None = None,
) -> dict[str, str]:
    """Create a fresh header mapping for one caller.

    ``foundry_call_id`` is echoed outbound only: it is the opaque,
    per-request call id the Foundry hosted-agent platform injects on the
    inbound ``/invocations`` request. Toolbox uses it to resolve the
    signed-in user and apply native document-level security. Inbound
    ``Authorization`` is never forwarded. Independent-resource bearer auth
    is acquired separately, never from caller/SQL/Search credentials.
    """

    headers = {"user-context": json.dumps(dict(user_context or {}))}
    if api_key:
        headers["X-API-KEY"] = api_key
    if foundry_call_id:
        headers[FOUNDRY_CALL_ID_HEADER] = foundry_call_id
    return headers


class LegacySSEMCPTool(MCPTool):
    """MAF MCP tool adapter for the MCP SDK's legacy SSE transport.

    The pinned MAF release has no public SSE tool, so this adapter subclasses its
    MCP base and supplies the SDK transport context manager.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        headers: Mapping[str, str],
        request_timeout: int,
        httpx_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            load_prompts=False,
            request_timeout=request_timeout,
        )
        self.url = url
        self.headers = dict(headers)
        self._httpx_client_factory = httpx_client_factory

    def get_mcp_client(self):
        kwargs: dict[str, Any] = {
            "url": self.url,
            "headers": self.headers,
            "timeout": float(self.request_timeout or 5),
            "sse_read_timeout": float(self.request_timeout or 300),
        }
        if self._httpx_client_factory is not None:
            kwargs["httpx_client_factory"] = self._httpx_client_factory
        return sse_client(**kwargs)


@asynccontextmanager
async def open_mcp_tool(
    *,
    endpoint: str,
    transport: str | None,
    timeout: int,
    user_context: Mapping[str, Any] | None,
    api_key: str | None,
    auth_resource_scope: str | None = None,
    foundry_call_id: str | None = None,
    http_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> AsyncIterator[MCPTool]:
    """Open one MCP tool and all of its request-scoped transport resources."""

    if timeout <= 0:
        raise ValueError("MCP_CLIENT_TIMEOUT must be greater than zero.")

    normalized_transport = normalize_mcp_transport(transport)
    url = resolve_mcp_endpoint(endpoint, normalized_transport)
    if auth_resource_scope is not None:
        scope = auth_resource_scope.strip()
        if (
            not scope.startswith("api://") or not scope.endswith("/.default")
            or not scope[6:-9] or any(c.isspace() for c in scope)
        ):
            raise ValueError("MCP_AUTH_RESOURCE_SCOPE must be a dedicated api:// resource /.default scope.")
    headers = build_mcp_headers(
        user_context, None if auth_resource_scope is not None else api_key, foundry_call_id,
    )

    async with AsyncExitStack() as resources:
        tool: MCPTool
        auth = None
        if auth_resource_scope is not None:
            credential = await resources.enter_async_context(ManagedIdentityCredential(
                client_id=os.environ.get("AZURE_CLIENT_ID") or None,
            ))
            auth = _ManagedIdentityAuth(credential, scope)

        if normalized_transport == "sse":
            TraceContextTextMapPropagator().inject(headers)
            tool = LegacySSEMCPTool(
                name="McpServerPlugin",
                url=url,
                headers=headers,
                request_timeout=timeout,
                httpx_client_factory=partial(_trace_context_only_http_client_factory, auth=auth),
            )
            await resources.enter_async_context(tool)
            yield tool
            return

        client_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": httpx.Timeout(float(timeout)),
        }
        if auth is not None:
            client_kwargs["auth"] = auth
        if http_client_factory is httpx.AsyncClient:
            client_kwargs["transport"] = _TraceContextOnlyAsyncTransport()
        http_client = await resources.enter_async_context(http_client_factory(**client_kwargs))
        tool = MCPStreamableHTTPTool(
            name="McpServerPlugin",
            url=url,
            request_timeout=timeout,
            load_prompts=False,
            http_client=http_client,
            tool_result_content="structured_first",
        )
        await resources.enter_async_context(tool)
        yield tool
