"""Native completion and interruption receipts, using public framework runs."""

import asyncio
import json

import pytest
from agent_framework import InMemoryCheckpointStorage

from native.agent import create_agent
from native.contracts import ResearchReviewResult, ResearchReviewStart, ReviewDecision
from native.presentation import response_projection
from native.workflow import build_workflow
from test_native_presentation_multi_agent import ToolClient, producer
from test_native_runtime import fake_config


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_review_and_completed_native_checkpoint_keep_optional_result(monkeypatch, enabled, decision):
    counts = {}
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"sales": {"enabled": enabled}},
    })})
    client = ToolClient("9007199254740993.001")
    research, source = producer(config, "sales", 0, client, counts)

    async def compose(*args):
        return [source]

    monkeypatch.setattr("native.agent.compose_tools", compose)
    storage = InMemoryCheckpointStorage()
    graph = build_workflow(config, research.agent_factory)
    pending = await graph.run(ResearchReviewStart(topic="Exact values"), checkpoint_storage=storage)
    assert pending.get_outputs() == []
    request = pending.get_request_info_events()[0]
    draft = request.data
    assert "authorized narration" in draft.draft
    assert "agent_lz_presentation" not in draft.draft
    assert (draft.presentation is not None) is enabled
    if enabled:
        assert "9007199254740993.001" in draft.draft
    calls = (client.calls, len(client.guidance), dict(counts))
    checkpoints = await storage.list_checkpoints(workflow_name=graph.name)

    async def denied(*args):
        raise AssertionError("Restoring recorded research must not invoke source composition")

    monkeypatch.setattr("native.agent.compose_tools", denied)
    resumed = await build_workflow(config, research.agent_factory).run(
        responses={request.request_id: ReviewDecision(decision=decision, comment="Reviewed")},
        checkpoint_id=checkpoints[-1].checkpoint_id, checkpoint_storage=storage,
    )
    result = resumed.get_outputs()[0]
    # Native business serialization, not a second transcript.
    saved = ResearchReviewResult.model_validate_json(result.model_dump_json())
    assert saved.draft == draft.draft
    assert saved.presentation == draft.presentation
    assert saved.status == ("approved" if decision == "approve" else "rejected")
    assert (client.calls, len(client.guidance), counts) == calls
    for _ in range(2):
        restored = ResearchReviewResult.model_validate_json(saved.model_dump_json())
        assert restored == saved
    assert (client.calls, len(client.guidance), counts) == calls
    if not enabled:
        assert "presentation" not in json.loads(saved.model_dump_json())


@pytest.mark.parametrize("workflow", [False, True])
async def test_interrupted_native_run_has_no_completed_presentation(monkeypatch, workflow):
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"sales": {"enabled": True}},
    })})
    counts = {}
    client = ToolClient("9.750")
    entered, release = asyncio.Event(), asyncio.Event()
    original = client._inner_get_response

    def blocked(**kwargs):
        async def wait():
            entered.set()
            await release.wait()
            return await original(**kwargs)
        return wait()

    client._inner_get_response = blocked
    research, source = producer(config, "sales", 0, client, counts)

    async def compose(*args):
        return [source]

    monkeypatch.setattr("native.agent.compose_tools", compose)
    if workflow:
        graph = build_workflow(config, research.agent_factory)
        task = asyncio.create_task(graph.run(ResearchReviewStart(topic="Interrupted")))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with create_agent(config, client=client, definition_id="sales") as agent:
            task = asyncio.create_task(agent.run("Interrupted"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert counts == {}
    assert client.guidance == []


async def test_completed_agent_public_response_roundtrip_keeps_snapshot(monkeypatch):
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"sales": {"enabled": True}},
    })})
    counts = {}
    client = ToolClient("9.750")
    _, source = producer(config, "sales", 0, client, counts)

    async def compose(*args):
        return [source]

    monkeypatch.setattr("native.agent.compose_tools", compose)
    async with create_agent(config, client=client, definition_id="sales") as agent:
        response = await agent.run("Completed")
    from agent_framework import AgentResponse
    fresh = AgentResponse.from_dict(json.loads(json.dumps(response.to_dict())))
    assert response_projection(fresh.messages) == response_projection(response.messages)
    assert counts == {"sales": 1}
    assert len(client.guidance) == 1
