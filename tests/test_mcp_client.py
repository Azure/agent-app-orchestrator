import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2 as httpx
import jwt
import pytest
from agent_framework import Content
from agent_framework.exceptions import ToolExecutionException
from azure.core.credentials import AccessTokenInfo
from azure.core.exceptions import ClientAuthenticationError
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from connectors import mcp_client
from connectors.mcp_client import (
    LegacySSEMCPTool,
    build_mcp_headers,
    normalize_mcp_transport,
    open_mcp_tool,
    resolve_mcp_endpoint,
)


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (None, "sse"),
        ("sse", "sse"),
        (" SSE ", "sse"),
        ("streamable_http", "streamable_http"),
        ("streamable-http", "streamable_http"),
    ],
)
def test_normalize_mcp_transport(transport, expected):
    assert normalize_mcp_transport(transport) == expected


def test_normalize_mcp_transport_rejects_unsupported_value():
    with pytest.raises(
        ValueError,
        match="Supported values: sse, streamable_http",
    ):
        normalize_mcp_transport("websocket")


@pytest.mark.parametrize(
    ("endpoint", "transport", "expected"),
    [
        ("https://example.test", "sse", "https://example.test/sse"),
        ("https://example.test/", "sse", "https://example.test/sse"),
        ("https://example.test/sse", "sse", "https://example.test/sse"),
        ("https://example.test/sse/", "sse", "https://example.test/sse"),
        (
            "https://example.test/api?tenant=one",
            "streamable_http",
            "https://example.test/api/mcp?tenant=one",
        ),
        (
            "https://example.test/api/mcp",
            "streamable_http",
            "https://example.test/api/mcp",
        ),
        (
            "https://example.test/api/MCP/",
            "streamable_http",
            "https://example.test/api/mcp",
        ),
    ],
)
def test_resolve_mcp_endpoint_is_idempotent(
    endpoint,
    transport,
    expected,
):
    assert resolve_mcp_endpoint(endpoint, transport) == expected


@pytest.mark.parametrize(
    ("endpoint", "transport", "expected_path"),
    [
        ("https://example.test/mcp", "sse", "/sse"),
        ("https://example.test/sse", "streamable_http", "/mcp"),
    ],
)
def test_resolve_mcp_endpoint_rejects_conflicting_path(
    endpoint,
    transport,
    expected_path,
):
    with pytest.raises(ValueError, match=expected_path):
        resolve_mcp_endpoint(endpoint, transport)


def test_build_mcp_headers_only_sets_request_identity_and_optional_key():
    headers = build_mcp_headers({"principal_id": "user-1"}, "secret")

    assert json.loads(headers["user-context"]) == {"principal_id": "user-1"}
    assert headers["X-API-KEY"] == "secret"
    assert "Content-Type" not in headers
    assert "Accept" not in headers
    assert build_mcp_headers(None, None) == {"user-context": "{}"}


def test_build_mcp_headers_echoes_foundry_call_id_when_present():
    """The opaque Foundry call id is echoed outbound so Toolbox can resolve
    the signed-in user's identity (ADR-0001 / Azure/GPT-RAG#591)."""

    headers = build_mcp_headers(
        {"principal_id": "user-1"},
        "secret",
        foundry_call_id="call-abc-123",
    )

    assert headers["x-agent-foundry-call-id"] == "call-abc-123"
    # No caller/model identity or Authorization ever gets echoed outbound.
    assert "Authorization" not in headers
    assert "x-agent-user-id" not in headers
    assert "x-client" not in headers


@pytest.mark.parametrize("foundry_call_id", [None, ""])
def test_build_mcp_headers_omits_foundry_call_id_when_absent(foundry_call_id):
    headers = build_mcp_headers(
        {"principal_id": "user-1"}, "secret", foundry_call_id=foundry_call_id
    )

    assert "x-agent-foundry-call-id" not in headers


