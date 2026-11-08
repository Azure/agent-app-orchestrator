"""Official FoundryToolbox MCP transport and Responses outcomes, entirely offline."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import httpx2
import pytest
from agent_framework import MiddlewareFailure
from agent_framework_foundry import FoundryChatClient
from azure.ai.agentserver.core import FoundryAgentRequestContext, reset_request_context, set_request_context
from azure.core.credentials import AccessToken
from openai import AsyncOpenAI

from native import hosted
from native.agent import create_agent
from native.config import FunctionalSnapshot, RuntimeSettings
from native.knowledge import KNOWLEDGE_TOOL, enforce_tool_outcomes, knowledge_toolbox
from test_native_runtime import BASE, SERVICE, fake_config

ENDPOINT = BASE["AI_FOUNDRY_PROJECT_ENDPOINT"] + "/toolboxes/approved-kb/mcp?api-version=v1"
CONSENT = "https://consent.example.invalid/authorize"
KB_SETTINGS = {
    "FOUNDRY_IQ_ENABLED": True, "TOOLBOX_ENDPOINT": ENDPOINT,
    "FOUNDRY_IQ_PATTERN": "searchIndex", "FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": "searchIndex",
    "FOUNDRY_IQ_INGESTION_PERMISSION_OPTIONS": ["rbacScope"],
}


@pytest.fixture
def toolbox_http(monkeypatch):
    packets = []
    state = {"error_at": None, "consent": False}
    credential = SimpleNamespace(get_token=Mock(return_value=AccessToken("synthetic-agent-token", 4102444800)))

    def boundary(request):
        assert str(request.url) == ENDPOINT
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        packets.append((body, dict(request.headers)))
        if "id" not in body:
            return httpx2.Response(202)
        method = body["method"]
        if method == "server/discover":
            return httpx2.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "error": {"code": -32601, "message": "Method not found"},
            })
        if state["error_at"] == method:
            if state.get("http_denied"):
                return httpx2.Response(403, json={"error": "authorization denied"})
            if state.get("tool_denied"):
                return httpx2.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"],
                    "result": {"isError": True, "content": [{"type": "text", "text": "authorization denied"}]},
                })
            error = {"code": -32000, "message": "retrieval authorization denied"}
            if state["consent"]:
                error = {"code": -32006, "message": "Tool source requires consent " + json.dumps({
                    "errors": [{"name": "knowledge_base", "type": "mcp",
                                "error": {"code": "CONSENT_REQUIRED", "message": CONSENT}}],
                })}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})
        results = {
            "initialize": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                "serverInfo": {"name": "managed-knowledge-toolbox", "version": "1"},
            },
            "tools/list": {"tools": [{
                "name": KNOWLEDGE_TOOL, "description": "Retrieve from the approved knowledge base.",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            }]},
            "tools/call": {"content": [{"type": "text", "text": "Authorized knowledge result"}], "isError": False},
            "ping": {},
        }
        assert method in results
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": results[method]})

    real_client = httpx2.AsyncClient

    class Client(real_client):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", httpx2.MockTransport(boundary))
            super().__init__(**kwargs)

    monkeypatch.setattr("agent_framework_foundry_hosting._toolbox.httpx.AsyncClient", Client)
    return packets, state, credential


async def test_official_toolbox_uses_current_call_id_for_two_users_and_agent_auth(toolbox_http):
    packets, _, credential = toolbox_http
    first = set_request_context(FoundryAgentRequestContext(user_id="user-a", call_id="call-a"))
    try:
        async with knowledge_toolbox(ENDPOINT, credential) as toolbox:
            assert [tool.name for tool in toolbox.functions] == [KNOWLEDGE_TOOL]
            await toolbox.call_tool(KNOWLEDGE_TOOL, query="First question")
            boundary = len(packets)
            second = set_request_context(FoundryAgentRequestContext(user_id="user-b", call_id="call-b"))
            try:
                await toolbox.call_tool(KNOWLEDGE_TOOL, query="Second question")
                assert any(body["method"] == "tools/call" for body, _ in packets[boundary:])
                assert all(headers["x-agent-foundry-call-id"] == "call-b" for _, headers in packets[boundary:])
            finally:
                reset_request_context(second)
        assert all(headers["x-agent-foundry-call-id"] == "call-a" for _, headers in packets[:boundary])
        assert all("x-ms-query-source-authorization" not in headers for _, headers in packets)
        assert all(headers["authorization"] == "Bearer synthetic-agent-token" for _, headers in packets)
        assert all(call.args == ("https://ai.azure.com/.default",) for call in credential.get_token.call_args_list)
    finally:
        reset_request_context(first)


@pytest.mark.parametrize("context,match", [
    (FoundryAgentRequestContext(), "trusted platform caller"),
    (FoundryAgentRequestContext(user_id="partition-without-call"), "call"),
])
async def test_missing_trusted_context_is_denied_before_any_toolbox_http(toolbox_http, context, match):
    packets, _, credential = toolbox_http
    token = set_request_context(context)
    try:
        with pytest.raises(Exception, match=match):
            async with knowledge_toolbox(ENDPOINT, credential):
                pytest.fail("Missing native identity must not connect.")
        assert not packets
    finally:
        reset_request_context(token)


@pytest.mark.parametrize("auth_mode", ["user_delegated", "service_identity"])
@pytest.mark.parametrize("failure", [
    None, "list-consent", "call-consent", "call-denied", "call-http-denied", "call-tool-denied",
])
async def test_actual_host_managed_kb_invocation_and_consent_outcomes(
    monkeypatch, tmp_path, caplog, toolbox_http, auth_mode, failure,
):
    packets, state, credential = toolbox_http
    if failure:
        state.update(error_at="tools/list" if failure == "list-consent" else "tools/call",
                     consent=failure.endswith("consent"),
                     http_denied=failure == "call-http-denied", tool_denied=failure == "call-tool-denied")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "managed-kb-session")
    config = fake_config({**(SERVICE if auth_mode == "service_identity" else {}), **KB_SETTINGS},
                         mode="conversational")
    config.sync_credential = credential
    model_packets = []

    def model_boundary(request):
        assert request.headers["x-agent-foundry-call-id"] == "current-call"
        assert "x-agent-user-id" not in request.headers
        body = json.loads(request.content)
        model_packets.append(body)
        output = [{
            "id": "kb-function", "type": "function_call", "call_id": "kb-call",
            "name": KNOWLEDGE_TOOL, "arguments": '{"query":"Question"}', "status": "completed",
        }]
        if any(item.get("type") == "function_call_output" for item in body["input"]):
            output = [{
                "id": "kb-answer", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Grounded response", "annotations": []}],
            }]
        response = {
            "id": f"model-response-{len(model_packets)}", "object": "response", "created_at": 0,
            "model": "synthetic-model", "status": "completed", "output": output, "usage": None,
        }
        if not body.get("stream"):
            return httpx2.Response(200, json=response)
        events = [{"type": "response.output_item.added", "output_index": 0, "item": output[0]}]
        if output[0]["type"] == "function_call":
            events.append({
                "type": "response.function_call_arguments.done", "output_index": 0,
                "item_id": output[0]["id"], "arguments": output[0]["arguments"],
            })
        else:
            events.append({
                "type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                "item_id": output[0]["id"], "delta": "Grounded response", "logprobs": [],
            })
        events.append({"type": "response.completed", "response": response})
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
        )

    model_http = httpx2.AsyncClient(transport=httpx2.MockTransport(model_boundary))
    openai = AsyncOpenAI(
        api_key="synthetic-placeholder", base_url="https://model.example.invalid/v1", http_client=model_http,
    )
    model = FoundryChatClient(
        project_client=SimpleNamespace(get_openai_client=lambda **_: openai), model="synthetic-model",
    )
    monkeypatch.setattr(hosted, "create_agent", lambda config, **kwargs: create_agent(config, client=model, **kwargs))
    server = hosted.build_server(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
        result = await client.post("/responses", headers={
            "x-agent-user-id": "platform-partition", "x-agent-foundry-call-id": "current-call",
        }, json={"input": "Question", "store": True, "stream": False})
    await openai.close()
    assert result.status_code == 200, result.text
    response = result.json()
    if failure and failure.endswith("consent") and auth_mode == "user_delegated":
        assert response["status"] == "incomplete", response
        consent = next(item for item in response["output"] if item["type"] == "oauth_consent_request")
        assert consent["consent_link"] == CONSENT and consent["server_label"] == "knowledge_base"
    elif failure:
        assert response["status"] == "failed", response
        assert not any(item["type"] == "oauth_consent_request" for item in response["output"])
    else:
        assert response["status"] == "completed", response
        calls = [body for body, _ in packets if body["method"] == "tools/call"]
        assert len(calls) == 1 and calls[0]["params"]["name"] == KNOWLEDGE_TOOL
        assert len(model_packets) == 2
    if failure:
        assert len(model_packets) == (0 if failure == "list-consent" else 1)
    assert all(headers["x-agent-foundry-call-id"] == "current-call" for _, headers in packets)
    assert "synthetic-agent-token" not in result.text
    assert "synthetic-agent-token" not in caplog.text
    assert "synthetic-agent-token" not in json.dumps(model_packets)
    assert "x-ms-query-source-authorization" not in json.dumps(model_packets)
    assert all(packet["store"] is False for packet in model_packets)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"synthetic-agent-token" not in path.read_bytes()


async def test_delegated_only_tool_is_denied_at_invocation_not_at_startup():
    next_call = AsyncMock()
    context = SimpleNamespace(function=SimpleNamespace(name="multimodal_retrieve"))
    with pytest.raises(MiddlewareFailure, match="requires user delegation \\(OBO\\)"):
        await enforce_tool_outcomes("service_identity")(context, next_call)
    next_call.assert_not_awaited()


@pytest.mark.parametrize("endpoint", [
    None, "", "https://other.invalid/toolboxes/kb/mcp?api-version=v1",
    ENDPOINT.replace("api-version=v1", "api-version=other"), ENDPOINT + "#fragment",
])
def test_managed_toolbox_binding_is_required_and_project_scoped(endpoint):
    with pytest.raises(ValueError, match="TOOLBOX_ENDPOINT"):
        RuntimeSettings.read({**BASE, **KB_SETTINGS, "TOOLBOX_ENDPOINT": endpoint}, {})
    assert "TOOLBOX_ENDPOINT" not in FunctionalSnapshot.capture({**BASE, **KB_SETTINGS}).values
