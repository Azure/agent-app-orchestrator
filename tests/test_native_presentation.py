import asyncio
import json

import pytest

from native.presentation import hydrate, normalize, settings, supervise


@pytest.mark.parametrize("domain", ["sales", "operations", "finance"])
@pytest.mark.parametrize("tool_name", ["native_metrics", "mcp_customer_tool", "independent_customer_connector"])
def test_reviewed_domains_have_identical_exact_measurements_independent_of_tool_name(domain, tool_name):
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/source-fixture.json").read_text())
    raw = next(s for s in fixture["sources"] if s["domain"] == domain)
    data = normalize(raw, tool_name, "authorized-invocation")
    numeric = next(f for f in data["fields"] if f["type"] == "decimal")
    category = next(f for f in data["fields"] if f["type"] == "string")
    card = {"sourceId": data["sourceId"], "sourceRowId": "row-0", "field": numeric["id"], "label": "Measure"}
    if domain == "operations":
        card["status"] = {"sourceField": "status"}
    result = hydrate({"schemaVersion": 1, "blocks": [
        {"type": "cards", "cards": [card]},
        {"type": "table", "sourceId": data["sourceId"],
         "columns": [{"field": f["id"], "label": f["id"]} for f in data["fields"]]},
        {"type": "chart", "chartType": "line", "sourceId": data["sourceId"], "categoryField": category["id"],
         "series": [{"field": numeric["id"], "label": "Measure"}]},
    ]}, [data], domain, "0" * 64)
    contribution = result["contributions"][0]
    cards, table, chart = contribution["blocks"]
    assert contribution["sourceRefs"][0]["toolName"] == tool_name
    assert cards["cards"][0]["value"]["value"] == raw["rows"][0]["data"][numeric["id"]]
    assert cards["cards"][0]["unit"] == numeric["unit"]
    for original, projected in zip(raw["rows"], table["rows"]):
        assert list(projected["values"].values()) == [original["data"][f["id"]] for f in data["fields"]]
    assert chart["series"][0]["values"] == [r["data"][numeric["id"]] for r in raw["rows"]]
    assert chart["series"][0]["unit"] == numeric["unit"]
    if domain == "operations":
        assert cards["cards"][0]["status"]["value"] == "ok"
        assert cards["cards"][0]["status"]["provenance"]["origin"] == "source"


def test_schema_packaging_integrity():
    import hashlib
    from native.presentation import ROOT
    names = ("agent-presentation-settings", "presentation-selection", "turn-presentation")
    for name in names:
        schema = ROOT / f"{name}-v1.schema.json"
        data = schema.read_bytes()
        assert b"\r" not in data
        assert hashlib.sha256(data).hexdigest() == schema.with_name(schema.name + ".sha256").read_text().split()[0]


def test_invalid_disabled_and_other_agent_rules_are_still_errors():
    rule = {"id": "approved", "toolName": "customer_tool", "field": "amount", "kind": "status",
            "missing": "omit", "cases": [{"tests": [{"operator": "gte", "threshold": "0"}], "value": "ok"}]}
    document = {"schemaVersion": 1, "agents": {"other": {"enabled": False, "rules": [rule, rule]}}}
    with pytest.raises(ValueError, match="duplicate rule"):
        settings(document, "agent-lz-conversation")


def test_exact_approved_indicator_rule_and_source_provenance():
    from native.presentation import indicator
    data = source()
    data["fields"].append({"id": "state", "type": "string"})
    data["rows"][0]["data"]["state"] = "critical"
    rule = {"id": "approved", "toolName": "customer_tool", "field": "amount", "kind": "status",
            "missing": "omit", "cases": [{"tests": [{"operator": "gt", "threshold": "9007199254740993.000"}], "value": "critical"}]}
    assert indicator({"ruleId": "approved"}, "status", data, data["rows"][0], [rule])["value"] == "critical"
    assert indicator({"ruleId": "approved"}, "status", data, data["rows"][1], [rule]) is None
    sourced = indicator({"sourceField": "state"}, "status", data, data["rows"][0], [])
    assert sourced["provenance"]["origin"] == "source"
    assert sourced["provenance"]["binding"]["field"] == "state"
    rule["toolName"] = "another-tool"
    assert indicator({"ruleId": "approved"}, "status", data, data["rows"][0], [rule]) is None