def test_legacy_sse_adapter_uses_request_scoped_headers(monkeypatch):
    captured = {}

    @asynccontextmanager
    async def fake_sse_client(**kwargs):
        captured.update(kwargs)
        yield object(), object()

    monkeypatch.setattr(mcp_client, "sse_client", fake_sse_client)
    tool = LegacySSEMCPTool(
        name="test",
        url="https://example.test/sse",
        headers={"user-context": '{"principal_id": "one"}'},
        request_timeout=42,
    )

    tool.get_mcp_client()

    assert captured == {}
    context_manager = tool.get_mcp_client()

    async def enter_context():
        async with context_manager:
            pass

    asyncio.run(enter_context())
    assert captured["headers"] == {
        "user-context": '{"principal_id": "one"}',
    }
    assert captured["timeout"] == 42.0
    assert captured["sse_read_timeout"] == 42.0


@pytest.mark.asyncio
async def test_streamable_http_clients_isolate_concurrent_users(monkeypatch):
    clients = []
    tools = []

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            self.headers = kwargs["headers"]
            self.timeout = kwargs["timeout"]
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            self.closed = True

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.http_client = kwargs["http_client"]
            self.functions = []
            self.closed = False
            tools.append(self)

        async def __aenter__(self):
            await asyncio.sleep(0)
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            self.closed = True

    monkeypatch.setattr(
        mcp_client,
        "MCPStreamableHTTPTool",
        FakeMCPTool,
    )

    async def connect(principal_id):
        async with open_mcp_tool(
            endpoint="https://example.test",
            transport="streamable_http",
            timeout=30,
            user_context={"principal_id": principal_id},
            api_key=f"key-{principal_id}",
            http_client_factory=FakeHTTPClient,
        ):
            await asyncio.sleep(0)

    await asyncio.gather(connect("one"), connect("two"))

    assert len(clients) == 2
    assert clients[0].headers is not clients[1].headers
    observed_contexts = {
        json.loads(client.headers["user-context"])["principal_id"]
        for client in clients
    }
    assert observed_contexts == {"one", "two"}
    assert {client.headers["X-API-KEY"] for client in clients} == {
        "key-one",
        "key-two",
    }
    assert all(client.closed for client in clients)
    assert all(tool.closed for tool in tools)


@pytest.mark.asyncio
async def test_streamable_http_clients_isolate_concurrent_foundry_call_ids(
    monkeypatch,
):
    """Two concurrent Toolbox-bound requests must never cross-contaminate
    each other's per-request call id (ADR-0001 / Azure/GPT-RAG#591)."""

    clients = []

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            self.headers = kwargs["headers"]

        async def __aenter__(self):
            clients.append(self)
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.functions = []

        async def __aenter__(self):
            await asyncio.sleep(0)
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(mcp_client, "MCPStreamableHTTPTool", FakeMCPTool)

    async def connect(call_id):
        async with open_mcp_tool(
            endpoint="https://example.test",
            transport="streamable_http",
            timeout=30,
            user_context={"principal_id": call_id},
            api_key=None,
            foundry_call_id=call_id,
            http_client_factory=FakeHTTPClient,
        ):
            await asyncio.sleep(0)

    await asyncio.gather(connect("call-one"), connect("call-two"))

    assert len(clients) == 2
    observed = {
        client.headers["x-agent-foundry-call-id"] for client in clients
    }
    assert observed == {"call-one", "call-two"}
    assert clients[0].headers is not clients[1].headers


@pytest.mark.asyncio
async def test_streamable_http_cleanup_runs_when_caller_fails(monkeypatch):
    state = {"client_closed": False, "tool_closed": False}

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            state["client_closed"] = True

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.functions = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            state["tool_closed"] = True

    monkeypatch.setattr(
        mcp_client,
        "MCPStreamableHTTPTool",
        FakeMCPTool,
    )

    with pytest.raises(RuntimeError, match="caller failed"):
        async with open_mcp_tool(
            endpoint="https://example.test",
            transport="streamable_http",
            timeout=30,
            user_context={},
            api_key=None,
            http_client_factory=FakeHTTPClient,
        ):
            raise RuntimeError("caller failed")

    assert state == {"client_closed": True, "tool_closed": True}


