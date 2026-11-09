"""Deterministic native graph production; no remote clients or new scheduler."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest
from agent_framework import ChatResponse, Content, InMemoryCheckpointStorage, Message, tool
from agent_framework.openai import OpenAIChatClient

from native.agent import create_agent
from native.contracts import ResearchReviewStart, ReviewDecision
from native.presentation import compact, merge_presentations
from native.workflow import ResearchProducer, build_workflow
from test_native_runtime import fake_config


class ToolClient(OpenAIChatClient):
    STORES_BY_DEFAULT = False

    def __init__(self, value, delay=0):
        super().__init__(model="fixture", api_key="local-fixture-not-a-credential",
                         base_url="https://model.example.invalid")
        self.value, self.delay = value, delay
        self.calls, self.guidance = 0, []
        self.selection_delay, self.invalid = 0, False

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        async def result():
            await asyncio.sleep(self.delay)
            if options.get("response_format"):
                await asyncio.sleep(self.selection_delay)
                self.guidance.append(messages[0].text)
                metadata = json.loads(messages[1].text.split("Authorized typed results:\n")[1])
                return ChatResponse(messages=[Message("assistant", [json.dumps({
                    "schemaVersion": 1, "blocks": [{
                        "type": "table", "sourceId": "not-owned" if self.invalid else metadata[0]["sourceId"],
                        "columns": [{"field": "amount", "label": "Amount"}],
                    }],
                })])])
            self.calls += 1
            if self.calls % 2:
                return ChatResponse(messages=[Message("assistant", [
                    Content.from_function_call(call_id=f"call-{self.calls}", name="customer_measurements", arguments={}),
                ])])
            return ChatResponse(messages=[Message("assistant", ["authorized narration"])])
        assert not stream
        return result()


def producer(config, definition, ordinal, client, counts):
    @tool
    def customer_measurements() -> dict:
        counts[definition] = counts.get(definition, 0) + 1
        return {"fields": [{"id": "amount", "type": "decimal", "unit": "USD"}],
                "rows": [{"data": {"amount": client.value}}, {"data": {"amount": None}}]}

    @asynccontextmanager
    async def factory(snapshot):
        async with create_agent(config, snapshot=snapshot, client=client, definition_id=definition,
                                workflow_ordinal=ordinal) as agent:
            yield agent
    return ResearchProducer(definition, ordinal, factory), customer_measurements


@pytest.mark.parametrize("run", range(20))
async def test_native_multi_agent_order_sources_occurrences_and_disabled(monkeypatch, run):
    counts = {}
    configuration = {"schemaVersion": 1, "agents": {
        "sales": {"enabled": True, "guidance": "sales wording"},
        "finance": {"enabled": True, "guidance": "finance wording"},
        "ordinary": {"enabled": False},
    }}
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps(configuration)})
    clients = [ToolClient("9.750", (run % 3) / 10000), ToolClient("9007199254740993.001", (run % 5) / 10000),
               ToolClient("0.000"), ToolClient("7.100")]
    definitions = [("sales", 0), ("finance", 1), ("ordinary", 2), ("sales", 3)]
    producers, tools = zip(*(producer(config, d, o, c, counts) for (d, o), c in zip(definitions, clients)))
    async def composed(view, identity, resources):
        # Each invocation receives its own authorized public function.
        return [tools[sum(counts.values())]]
    monkeypatch.setattr("native.agent.compose_tools", composed)
    graph = build_workflow(config, producers[0].agent_factory, producers=producers)
    storage = InMemoryCheckpointStorage()
    pending = await graph.run(ResearchReviewStart(topic="Compare exact values"), checkpoint_storage=storage)
    review = pending.get_request_info_events()[0].data
    envelope = review.presentation
    contributions = envelope["contributions"]
    assert [(c["agentId"], c["workflowOrdinal"]) for c in contributions] == [("sales", 0), ("finance", 1), ("sales", 3)]
    assert [c["blocks"][0]["rows"][0]["values"]["c0"] for c in contributions] == [
        "9.750", "9007199254740993.001", "7.100",
    ]
    assert all(c["blocks"][0]["rows"][1]["values"]["c0"] is None for c in contributions)
    assert len({c["sourceRefs"][0]["sourceId"] for c in contributions}) == 3
    assert counts == {"sales": 2, "finance": 1, "ordinary": 1}
    assert not clients[2].guidance
    assert all("finance wording" not in text for text in clients[0].guidance)
    assert all("sales wording" not in text for text in clients[1].guidance)
    assert "authorized narration" in review.draft
    reversed_envelope = copy.deepcopy(envelope)
    reversed_envelope["contributions"].reverse()
    merged = merge_presentations([reversed_envelope])
    assert merged["contributions"] == contributions
    assert merged["markdown"] == envelope["markdown"]
    invalid = copy.deepcopy(envelope)
    invalid["contributions"][0]["blocks"][0]["sourceId"] = "not-owned"
    siblings = merge_presentations([invalid])
    assert [c["workflowOrdinal"] for c in siblings["contributions"]] == [1, 3]
    poisoned = copy.deepcopy(invalid)
    poisoned["settingsRevision"] = "f" * 64
    poisoned["contributions"] = poisoned["contributions"][:1]
    assert merge_presentations([poisoned, envelope])["contributions"] == contributions
    over = copy.deepcopy(envelope)
    for i in range(8):
        extra = copy.deepcopy(over["contributions"][0])
        extra["workflowOrdinal"], extra["contributionId"] = i + 4, f"extra-{i}"
        extra["blocks"][0]["blockId"] = f"extra-block-{i}"
        over["contributions"].append(extra)
    assert merge_presentations([over]) is None  # 11 combined blocks: atomic rejection
    large = copy.deepcopy(envelope)
    large["contributions"][0]["blocks"][0]["title"] = "x" * 262144
    assert merge_presentations([large]) is None
    assert len(compact(envelope).encode("utf-8")) < 262144
    checkpoints = await storage.list_checkpoints(workflow_name=graph.name)
    resumed = await build_workflow(config, producers[0].agent_factory, producers=producers).run(
        responses={pending.get_request_info_events()[0].request_id: ReviewDecision(decision="approve")},
        checkpoint_id=checkpoints[-1].checkpoint_id, checkpoint_storage=storage,
    )
    assert resumed.get_outputs()[0].presentation == envelope
    assert counts == {"sales": 2, "finance": 1, "ordinary": 1}


@pytest.mark.parametrize("failure", ["invalid", "timeout"])
async def test_failed_optional_producer_keeps_native_sibling_and_text(monkeypatch, failure):
    from native import presentation_middleware
    from native.presentation import supervise
    counts = {}
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"sales": {"enabled": True}, "finance": {"enabled": True}},
    })})
    good, bad = ToolClient("9.750"), ToolClient("0.100")
    bad.invalid = failure == "invalid"
    bad.selection_delay = 0.25 if failure == "timeout" else 0
    first, first_tool = producer(config, "sales", 0, good, counts)
    second, second_tool = producer(config, "finance", 1, bad, counts)
    async def composed(view, identity, resources):
        return [[first_tool, second_tool][sum(counts.values())]]
    async def short_deadline(work):
        return await supervise(work, budget=0.1)
    monkeypatch.setattr("native.agent.compose_tools", composed)
    monkeypatch.setattr(presentation_middleware, "supervise", short_deadline)
    pending = await build_workflow(config, first.agent_factory, producers=[first, second]).run({"topic": "Compare"})
    review = pending.get_request_info_events()[0].data
    assert [c["agentId"] for c in review.presentation["contributions"]] == ["sales"]
    assert review.draft.count("authorized narration") == 2
    assert counts == {"sales": 1, "finance": 1}
    sealed = compact(review.presentation)
    await asyncio.sleep(0.3 if failure == "timeout" else 0)
    assert compact(review.presentation) == sealed