def test_malformed_unit_does_not_contaminate_valid_sibling_fallback():
    data = source()
    result = hydrate({"schemaVersion": 1, "blocks": [
        {"type": "cards", "cards": [{"sourceId": "foreign", "sourceRowId": "row-0", "field": "amount", "label": "rejected"}]},
        {"type": "suggestions", "prompts": ["Compare again"]},
    ]}, [data], "a", "0" * 64)
    assert len(result["contributions"][0]["blocks"]) == 1
    assert result["markdown"] == "Compare again"


def test_aggregate_exact_byte_limit_and_block_limit():
    from native.presentation import compact
    selection = {"schemaVersion": 1, "blocks": [{"type": "suggestions", "title": "", "prompts": ["x" * 30000] * 4}]}
    baseline = hydrate(selection, [], "a", "0" * 64)
    remaining = 262144 - len(compact(baseline).encode())
    assert 0 < remaining < 32768
    selection["blocks"][0]["title"] = "x" * remaining
    assert len(compact(hydrate(selection, [], "a", "0" * 64)).encode()) == 262144
    selection["blocks"][0]["title"] += "x"
    with pytest.raises(ValueError, match="aggregate byte"):
        hydrate(selection, [], "a", "0" * 64)
    with pytest.raises(ValueError, match="aggregate selection"):
        hydrate({"schemaVersion": 1, "blocks": [{"type": "suggestions", "prompts": ["a"]}] * 11}, [], "a", "0" * 64)


@pytest.mark.asyncio
async def test_public_function_capture_success_failure_and_disabled():
    from agent_framework import FunctionInvocationContext, tool
    from native.presentation_middleware import _capture, capture_result
    @tool(name="customer_business_tool")
    def business_tool() -> dict:
        return {}
    context = FunctionInvocationContext(business_tool, {})
    captured = []
    async def success():
        context.result = {"fields": [], "rows": []}
    token = _capture.set(captured)
    try:
        await capture_result(context, success)
        assert len(captured) == 1 and captured[0][1] == "customer_business_tool"
        async def denied():
            raise PermissionError("denied")
        with pytest.raises(PermissionError):
            await capture_result(context, denied)
        assert len(captured) == 1
    finally:
        _capture.reset(token)
    await capture_result(context, success)
    assert len(captured) == 1


def test_functional_snapshot_is_immutable_and_wire_stable():
    from native.config import FunctionalSnapshot
    original = {"AGENT_PRESENTATION_SETTINGS": {"schemaVersion": 1, "agents": {"a": {"enabled": True}}}}
    snapshot = FunctionalSnapshot(revision="rev", values=original)
    original["AGENT_PRESENTATION_SETTINGS"]["agents"]["a"]["enabled"] = False
    assert snapshot.values["AGENT_PRESENTATION_SETTINGS"]["agents"]["a"]["enabled"] is True
    with pytest.raises(TypeError):
        snapshot.values["AGENT_PRESENTATION_SETTINGS"]["agents"]["a"]["enabled"] = False
    assert json.loads(snapshot.model_dump_json())["values"]["AGENT_PRESENTATION_SETTINGS"]["agents"]["a"]["enabled"] is True


def test_workflow_business_optional_field_and_readable_projection():
    from agent_framework import Message
    from native.contracts import ReviewRequest, ResearchReviewResult
    from native.presentation import compact, response_projection
    ordinary = ReviewRequest(topic="test", draft="readable", configuration_revision="rev")
    assert "presentation" not in json.loads(ordinary.model_dump_json())
    data = source()
    envelope = hydrate({"schemaVersion": 1, "blocks": [{"type": "table", "sourceId": data["sourceId"],
                        "columns": [{"field": "amount", "label": "Amount"}]}]}, [data], "a", "0" * 64)
    draft, presentation = response_projection([Message("assistant", ["Narration"]), Message("assistant", [compact(envelope)])])
    assert draft.startswith("Narration") and "9007199254740993.001" in draft
    assert "agent_lz_presentation" not in draft
    request = ReviewRequest(topic="test", draft=draft, configuration_revision="rev", presentation=presentation)
    restored = ReviewRequest.model_validate_json(request.model_dump_json())
    result = ResearchReviewResult(status="approved", review_comment="", **restored.model_dump())
    assert result.presentation == envelope