class _CaptureHeaders:
    def __init__(self, app):
        self.app = app
        self.requests = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.requests.append(
                {
                    key.decode().lower(): value.decode()
                    for key, value in scope["headers"]
                }
            )
        await self.app(scope, receive, send)


@pytest.mark.asyncio
async def test_streamable_http_in_process_transport_contract():
    # Stateless ASGI transport cannot hold the subscription stream open.
    # Disable that optional server capability using the published API.
    server = MCPServer("contract-test", subscriptions=False)

    @server.tool()
    def echo(text: str) -> str:
        return text

    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    captured_app = _CaptureHeaders(app)

    def client_factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=captured_app),
            base_url="http://test",
            **kwargs,
        )

    async with app.router.lifespan_context(app):
        async with open_mcp_tool(
            endpoint="http://test",
            transport="streamable_http",
            timeout=30,
            user_context={"principal_id": "contract-user"},
            api_key="contract-key",
            http_client_factory=client_factory,
        ) as tool:
            assert [function.name for function in tool.functions] == ["echo"]
            result = await tool.call_tool("echo", text="hello")

    assert any(
        request["user-context"] == '{"principal_id": "contract-user"}'
        and request["x-api-key"] == "contract-key"
        for request in captured_app.requests
    )
    assert isinstance(result[0], Content) and result[0].type == "text"
    assert json.loads(result[0].text) == {"result": "hello"}


MCP_API_CLIENT_ID = "00000000-0000-0000-0000-000000000001"
TENANT_ID = "00000000-0000-0000-0000-000000000002"
SCOPE = f"api://{MCP_API_CLIENT_ID}/.default"
TEST_SIGNING_KEY = "synthetic-local-fixture-signing-key-only"
V2_ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
QUERY_RESULT = {
    "schema_version": 1, "source_id": "sales", "query_id": "query-1",
    "metadata_version": "catalog-1",
    "columns": [
        {"name": "id", "type": "integer"}, {"name": "amount", "type": "decimal"},
        {"name": "date", "type": "date"}, {"name": "data", "type": "binary"},
        {"name": "active", "type": "boolean"}, {"name": "missing", "type": "string"},
    ],
    "rows": [[1, "12.30", "2026-11-01", "AQ==", True, None]], "row_count": 1,
}


@pytest.fixture
def managed_identity(monkeypatch):
    # Local token fixture models the dedicated resource's v2 client-ID audience.
    # Real Entra issuance/version is owned by the parent API registration.
    bearer = jwt.encode({
        "aud": MCP_API_CLIENT_ID, "iss": V2_ISSUER, "ver": "2.0",
        "azp": "synthetic-publication-client", "roles": ["MCP.Invoke"],
        "exp": int(time.time()) + 3600,
    }, TEST_SIGNING_KEY, algorithm="HS256")
    credential = SimpleNamespace(
        get_token_info=AsyncMock(return_value=AccessTokenInfo(bearer, int(time.time()) + 3600)),
        __aenter__=AsyncMock(), __aexit__=AsyncMock(),
    )
    credential.bearer = bearer
    # Special methods must live on the class for async context managers.
    class Credential:
        get_token_info = credential.get_token_info

        async def __aenter__(self):
            await credential.__aenter__()
            return self

        async def __aexit__(self, *args):
            await credential.__aexit__(*args)

    created = []
    def factory(**kwargs):
        created.append(kwargs)
        return Credential()

    monkeypatch.setattr(mcp_client, "ManagedIdentityCredential", factory)
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-managed-identity")
    credential.created = created
    return credential


