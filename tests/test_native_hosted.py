"""Actual product Responses host against published SDK, without a listening server."""

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest

pytest.importorskip("agent_framework_foundry_hosting")

from native import hosted
from native.agent import create_agent
from native.contracts import ResearchReviewResult, ResearchReviewStart
from native.tools import ToolIdentity
from test_native_runtime import SERVICE, RecordingClient, fake_config


@pytest.mark.parametrize("name,media_type,data", [
    ("note.txt", "text/plain", b"synthetic private text"),
    ("note.md", "text/markdown", b"# synthetic private text"),
    ("note.pdf", "application/pdf", b"%PDF-synthetic"),
    ("image.png", "image/png", b"\x89PNG\r\n\x1a\n"),
    ("image.jpg", "image/jpeg", b"\xff\xd8\xff"),
    ("image.jpeg", "image/jpeg", b"\xff\xd8\xff"),
])
async def test_native_inline_input_reaches_real_foundry_model_serialization(
    monkeypatch, tmp_path, name, media_type, data,
):
    from agent_framework.foundry import FoundryChatClient
    from openai import AsyncOpenAI

    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session")
    packets = []

    def model_boundary(request):
        assert request.url.path == "/v1/responses"
        packets.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "synthetic-response", "object": "response", "created_at": 0,
            "model": "synthetic-model", "status": "completed", "output": [{
                "id": "synthetic-message", "type": "message", "status": "completed",
                "role": "assistant", "content": [{
                    "type": "output_text", "text": "Synthetic response", "annotations": [],
                }],
            }],
        })

    # Inject only the public project-client/HTTP boundary. The native host,
    # Agent, FoundryChatClient and OpenAI serialization all use published APIs.
    transport = httpx2.AsyncClient(transport=httpx2.MockTransport(model_boundary))
    openai_client = AsyncOpenAI(
        api_key="synthetic-test-placeholder", base_url="https://model.example.invalid/v1",
        http_client=transport,
    )
    project = SimpleNamespace(get_openai_client=lambda **_: openai_client)
    model = FoundryChatClient(project_client=project, model="synthetic-model")
    monkeypatch.setattr(hosted, "create_agent", lambda config, **_: create_agent(config, client=model))
    server = hosted.build_server(fake_config(mode="conversational"))
    uri = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
    content = (
        {"type": "input_image", "image_url": uri} if media_type.startswith("image/")
        else {"type": "input_file", "filename": name, "file_data": uri}
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
            response = await client.post("/responses", headers={
                "x-agent-user-id": "synthetic-user", "x-agent-foundry-call-id": "synthetic-call",
            }, json={
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Inspect"}, content]}],
                "store": True, "stream": False,
            })
        assert response.status_code == 200 and response.json()["status"] == "completed", response.text
        assert len(packets) == 1
        user = next(message for message in packets[0]["input"] if message.get("role") == "user")
        if media_type.startswith("image/"):
            item = next(item for item in user["content"] if item["type"] == "input_image")
            assert item["image_url"] == uri
        elif media_type.startswith("text/"):
            # The native SDK decodes UTF-8 file content to model text, rather
            # than sending a text file through the PDF/image model envelope.
            assert any(
                item["type"] == "input_text" and data.decode("utf-8") in item["text"]
                for item in user["content"]
            ), user
        else:
            item = next(item for item in user["content"] if item["type"] == "input_file")
            assert item["filename"] == name and item["file_data"] == uri
    finally:
        await openai_client.close()


async def test_default_hosted_obo_with_retrieval_off_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session")
    model = RecordingClient()

    def factory(config, **kwargs):
        return create_agent(config, client=model, **kwargs)

    monkeypatch.setattr(hosted, "create_agent", factory)
    server = hosted.build_server(fake_config({
        "FOUNDRY_IQ_ENABLED": False,
    }, mode="conversational"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
        response = await client.post("/responses", headers={
            "x-agent-user-id": "real-platform-partition", "x-agent-foundry-call-id": "real-platform-call",
        }, json={"input": "Protected query", "store": True, "stream": False})
    assert response.status_code == 200 and response.json()["status"] == "completed", response.text
    assert len(model.calls) == 1
    assert not model.calls[0][1].get("tools")


async def test_parser_uses_typed_start_and_native_pending_response_decoder():
    request = SimpleNamespace(
        request={"store": True},
        get_input_items=AsyncMock(return_value=[{"type": "message"}]),
        get_input_text=AsyncMock(return_value='{"topic":"Synthetic question"}'),
        get_workflow_responses=AsyncMock(),
    )
    turn = await hosted.parse_workflow_turn(request)
    assert turn.input == ResearchReviewStart(topic="Synthetic question")
    request.get_workflow_responses.assert_not_awaited()
    request.get_input_items.return_value = [{"type": "function_call_output", "call_id": "wire-id"}]
    request.get_workflow_responses.return_value = {"pending-native-id": {"decision": "reject"}}
    assert (await hosted.parse_workflow_turn(request)).responses == request.get_workflow_responses.return_value
    request.get_workflow_responses.side_effect = ValueError("stale")
    with pytest.raises(ValueError, match="stale"):
        await hosted.parse_workflow_turn(request)
    request.request["store"] = False
    with pytest.raises(ValueError, match="store=true"):
        await hosted.parse_workflow_turn(request)


async def test_actual_product_host_uses_native_server_history(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session")
    clients = []

    def factory(config, **kwargs):
        client = RecordingClient()
        clients.append(client)
        return create_agent(config, client=client)

    monkeypatch.setattr(hosted, "create_agent", factory)
    server = hosted.build_server(fake_config(mode="conversational"))
    headers = {"x-agent-user-id": "synthetic-user", "x-agent-foundry-call-id": "synthetic-call"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
        first = await client.post("/responses", headers=headers, json={
            "input": "Remember the synthetic marker cobalt.", "store": True, "stream": False,
        })
        assert first.status_code == 200, first.text
        response = first.json()
        assert response["status"] == "completed", response
        second = await client.post("/responses", headers=headers, json={
            "input": "What marker did I ask you to remember?",
            "previous_response_id": response["id"], "store": True, "stream": False,
        })
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "completed", second.text
        assert "cobalt" in "\n".join(m.text for m in clients[-1].calls[0][0])
        assert len(clients) == 2  # Fresh agent/client; history comes from the native host.
        for route in ("/orchestrator", "/conversations", "/api/dashboard"):
            assert (await client.get(route)).status_code == 404


async def test_native_host_passes_image_content_without_a_legacy_multimodal_strategy(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session")
    model = RecordingClient()
    monkeypatch.setattr(hosted, "create_agent", lambda config, **kwargs: create_agent(config, client=model))
    server = hosted.build_server(fake_config(mode="conversational"))
    # Synthetic one-pixel PNG: no URL download, customer image or Azure call.
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jz1sAAAAASUVORK5CYII="
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
        result = await client.post("/responses", headers={
            "x-agent-user-id": "synthetic-user", "x-agent-foundry-call-id": "synthetic-call",
        }, json={
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Inspect this synthetic image."},
                {"type": "input_image", "image_url": image},
            ]}],
            "store": True, "stream": False,
        })
    assert result.status_code == 200 and result.json()["status"] == "completed", result.text
    images = [
        content for message in model.calls[0][0] for content in message.contents
        if content.type == "data" and content.media_type == "image/png"
    ]
    assert len(images) == 1 and images[0].uri == image
    assert images[0].additional_properties["openai"]["content_type"] == "input_image"


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_actual_product_workflow_http_pause_resume_and_duplicate_rejection(monkeypatch, tmp_path, decision):
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session")
    clients = []

    def research_factory(config, identity_provider):
        @asynccontextmanager
        async def factory(snapshot):
            model = RecordingClient()
            clients.append(model)
            async with create_agent(config, snapshot=snapshot, client=model) as agent:
                yield agent
        return factory

    monkeypatch.setattr(hosted, "agent_factory", research_factory)
    server = hosted.build_server(fake_config(mode="workflow"))
    headers = {"x-agent-user-id": "synthetic-user", "x-agent-foundry-call-id": "synthetic-call"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
        first = await client.post("/responses", headers=headers, json={
            "input": json.dumps({"topic": "Synthetic research"}), "store": True, "stream": False,
        })
        assert first.status_code == 200, first.text
        pending = first.json()
        calls = [item for item in pending["output"] if item["type"] == "function_call"]
        assert len(calls) == 1, pending.get("error") or pending
        reply = {
            "previous_response_id": pending["id"], "store": True, "stream": False,
            "input": [{
                "type": "function_call_output", "call_id": calls[0]["call_id"],
                "output": json.dumps({"decision": decision, "comment": "Synthetic review"}),
            }],
        }
        second = await client.post("/responses", headers=headers, json=reply)
        assert second.status_code == 200, second.text
        result = second.json()
        assert result["status"] == "completed", result
        structured = [item for item in result["output"] if item["type"] == "structured_outputs"]
        assert len(structured) == 1, result
        typed = ResearchReviewResult.model_validate(structured[0]["output"])
        assert typed.status == ("approved" if decision == "approve" else "rejected")
        assert typed.topic == "Synthetic research"
        assert typed.review_comment == "Synthetic review"
        assert len(typed.configuration_revision) == 64
        assert len(clients) == 1  # Native resume does not repeat research.
        duplicate = await client.post("/responses", headers=headers, json=reply)
        assert duplicate.status_code >= 400 or duplicate.json()["status"] != "completed", duplicate.text
        assert len(clients) == 1  # A stale reply is never converted to a new run.


def test_hosted_identity_never_uses_request_metadata(monkeypatch):
    from azure.ai.agentserver.core import FoundryAgentRequestContext
    import azure.ai.agentserver.core as core

    context = FoundryAgentRequestContext(user_id="opaque-platform-user", call_id="platform-call")
    monkeypatch.setattr(core, "get_request_context", lambda: context)
    identity = hosted.hosted_identity()
    assert identity.call_id == "platform-call"
    assert identity.search_token is None
    assert not hasattr(identity, "dax_token")
    monkeypatch.setattr(core, "get_request_context", lambda: None)
    with pytest.raises(ValueError, match="trusted platform"):
        hosted.hosted_identity()


def test_service_identity_requires_real_platform_partition_without_fabricating_user_context(monkeypatch):
    from azure.ai.agentserver.core import FoundryAgentRequestContext
    import azure.ai.agentserver.core as core

    context = FoundryAgentRequestContext(user_id="opaque-service-partition", call_id="service-call")
    monkeypatch.setattr(core, "get_request_context", lambda: context)
    identity = hosted.hosted_identity("service_identity")
    assert identity.call_id == "service-call" and identity.search_token is None
    assert not hasattr(identity, "user_id")
    assert context.user_id == "opaque-service-partition"
    context.user_id = None
    with pytest.raises(ValueError, match="No end-user identity can be fabricated"):
        hosted.hosted_identity("service_identity")


async def test_service_caller_uses_official_responses_host_and_native_history(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session")
    calls = []

    def factory(config, **kwargs):
        kwargs["identity_provider"]()
        model = RecordingClient()
        calls.append(model)
        return create_agent(config, client=model, **kwargs)

    monkeypatch.setattr(hosted, "create_agent", factory)
    server = hosted.build_server(fake_config(SERVICE, mode="conversational"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as client:
        headers = {"x-agent-user-id": "opaque-service-partition", "x-agent-foundry-call-id": "service-call"}
        response = await client.post("/responses", headers=headers, json={
            "input": "Remember cobalt", "store": True, "stream": False,
        })
        assert response.status_code == 200 and response.json()["status"] == "completed", response.text
        continued = await client.post("/responses", headers=headers, json={
            "input": "Recall it", "previous_response_id": response.json()["id"],
            "store": True, "stream": False,
        })
        assert continued.status_code == 200 and continued.json()["status"] == "completed", continued.text
        assert "cobalt" in "\n".join(message.text for message in calls[-1].calls[0][0])
        rejected = await client.post("/responses", headers={"x-agent-foundry-call-id": "service-call"}, json={
            "input": "No forged partition", "store": True, "stream": False,
        })
        assert rejected.status_code >= 400 or rejected.json()["status"] != "completed"
        assert len(calls) == 2