@pytest.mark.asyncio
async def test_expired_serialization_cannot_mutate_completed_native_answer(monkeypatch):
    import time
    from native.agent import create_agent
    from native import presentation_middleware
    from native.presentation import compact
    from test_native_runtime import fake_config, RecordingClient
    envelope = hydrate({"schemaVersion": 1, "blocks": [{"type": "suggestions", "prompts": ["Compare"]}]}, [], "a", "0" * 64)
    async def selected(*args):
        return envelope
    async def short_budget(work):
        return await supervise(work, budget=0.005)
    def slow_serialization(value):
        time.sleep(0.04)
        return compact(value)
    monkeypatch.setattr(presentation_middleware, "stage", selected)
    monkeypatch.setattr(presentation_middleware, "supervise", short_budget)
    monkeypatch.setattr(presentation_middleware, "compact", slow_serialization)
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"agent-lz-conversation": {"enabled": True}},
    })})
    async with create_agent(config, client=RecordingClient()) as agent:
        final = await agent.run("question")
    assert final.text == "synthetic research draft"
    await asyncio.sleep(0.08)
    assert len(final.messages) == 1 and final.text == "synthetic research draft"


@pytest.mark.asyncio
async def test_disabled_split_primary_marker_is_inert_without_optional_calls():
    from agent_framework import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream
    from native.agent import create_agent
    from native.presentation import guard_primary
    from test_native_runtime import fake_config, RecordingClient
    raw = '{"kind":"agent_lz_presentation","schemaVersion":1,"markdown":"model imitation"}'
    class Client(RecordingClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.calls.append((messages, options))
            response = ChatResponse(messages=[Message("assistant", [raw])])
            async def updates():
                for chunk in (raw[:17], raw[17:23], raw[23:]):
                    yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(chunk)], message_id="primary")
            return ResponseStream(updates(), finalizer=lambda _: response)
    client = Client()
    async with create_agent(fake_config(), client=client) as agent:
        stream = agent.run("question", stream=True)
        updates = [item async for item in stream]
        final = await stream.get_final_response()
    assert "".join(item.text for item in updates) == guard_primary(raw)
    assert final.text == guard_primary(raw)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_local_native_carrier_to_component_ui_reader(monkeypatch):
    """Local cross-component carrier test; not a real-host/browser/tool smoke."""
    ui = pytest.importorskip("gpt_rag_ui.services.native_presentation")
    from types import SimpleNamespace
    from native.agent import create_agent
    from native import presentation_middleware
    from test_native_runtime import fake_config, RecordingClient
    actual_stage = presentation_middleware.stage
    captured = [({
        "fields": [{"id": "amount", "type": "decimal", "unit": "USD"}],
        "rows": [{"data": {"amount": "9007199254740993.001"}}, {"data": {"amount": None}}],
    }, "authorized_fixture_tool", "fixture-invocation")]
    class Selector:
        async def get_response(self, messages, *, options):
            metadata = json.loads(messages[1].text.split("Authorized typed results:\n")[1])
            return SimpleNamespace(text=json.dumps({"schemaVersion": 1, "blocks": [{
                "type": "table", "sourceId": metadata[0]["sourceId"],
                "columns": [{"field": "amount", "label": "Amount"}],
            }]}))
    async def fixture_stage(client, unused, agent_id, revision, guidance, intent, rules):
        return await actual_stage(Selector(), captured, agent_id, revision, guidance, intent, rules)
    monkeypatch.setattr(presentation_middleware, "stage", fixture_stage)
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"agent-lz-conversation": {"enabled": True}},
    })})
    async with create_agent(config, client=RecordingClient()) as agent:
        stream = agent.run("compare", stream=True)
        updates = [item async for item in stream]
        final = await stream.get_final_response()
    carrier = final.messages[-1].text
    assert updates[-1].text == carrier
    projected = ui.read_presentation(carrier, admitted=True)
    rows = projected["contributions"][0]["blocks"][0]["rows"]
    assert rows[0]["values"]["c0"] == "9007199254740993.001"
    assert rows[1]["values"]["c0"] is None
    assert ui.text_projection({"messages": [{"role": "assistant", "contents": [
        {"type": "text", "text": carrier},
    ]}]}) == projected["markdown"]