@pytest.mark.parametrize("mode", ["structured", "structured_only", "text", "error"])
async def test_official_protocol_nl2sql_result_mapping_and_resource_bearer(mode, managed_identity):
    server = MCPServer("independent-sql", subscriptions=False)

    @server.tool(name="nl2sql.list_sources")
    def list_sources() -> dict:
        return {"sources": [{"source_id": "sales", "name": "Sales"}]}

    @server.tool(name="nl2sql.query")
    def query(source_id: str, question: str) -> types.CallToolResult:
        assert source_id == "sales" and question == "Total sales?"
        return types.CallToolResult(
            content=[] if mode == "structured_only" else [
                types.TextContent(type="text", text=(
                    "Query rejected" if mode == "error" else
                    json.dumps(QUERY_RESULT) if mode == "text" else "Human-readable summary"
                )),
            ],
            structuredContent=QUERY_RESULT if mode in ("structured", "structured_only") else None,
            isError=mode == "error",
        )

    app = server.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["testserver"], allowed_origins=["https://testserver"],
        ),
    )
    captured = _CaptureHeaders(app)
    clients = []
    def client_factory(**kwargs):
        client = httpx.AsyncClient(
            transport=mcp_client._TraceContextOnlyAsyncTransport(httpx.ASGITransport(app=captured)),
            **kwargs,
        )
        clients.append(client)
        return client

    async with app.router.lifespan_context(app):
        async with open_mcp_tool(
            endpoint="https://testserver", transport="streamable-http", timeout=90,
            user_context={"principal_id": "synthetic-user"}, foundry_call_id="synthetic-call",
            api_key="must-not-be-sent", auth_resource_scope=SCOPE,
            http_client_factory=client_factory,
        ) as tool:
            assert len(tool.functions) == 2
            assert {f.name for f in tool.functions} == {"nl2sql.list_sources", "nl2sql.query"}
            assert json.loads((await tool.call_tool("nl2sql.list_sources"))[0].text)["sources"][0]["source_id"] == "sales"
            if mode == "error":
                with pytest.raises(ToolExecutionException, match="Query rejected"):
                    await tool.call_tool("nl2sql.query", source_id="sales", question="Total sales?")
            else:
                function = next(f for f in tool.functions if f.name == "nl2sql.query")
                result = await function.invoke(
                    arguments={"source_id": "sales", "question": "Total sales?"},
                    skip_parsing=True,
                )
                assert len(result) == 1 and isinstance(result[0], Content)
                assert json.loads(result[0].text) == QUERY_RESULT

    assert captured.requests
    assert all(r["authorization"] == f"Bearer {managed_identity.bearer}" for r in captured.requests)
    for request in captured.requests:
        claims = jwt.decode(
            request["authorization"].removeprefix("Bearer "), TEST_SIGNING_KEY,
            algorithms=["HS256"], audience=MCP_API_CLIENT_ID, issuer=V2_ISSUER,
        )
        assert claims["ver"] == "2.0" and claims["roles"] == ["MCP.Invoke"]
        assert claims["aud"] != SCOPE  # v2 aud is the API client ID, not the scope URI.
    assert all("x-api-key" not in r and "baggage" not in r for r in captured.requests)
    assert all(r["x-agent-foundry-call-id"] == "synthetic-call" for r in captured.requests)
    assert all(json.loads(r["user-context"]) == {"principal_id": "synthetic-user"} for r in captured.requests)
    managed_identity.get_token_info.assert_awaited_once()
    assert managed_identity.get_token_info.await_args.args == (SCOPE,)
    assert managed_identity.created == [{"client_id": "synthetic-managed-identity"}]
    managed_identity.__aexit__.assert_awaited_once()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("outcome", ["success", "caller_failure", "cancelled", "expired", "auth_failure", "unexpected"])
