import asyncio
import json
from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest
from agent_framework import Content
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
    assert result[0].text == "hello"