def test_table_row_boundary_retains_valid_siblings():
    data = normalize({"fields": [{"id": "v", "type": "integer"}],
                      "rows": [{"data": {"v": str(i)}} for i in range(500)]}, "native_tool", "result")
    selection = {"schemaVersion": 1, "blocks": [
        {"type": "table", "sourceId": data["sourceId"], "columns": [{"field": "v", "label": "Count"}]},
        {"type": "suggestions", "prompts": ["Compare"]},
    ]}
    assert len(hydrate(selection, [data], "a", "0" * 64)["contributions"][0]["blocks"][0]["rows"]) == 500
    data["rows"].append({"sourceRowId": "row-500", "data": {"v": "500"}})
    result = hydrate(selection, [data], "a", "0" * 64)
    assert [b["type"] for b in result["contributions"][0]["blocks"]] == ["suggestions"]
    assert result["markdown"] == "Compare"


def test_chart_series_and_nonnull_point_boundaries():
    fields = [{"id": "category", "type": "string"}] + [{"id": f"v{i}", "type": "decimal", "unit": "USD"} for i in range(8)]
    rows = [{"data": {"category": str(i), **{f"v{n}": "1.000" for n in range(8)}}} for i in range(126)]
    for n in range(8):
        rows[0]["data"][f"v{n}"] = None
    data = normalize({"fields": fields, "rows": rows}, "mcp_customer_tool", "result")
    chart = {"type": "chart", "chartType": "line", "sourceId": data["sourceId"], "categoryField": "category",
             "series": [{"field": f"v{i}", "label": f"Series {i}"} for i in range(8)]}
    selection = {"schemaVersion": 1, "blocks": [chart, {"type": "suggestions", "prompts": ["Compare"]}]}
    result = hydrate(selection, [data], "a", "0" * 64)
    assert len(result["contributions"][0]["blocks"][0]["series"]) == 8
    data["rows"][0]["data"]["v0"] = "1.000"  # exactly 1,001 non-null points
    assert [b["type"] for b in hydrate(selection, [data], "a", "0" * 64)["contributions"][0]["blocks"]] == ["suggestions"]
    data["rows"][0]["data"]["v0"] = None
    chart["series"].append({"field": "v0", "label": "Ninth series"})
    assert [b["type"] for b in hydrate(selection, [data], "a", "0" * 64)["contributions"][0]["blocks"]] == ["suggestions"]


@pytest.mark.parametrize("chart_type,series_count", [("bar", 1), ("grouped_bar", 2), ("line", 1)])
def test_supported_chart_variants_are_exact_and_all_null_is_not_success(chart_type, series_count):
    data = source()
    selection = {"schemaVersion": 1, "blocks": [{
        "type": "chart", "chartType": chart_type, "sourceId": data["sourceId"], "categoryField": "month",
        "series": [{"field": "amount", "label": f"Amount {i}"} for i in range(series_count)],
    }]}
    result = hydrate(selection, [data], "a", "0" * 64)
    assert result["contributions"][0]["blocks"][0]["series"][0]["values"] == ["9007199254740993.001", None]
    data["rows"][0]["data"]["amount"] = None
    assert hydrate(selection, [data], "a", "0" * 64) is None


def source():
    return normalize({
        "fields": [{"id": "month", "type": "string"}, {"id": "amount", "type": "decimal", "unit": "USD"}],
        "rows": [{"data": {"month": "Jan", "amount": "9007199254740993.001"}},
                 {"data": {"month": "Feb", "amount": None}}],
    }, "customer_tool", "invocation-1")