async def test_bearer_refresh_failure_and_cleanup_before_network(outcome, managed_identity, monkeypatch):
    requests, clients, closed = [], [], []
    now = int(time.time())
    if outcome == "expired":
        managed_identity.get_token_info.side_effect = [
            AccessTokenInfo("synthetic-near-expiry", now + 10),
            AccessTokenInfo("synthetic-refreshed", now + 3600),
        ]
    elif outcome == "auth_failure":
        managed_identity.get_token_info.side_effect = ClientAuthenticationError("synthetic-private-provider-detail")
    elif outcome == "unexpected":
        managed_identity.get_token_info.side_effect = RuntimeError("synthetic-defect")

    async def handle(request):
        requests.append(dict(request.headers))
        return httpx.Response(200)

    def client_factory(**kwargs):
        client = httpx.AsyncClient(
            transport=mcp_client._TraceContextOnlyAsyncTransport(httpx.MockTransport(handle)),
            **kwargs,
        )
        clients.append(client)
        return client

    class Tool:
        def __init__(self, **kwargs):
            self.client = kwargs["http_client"]

        async def __aenter__(self):
            await self.client.post("https://mcp.example.invalid/mcp", headers={"baggage": "private=never-send"})
            return self

        async def __aexit__(self, *args):
            closed.append(True)

    monkeypatch.setattr(mcp_client, "MCPStreamableHTTPTool", Tool)

    async def run():
        async with open_mcp_tool(
            endpoint="https://mcp.example.invalid", transport="streamable_http", timeout=90,
            user_context=None, api_key="must-not-be-sent", auth_resource_scope=SCOPE,
            http_client_factory=client_factory,
        ) as tool:
            await tool.client.post("https://mcp.example.invalid/mcp")
            if outcome == "caller_failure":
                raise ValueError("synthetic-caller")
            if outcome == "cancelled":
                raise asyncio.CancelledError("synthetic-cancellation")

    expected = {
        "caller_failure": ValueError, "cancelled": asyncio.CancelledError,
        "auth_failure": ClientAuthenticationError, "unexpected": RuntimeError,
    }
    if outcome in expected:
        with pytest.raises(expected[outcome]) as raised:
            await run()
        if outcome == "auth_failure":
            assert str(raised.value) == "MCP managed identity token acquisition failed."
    else:
        await run()
    assert all(client.is_closed for client in clients)
    managed_identity.__aexit__.assert_awaited_once()
    assert len(requests) == (0 if outcome in ("auth_failure", "unexpected") else 2)
    assert bool(closed) is (outcome not in ("auth_failure", "unexpected"))
    assert all("x-api-key" not in r and "baggage" not in r for r in requests)
    if outcome == "expired":
        assert [r["authorization"] for r in requests] == [
            "Bearer synthetic-near-expiry", "Bearer synthetic-refreshed",
        ]
        assert managed_identity.get_token_info.await_count == 2
    else:
        assert managed_identity.get_token_info.await_count == 1


@pytest.mark.parametrize("scope", ["", " ", "https://database.windows.net/.default", "https://search.azure.com/.default", "api:///.default", "api://resource/read"])
async def test_invalid_bearer_scope_never_downgrades_to_api_key(scope, managed_identity):
    with pytest.raises(ValueError, match="MCP_AUTH_RESOURCE_SCOPE"):
        async with open_mcp_tool(
            endpoint="https://mcp.example.invalid", transport="streamable_http", timeout=90,
            user_context=None, api_key="must-not-be-used", auth_resource_scope=scope,
        ):
            pytest.fail("Invalid scope cannot connect.")
    assert not managed_identity.created


@pytest.mark.parametrize("outcome", ["empty", "cancelled"])
async def test_empty_or_cancelled_acquisition_sends_no_request_and_closes_credential(outcome, managed_identity, monkeypatch):
    if outcome == "empty":
        managed_identity.get_token_info.return_value = AccessTokenInfo("", int(time.time()) + 3600)
    else:
        managed_identity.get_token_info.side_effect = asyncio.CancelledError("synthetic-cancellation")
    handler = AsyncMock(return_value=httpx.Response(200))
    clients = []
    def client_factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    class Tool:
        def __init__(self, **kwargs):
            self.client = kwargs["http_client"]
        async def __aenter__(self):
            await self.client.post("https://mcp.example.invalid/mcp")
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(mcp_client, "MCPStreamableHTTPTool", Tool)
    with pytest.raises(ClientAuthenticationError if outcome == "empty" else asyncio.CancelledError):
        async with open_mcp_tool(
            endpoint="https://mcp.example.invalid", transport="streamable_http", timeout=90,
            user_context=None, api_key="must-not-be-used", auth_resource_scope=SCOPE,
            http_client_factory=client_factory,
        ):
            pytest.fail("Credential failure cannot connect.")
    handler.assert_not_awaited()
    managed_identity.__aexit__.assert_awaited_once()
    assert all(client.is_closed for client in clients)
