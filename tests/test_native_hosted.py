"""Actual product Responses host against published SDK, without a listening server."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip("agent_framework_foundry_hosting")

from native import hosted
from native.agent import create_agent
from native.contracts import ResearchReviewResult, ResearchReviewStart
from native.tools import ToolIdentity
from test_native_runtime import RecordingClient, fake_config


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
    assert identity.dax_token is None
    monkeypatch.setattr(core, "get_request_context", lambda: None)
    with pytest.raises(ValueError, match="trusted platform"):
        hosted.hosted_identity()
