"""Exact local deadline edges, safe diagnostics and public content preservation."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from agent_framework import AgentResponse, AgentResponseUpdate, Content, Message, ResponseStream

from native import presentation, presentation_middleware


@pytest.mark.parametrize("entity,field,admitted", [
    ("Jan", "month", True), (None, "month", False),
    ("Other", "month", False), ("Jan", "amount", False),
])
def test_source_only_table_status_and_stable_business_action(entity, field, admitted):
    from test_native_presentation import source
    data = source()
    data["fields"].append({"id": "state", "type": "string"})
    for row in data["rows"]:
        row["data"]["state"] = "critical"
    if entity is not None:
        data["rows"][0]["entityId"] = entity
    result = presentation.hydrate({"schemaVersion": 1, "blocks": [{
        "type": "table", "sourceId": data["sourceId"],
        "columns": [{"field": "amount", "label": "Amount"}],
        "status": {"sourceField": "state"},
        "rowAction": {"identifierField": field, "label": "Details", "promptPrefix": "Show details for"},
    }]}, [data], "agent", "0" * 64)
    table = result["contributions"][0]["blocks"][0]
    row = table["rows"][0]
    assert row["status"]["value"] == "critical"
    assert row["status"]["provenance"]["origin"] == "source"
    assert row["values"][table["columns"][0]["id"]] == "9007199254740993.001"
    assert ("followUp" in row) is admitted
    assert "followUp" not in table["rows"][1]
    if admitted:
        assert row["entityId"] == "Jan"
        assert row["followUp"]["prompt"] == "Show details for Jan"
    else:
        assert "rowAction" not in table


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_at,accepted", [(9.999, True), (10.0, False), (10.001, False)])
async def test_exact_deadline_edges(monkeypatch, caplog, completed_at, accepted):
    ticks = iter([0.0, completed_at, completed_at, completed_at])
    async def work():
        return "authorized"
    async def completed(tasks, timeout):
        task = next(iter(tasks))
        await task
        return {task}, set()
    monkeypatch.setattr(presentation.asyncio, "wait", completed)
    with caplog.at_level("INFO"):
        result = await presentation.supervise(work(), clock=lambda: next(ticks))
    assert (result == "authorized") is accepted
    if not accepted:
        record = caplog.records[-1]
        assert record.reason == "deadline"
        assert record.elapsed_seconds >= 10
        assert len(record.correlation_id) == 32
        assert record.source_count == record.block_count == 0


@pytest.mark.asyncio
async def test_correlated_late_failure_is_observed_without_disclosure(caplog):
    ready, release = asyncio.Event(), asyncio.Event()
    async def uncooperative():
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("private values and guidance must never be logged")
    with caplog.at_level("INFO"):
        assert await presentation.supervise(uncooperative(), budget=0.01) is None
        assert ready.is_set()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    timeout = next(r for r in caplog.records if r.msg == "presentation_timed_out")
    late = next(r for r in caplog.records if r.msg == "presentation_late_completion")
    assert timeout.correlation_id == late.correlation_id
    assert late.elapsed_seconds >= timeout.elapsed_seconds
    assert "private values" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,enabled", [(False, False), (False, True), (True, False), (True, True)])
async def test_public_citation_annotations_survive_primary_guard(monkeypatch, stream, enabled):
    annotation = {"type": "url_citation", "url": "https://source.example.invalid/reviewed",
                  "title": "Authorized opaque citation", "opaque": {"grant": "fixture"}}
    content = Content.from_text("Narration agent_lz_presentation", annotations=[annotation])
    primary = AgentResponse(messages=[Message("assistant", [content], message_id="primary")])
    before = copy.deepcopy(primary.messages[0].contents[0].annotations)
    async def no_visual(*args, **kwargs):
        return None
    monkeypatch.setattr(presentation_middleware, "stage", no_visual)
    async def updates():
        yield AgentResponseUpdate(role="assistant", message_id="primary", contents=[content])
    context = SimpleNamespace(stream=stream, messages=[Message("user", ["Question"])])
    async def next_call():
        context.result = ResponseStream(updates(), finalizer=lambda _: primary) if stream else primary
    await presentation_middleware.compose(context, next_call, None, ({}, "0" * 64) if enabled else None, "a")
    if stream:
        emitted = [u async for u in context.result]
        final = await context.result.get_final_response()
        assert any(c.annotations == before for u in emitted for c in u.contents)
    else:
        final = context.result
    assert final.messages[0].contents[0].annotations == before
    assert primary.messages[0].contents[0].annotations == before
    assert primary.text == "Narration agent_lz_presentation"
    assert final.text == "Narration agent\\_lz\\_presentation"


@pytest.mark.asyncio
async def test_retry_processing_uses_one_absolute_budget(monkeypatch, caplog):
    from test_native_presentation import source
    clock = SimpleNamespace(value=0.0)
    data = source()
    calls = []
    async def work():
        # Controlled retry/processing clock, not independent per-attempt budgets.
        for _ in range(3):
            clock.value += 3.4
            calls.append(clock.value)
            await asyncio.sleep(0)
        return presentation.hydrate({"schemaVersion": 1, "blocks": [
            {"type": "suggestions", "prompts": ["Readable sibling"]},
        ]}, [data], "a", "0" * 64)
    with caplog.at_level("INFO"):
        assert await presentation.supervise(work(), clock=lambda: clock.value) is None
    assert len(calls) == 3
    assert caplog.records[-1].elapsed_seconds >= 10


@pytest.mark.asyncio
async def test_native_authorization_error_is_not_optional_fallback():
    context = SimpleNamespace(stream=False, messages=[])
    async def denied():
        raise PermissionError("native authorization denied")
    with pytest.raises(PermissionError, match="native authorization denied"):
        await presentation_middleware.compose(context, denied, None, ({}, "0" * 64), "a")
    assert not hasattr(context, "result")


@pytest.mark.parametrize("raw", ['{"fields":[],"fields":[]}', '{"v":NaN}', '{"v":Infinity}'])
def test_duplicate_and_nonfinite_inputs_reject_without_disclosure(raw):
    with pytest.raises(ValueError):
        presentation.loads(raw)


def test_exact_ten_blocks_and_cross_source_unit_rejection():
    from test_native_presentation import source
    data = source()
    ten = {"schemaVersion": 1, "blocks": [{"type": "suggestions", "prompts": [f"Suggestion {i}"]} for i in range(10)]}
    assert len(presentation.hydrate(ten, [data], "a", "0" * 64)["contributions"][0]["blocks"]) == 10
    selected = {"schemaVersion": 1, "blocks": [
        {"type": "table", "sourceId": "other-contribution", "columns": [{"field": "amount", "label": "Hidden"}]},
        {"type": "suggestions", "prompts": ["Readable sibling"]},
    ]}
    result = presentation.hydrate(selected, [data], "a", "0" * 64)
    assert result["markdown"] == "Readable sibling"
    assert "9007199254740993" not in result["markdown"]


def test_compact_utf8_exact_boundary_with_multibyte_title():
    selected = {"schemaVersion": 1, "blocks": [{"type": "suggestions", "title": "", "prompts": ["x" * 30000] * 4}]}
    baseline = presentation.hydrate(selected, [], "a", "0" * 64)
    remaining = presentation.MAX_BYTES - len(presentation.compact(baseline).encode("utf-8"))
    selected["blocks"][0]["title"] = "é" * (remaining // 2) + "x" * (remaining % 2)
    exact = presentation.hydrate(selected, [], "a", "0" * 64)
    assert len(presentation.compact(exact).encode("utf-8")) == presentation.MAX_BYTES
    selected["blocks"][0]["title"] += "x"
    with pytest.raises(ValueError, match="aggregate byte"):
        presentation.hydrate(selected, [], "a", "0" * 64)


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_wait_for_optional_child():
    entered, release = asyncio.Event(), asyncio.Event()
    async def child():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
    task = asyncio.create_task(presentation.supervise(child()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_native_cleanup_error_is_diagnosed_without_payload(caplog):
    primary = AgentResponse(messages=[Message("assistant", ["Authorized text"])])
    async def updates():
        yield AgentResponseUpdate(role="assistant", contents=[Content.from_text("Authorized text")])
    class FailingCleanup(ResponseStream):
        async def close(self):
            raise RuntimeError("private transport payload")
    context = SimpleNamespace(stream=True, messages=[])
    async def next_call():
        context.result = FailingCleanup(updates(), finalizer=lambda _: primary)
    await presentation_middleware.compose(context, next_call, None, None, "a")
    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="private transport payload"):
        async for _ in context.result:
            pass
    assert "private transport payload" not in caplog.text
    assert caplog.records[-1].reason == "native_stream_cleanup"