def test_exact_table_and_chart_hydration():
    data = source()
    selection = {"schemaVersion": 1, "blocks": [
        {"type": "table", "sourceId": data["sourceId"], "columns": [{"field": "amount", "label": "Amount"}]},
        {"type": "chart", "chartType": "line", "sourceId": data["sourceId"], "categoryField": "month",
         "series": [{"field": "amount", "label": "Amount"}]},
    ]}
    result = hydrate(selection, [data], "agent-lz-conversation", "0" * 64)
    assert result["contributions"][0]["blocks"][0]["rows"][0]["values"]["c0"] == "9007199254740993.001"
    assert result["contributions"][0]["blocks"][1]["series"][0]["values"] == ["9007199254740993.001", None]
    assert "9007199254740993.001" in result["markdown"]


def test_configuration_default_and_invalid_present():
    assert settings(None, "a") is None
    assert settings('{"schemaVersion":1,"agents":{}}', "a") is None
    with pytest.raises(ValueError):
        settings('{"schemaVersion":2,"agents":{}}', "a")
    with pytest.raises(ValueError):
        settings('{"schemaVersion":1,"schemaVersion":1,"agents":{}}', "a")


def test_lossy_source_rejected():
    with pytest.raises(ValueError):
        normalize({"fields": [{"id": "v", "type": "decimal"}], "rows": [{"data": {"v": 1.1}}]}, "tool", "id")


@pytest.mark.asyncio
async def test_deadline_does_not_wait_for_uncooperative_child():
    finished = asyncio.Event()
    async def late():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
        finished.set()
        return "late"
    assert await supervise(late(), budget=0.01) is None
    assert not finished.is_set()
    await asyncio.sleep(0.08)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_public_sdk_enabled_stream_finalization_without_sources():
    from native.agent import create_agent
    from test_native_runtime import fake_config, RecordingClient
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"agent-lz-conversation": {"enabled": True}},
    })})
    client = RecordingClient()
    async with create_agent(config, client=client) as agent:
        stream = agent.run("question", stream=True)
        updates = [item async for item in stream]
        final = await stream.get_final_response()
    assert final.text == "synthetic research draft"
    assert "".join(item.text for item in updates) == final.text
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_tool_free_selection_stage_hydrates_instead_of_model_measurements():
    from types import SimpleNamespace
    from native.presentation_middleware import stage
    calls = []
    class Client:
        async def get_response(self, messages, *, options):
            calls.append(options)
            metadata = json.loads(messages[1].text.split("Authorized typed results:\n")[1])
            return SimpleNamespace(text=json.dumps({"schemaVersion": 1, "blocks": [{
                "type": "table", "sourceId": metadata[0]["sourceId"],
                "columns": [{"field": "amount", "label": "Amount"}],
            }]}))
    data = source()
    captured = [(data, "customer_tool", "invocation-1")]
    result = await stage(Client(), captured, "agent-lz-conversation", "0" * 64, "", "compare")
    assert result["contributions"][0]["blocks"][0]["rows"][0]["values"]["c0"] == "9007199254740993.001"
    assert len(calls) == 1 and calls[0]["tools"] == []
    # Optional properties in the reviewed JSON Schema use local strict admission,
    # not the provider's narrower strict structured-output dialect.
    assert calls[0]["response_format"]["json_schema"]["strict"] is False


@pytest.mark.asyncio
async def test_public_sdk_complete_envelope_stream_and_final_are_identical(monkeypatch):
    from native.agent import create_agent
    from native.presentation import compact
    from native import presentation_middleware
    from test_native_runtime import fake_config, RecordingClient
    data = source()
    envelope = hydrate({"schemaVersion": 1, "blocks": [{
        "type": "table", "sourceId": data["sourceId"],
        "columns": [{"field": "amount", "label": "Amount"}],
    }]}, [data], "agent-lz-conversation", "0" * 64)
    async def selected(*args):
        return envelope
    monkeypatch.setattr(presentation_middleware, "stage", selected)
    config = fake_config({"AGENT_PRESENTATION_SETTINGS": json.dumps({
        "schemaVersion": 1, "agents": {"agent-lz-conversation": {"enabled": True}},
    })})
    async with create_agent(config, client=RecordingClient()) as agent:
        stream = agent.run("question", stream=True)
        updates = [item async for item in stream]
        final = await stream.get_final_response()
    assert final.messages[-1].text == compact(envelope)
    assert updates[-1].text == final.messages[-1].text
