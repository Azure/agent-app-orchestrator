import asyncio
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import jsonschema
import pytest
from azure.monitor.opentelemetry.exporter.export.logs._exporter import (
    _convert_log_to_envelope,
    _log_data_is_event,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider

from telemetry.audit import (
    AuditEmitter,
    begin_audit_request,
    end_audit_request,
    invoke_audited_tool,
    wrap_ai_functions,
)
from telemetry.audit_contract import (
    AUDIT_LOG_BODY,
    MAX_AUDIT_EVENTS,
    AuditSettings,
    AuditStatus,
    EventType,
    ReasonCode,
    ROOT_PARENT_EVENT_ID,
)


class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class RaisingHandler(logging.Handler):
    def emit(self, record):
        raise RuntimeError("exporter failed")


def enabled_emitter(*, source_limit=25, sensitive_fields=frozenset()):
    return AuditEmitter(
        AuditSettings(
            enabled=True,
            sensitive_content_enabled=bool(sensitive_fields),
            sensitive_content_fields=sensitive_fields,
            actor_pseudonym_enabled=False,
            source_event_limit=source_limit,
            hmac_key_id="v1",
            hmac_key=b"k" * 32,
            additional_redacted_keys=frozenset(),
        ),
        service_name="agent-app-orchestrator",
        service_version="3.7.0",
        environment="test",
    )


@contextmanager
def capture_audit_logs(handler=None):
    logger = logging.getLogger("gptrag.audit")
    previous = (list(logger.handlers), logger.propagate, logger.level)
    handler = handler or CaptureHandler()
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.handlers, logger.propagate, logger.level = previous


def event_types(handler):
    return [
        getattr(record, "event_type")
        for record in handler.records
        if hasattr(record, "event_type")
    ]


def test_fixed_body_custom_event_name_and_metadata_only_default():
    emitter = enabled_emitter()
    AuditEmitter._default = emitter

    with capture_audit_logs() as capture:
        emitter.emit(
            EventType.REQUEST_STARTED,
            operation="test",
            status=AuditStatus.STARTED,
            reason_code=ReasonCode.REQUEST_RECEIVED,
            sensitive={"prompt": "must not be exported"},
        )

    record = capture.records[0]
    assert record.getMessage() == AUDIT_LOG_BODY
    assert (
        getattr(record, "microsoft.custom_event.name")
        == "agentlz.audit.request.started"
    )
    assert not hasattr(record, "prompt")
    assert "prompt" in record.omitted_fields
    assert record.parent_event_id == ROOT_PARENT_EVENT_ID


def test_emitter_failure_never_escapes_to_user_operation():
    emitter = enabled_emitter()

    with capture_audit_logs(RaisingHandler()):
        assert (
            emitter.emit(
                EventType.REQUEST_STARTED,
                operation="test",
                status=AuditStatus.STARTED,
                reason_code=ReasonCode.REQUEST_RECEIVED,
            )
            is None
        )


def test_audit_warning_failure_also_preserves_primary_operation():
    emitter = enabled_emitter()
    from telemetry import audit

    warning_logger = audit._warning_logger
    previous = (list(warning_logger.handlers), warning_logger.propagate)
    warning_logger.handlers = [RaisingHandler()]
    warning_logger.propagate = False
    try:
        with capture_audit_logs(RaisingHandler()):
            assert emitter.emit(
                EventType.REQUEST_STARTED,
                operation="test",
                status=AuditStatus.STARTED,
                reason_code=ReasonCode.REQUEST_RECEIVED,
            ) is None
    finally:
        warning_logger.handlers, warning_logger.propagate = previous


async def test_real_tool_audit_sink_failure_preserves_primary_unwind(monkeypatch):
    # One exact JUnit selector binds the entire matrix, not just a passing cell.
    for sink in ("export", "failure_event", "warning"):
        for error_type in (
            RuntimeError, asyncio.CancelledError, KeyboardInterrupt, SystemExit,
            GeneratorExit,
        ):
            for outcome in ("success", "failure", "timeout", "cancellation"):
                for phase in ("started", "terminal"):
                    with monkeypatch.context() as patch:
                        await _assert_tool_audit_sink_outcome(
                            patch, sink, error_type, outcome, phase,
                        )


async def test_successful_recovery_direct_await_propagates_audit_control(monkeypatch):
    # Direct await retains the outer except's ambient exception state.
    try:
        raise ValueError("recovered-private")
    except ValueError:
        for sink in ("export", "failure_event", "warning"):
            for error_type in (
                RuntimeError, asyncio.CancelledError, KeyboardInterrupt,
                SystemExit, GeneratorExit,
            ):
                for phase in ("started", "terminal"):
                    with monkeypatch.context() as patch:
                        await _assert_tool_audit_sink_outcome(
                            patch, sink, error_type, "success", phase,
                        )


def test_recovery_direct_emit_failure_propagates_control(monkeypatch):
    try:
        raise ValueError("recovered-private")
    except ValueError:
        test_direct_failure_event_control_propagates_and_guard_recovers(monkeypatch)


def test_recovery_direct_emit_propagates_control():
    emitter = enabled_emitter()
    for error_type in (
        asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit,
    ):
        error = error_type("sink-private")

        class ControlHandler(logging.Handler):
            def emit(self, record):
                raise error

        try:
            raise ValueError("recovered-private")
        except ValueError:
            with capture_audit_logs(ControlHandler()), pytest.raises(error_type) as raised:
                emitter.emit(
                    EventType.REQUEST_STARTED,
                    operation="test",
                    status=AuditStatus.STARTED,
                    reason_code=ReasonCode.REQUEST_RECEIVED,
                )
            assert raised.value is error


async def _assert_tool_audit_sink_outcome(
    monkeypatch, sink, sink_error_type, outcome, phase,
):
    """A secondary control exception must not replace an unwinding primary."""
    from telemetry import audit

    emitter = enabled_emitter()
    monkeypatch.setattr(AuditEmitter, "_default", emitter)
    primary = (
        asyncio.CancelledError("primary-private")
        if outcome == "cancellation"
        else TimeoutError("primary-private")
        if outcome == "timeout"
        else RuntimeError("primary-private")
    )
    sink_error = sink_error_type("sink-private")
    attempts = []
    invoked = False

    class FailingTerminalHandler(logging.Handler):
        def emit(self, record):
            attempts.append(record)
            if phase == "terminal" and record.event_type == EventType.TOOL_STARTED.value:
                return
            if sink == "export" or (
                sink == "failure_event"
                and record.event_type == EventType.EMISSION_FAILED.value
            ):
                raise sink_error
            raise RuntimeError("first-export-private")

    class WarningHandler(logging.Handler):
        def emit(self, record):
            attempts.append(record)
            if sink == "warning":
                raise sink_error

    monkeypatch.setattr(audit._warning_logger, "handlers", [WarningHandler()])
    monkeypatch.setattr(audit._warning_logger, "propagate", False)
    monkeypatch.setattr(audit._warning_logger, "level", logging.WARNING)

    async def invocation():
        nonlocal invoked
        invoked = True
        if outcome != "success":
            raise primary
        return "result"

    _, token = begin_audit_request()
    try:
        with capture_audit_logs(FailingTerminalHandler()):
            if phase == "started" and sink_error_type is not RuntimeError:
                with pytest.raises(sink_error_type) as raised:
                    await invoke_audited_tool("test", invocation)
                assert raised.value is sink_error
                assert not invoked
            elif outcome != "success":
                with pytest.raises(type(primary)) as raised:
                    await invoke_audited_tool("test", invocation)
                assert raised.value is primary
            elif sink_error_type is RuntimeError:
                assert await invoke_audited_tool("test", invocation) == "result"
            else:
                with pytest.raises(sink_error_type) as raised:
                    await invoke_audited_tool("test", invocation)
                assert raised.value is sink_error
    finally:
        end_audit_request(token)

    assert len(attempts) <= 4
    assert sum(
        getattr(record, "event_type", None) == EventType.EMISSION_FAILED.value
        for record in attempts
    ) <= 1
    assert sum(record.levelno == logging.WARNING for record in attempts) <= 1
    assert not audit._failure_emission_active.get()
    serialized = str([record.__dict__ for record in attempts])
    assert "primary-private" not in serialized
    assert "sink-private" not in serialized
    assert "first-export-private" not in serialized


def test_direct_failure_event_control_propagates_and_guard_recovers(monkeypatch):
    for sink in ("failure_event", "warning"):
        for error_type in (
            asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit,
        ):
            with monkeypatch.context() as patch:
                _assert_direct_failure_control(patch, sink, error_type)


def _assert_direct_failure_control(monkeypatch, sink, error_type):
    from telemetry import audit

    emitter = enabled_emitter()
    error = error_type("sink-private")

    class ControlHandler(logging.Handler):
        def emit(self, record):
            raise error

    monkeypatch.setattr(audit._warning_logger, "handlers", [ControlHandler()])
    monkeypatch.setattr(audit._warning_logger, "propagate", False)
    monkeypatch.setattr(audit._warning_logger, "level", logging.WARNING)
    handler = ControlHandler() if sink == "failure_event" else RaisingHandler()
    with capture_audit_logs(handler), pytest.raises(error_type) as raised:
        emitter.emit_failure(ReasonCode.EXPORT_FAILURE)
    assert raised.value is error
    assert not audit._failure_emission_active.get()
    with capture_audit_logs() as capture:
        emitter.emit_failure(ReasonCode.EXPORT_FAILURE)
    assert event_types(capture) == [EventType.EMISSION_FAILED.value]


async def test_real_tool_task_cancellation_survives_ordinary_audit_sink_failures(
    monkeypatch,
):
    from telemetry import audit

    monkeypatch.setattr(AuditEmitter, "_default", enabled_emitter())
    monkeypatch.setattr(audit._warning_logger, "handlers", [RaisingHandler()])
    monkeypatch.setattr(audit._warning_logger, "propagate", False)
    entered = asyncio.Event()
    cleaned = []

    class TerminalFailureHandler(RaisingHandler):
        def emit(self, record):
            if record.event_type != EventType.TOOL_STARTED.value:
                super().emit(record)

    async def invocation():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    _, token = begin_audit_request()
    try:
        with capture_audit_logs(TerminalFailureHandler()):
            task = asyncio.create_task(invoke_audited_tool("test", invocation))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert cleaned == [True]
    finally:
        end_audit_request(token)
    assert not audit._failure_emission_active.get()


def test_audit_environment_lookup_failure_is_metadata_only(monkeypatch):
    from unittest.mock import MagicMock

    settings = enabled_emitter().settings
    monkeypatch.setattr(AuditSettings, "from_config", lambda config: settings)
    monkeypatch.setattr(AuditEmitter, "_default", None)
    config = MagicMock()
    config.get.side_effect = RuntimeError("synthetic-private-config")
    emitter = AuditEmitter.configure(
        config, service_name="agent-app-orchestrator", service_version="test",
    )
    assert emitter.environment == "unknown"
    assert emitter.enabled


def test_redaction_failure_discards_payload_and_emits_minimal_failure():
    emitter = enabled_emitter()

    with capture_audit_logs() as capture:
        result = emitter.emit(
            EventType.REQUEST_STARTED,
            operation="test",
            status=AuditStatus.STARTED,
            reason_code=ReasonCode.REQUEST_RECEIVED,
            metadata={"duration_ms": float("nan")},
            sensitive={"prompt": "payload-to-discard"},
        )

    assert result is None
    assert event_types(capture) == ["audit.emission.failed"]
    assert "payload-to-discard" not in str(capture.records[0].__dict__)


def test_failure_event_uses_only_constant_safe_metadata(monkeypatch):
    from telemetry.audit_sanitizer import AuditSanitizationError

    secret = "super-secret-config-value"
    emitter = AuditEmitter(
        AuditSettings(
            enabled=True,
            sensitive_content_enabled=True,
            sensitive_content_fields=frozenset({"prompt"}),
            actor_pseudonym_enabled=False,
            source_event_limit=25,
            hmac_key_id=secret,
            hmac_key=b"k" * 32,
            additional_redacted_keys=frozenset({secret}),
        ),
        service_name=secret,
        service_version=secret,
        environment=secret,
    )
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    try:
        monkeypatch.setattr(
            "telemetry.audit.sanitize_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AuditSanitizationError(secret)
            ),
        )
        with capture_audit_logs() as capture:
            for _ in range(3):
                emitter.emit(
                    EventType.REQUEST_STARTED,
                    operation=secret,
                    status=AuditStatus.STARTED,
                    reason_code=ReasonCode.REQUEST_RECEIVED,
                    metadata={"decision_value": secret},
                    sensitive={"prompt": secret},
                )
    finally:
        end_audit_request(token)

    assert event_types(capture) == ["audit.emission.failed"]
    exporter_input = json.dumps(
        capture.records[0].__dict__, default=str, sort_keys=True
    )
    assert secret not in exporter_input
    assert capture.records[0].service_name == "agent-app-orchestrator"
    assert capture.records[0].environment == "unknown"
    assert capture.records[0].correlation_id == context.correlation_id


def test_enabled_sensitive_allowlist_still_redacts_prohibited_values():
    emitter = enabled_emitter(sensitive_fields=frozenset({"prompt"}))

    with capture_audit_logs() as capture:
        emitter.emit(
            EventType.REQUEST_STARTED,
            operation="test",
            status=AuditStatus.STARTED,
            reason_code=ReasonCode.REQUEST_RECEIVED,
            sensitive={
                "prompt": {
                    "question": "safe text",
                    "access_token": "must-not-export",
                }
            },
        )

    record = capture.records[0]
    assert "safe text" in record.prompt
    assert "must-not-export" not in record.prompt
    assert record.redaction_applied is True


def test_in_memory_otel_log_has_current_trace_and_span_context():
    emitter = enabled_emitter()
    log_provider = LoggerProvider()
    log_exporter = InMemoryLogRecordExporter()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    trace_provider = TracerProvider()

    with capture_audit_logs(LoggingHandler(logger_provider=log_provider)):
        with trace_provider.get_tracer("audit-test").start_as_current_span(
            "request"
        ) as span:
            emitter.emit(
                EventType.REQUEST_STARTED,
                operation="test",
                status=AuditStatus.STARTED,
                reason_code=ReasonCode.REQUEST_RECEIVED,
            )
            expected_trace = span.get_span_context().trace_id
            expected_span = span.get_span_context().span_id

    readable = log_exporter.get_finished_logs()[0]
    assert readable.log_record.trace_id == expected_trace
    assert readable.log_record.span_id == expected_span
    assert _log_data_is_event(readable) is True
    assert len(readable.log_record.attributes) <= 64
    envelope = _convert_log_to_envelope(readable)
    assert (
        envelope.data.base_data.properties["parent_event_id"]
        == ROOT_PARENT_EVENT_ID
    )
    wire_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "audit-event-v2.application-insights.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(wire_schema).validate(
        {
            "name": envelope.data.base_data.name,
            "properties": dict(envelope.data.base_data.properties),
        }
    )


@pytest.mark.asyncio
async def test_tool_success_failure_timeout_and_cancellation_events():
    emitter = enabled_emitter()
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    context.request_started_event_id = "evt_" + ("1" * 32)

    async def fail():
        raise ValueError("raw failure must not be exported")

    async def timeout():
        raise TimeoutError()

    async def cancel():
        raise asyncio.CancelledError()

    try:
        with capture_audit_logs() as capture:
            assert await invoke_audited_tool("ok", lambda: "done") == "done"
            with pytest.raises(ValueError):
                await invoke_audited_tool("fail", fail)
            with pytest.raises(TimeoutError):
                await invoke_audited_tool("timeout", timeout)
            with pytest.raises(asyncio.CancelledError):
                await invoke_audited_tool("cancel", cancel)
    finally:
        end_audit_request(token)

    assert event_types(capture) == [
        "tool.invocation.started",
        "tool.invocation.completed",
        "tool.invocation.started",
        "tool.invocation.failed",
        "tool.invocation.started",
        "tool.invocation.failed",
        "tool.invocation.started",
        "tool.invocation.cancelled",
    ]
    serialized = " ".join(str(record.__dict__) for record in capture.records)
    assert "raw failure must not be exported" not in serialized


@pytest.mark.asyncio
async def test_request_and_tool_budgets_bound_export_and_preserve_terminal():
    emitter = enabled_emitter()
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    try:
        with capture_audit_logs() as capture:
            context.request_started_event_id = emitter.emit(
                EventType.REQUEST_STARTED,
                operation="test",
                status=AuditStatus.STARTED,
                reason_code=ReasonCode.REQUEST_RECEIVED,
            )
            for rank in range(100):
                emitter.emit_source(
                    selected=True,
                    source_type="test",
                    source_reference=f"source-{rank}",
                    source_rank=rank,
                )
            for index in range(100):
                assert await invoke_audited_tool(
                    f"tool-{index}", lambda: "done"
                ) == "done"
            for _ in range(100):
                emitter.emit(
                    EventType.ROUTE_SELECTED,
                    operation="test",
                    status=AuditStatus.SELECTED,
                    reason_code=ReasonCode.STRATEGY_CONFIGURED,
                )
            emitter.emit(
                EventType.REQUEST_COMPLETED,
                operation="test",
                status=AuditStatus.COMPLETED,
                reason_code=ReasonCode.REQUEST_COMPLETED,
                started_at=context.started_at,
                duration_ms=1,
            )
    finally:
        end_audit_request(token)

    assert len(capture.records) <= MAX_AUDIT_EVENTS
    assert event_types(capture)[-1] == "request.completed"
    terminal = capture.records[-1]
    assert terminal.audit_events_omitted > 0
    assert terminal.source_events_omitted == 75
    assert terminal.tool_invocations_omitted == 84


def test_reserved_failure_survives_terminal_sanitization_failure():
    emitter = enabled_emitter()
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    try:
        with capture_audit_logs() as capture:
            for _ in range(MAX_AUDIT_EVENTS - 2):
                emitter.emit(
                    EventType.ROUTE_SELECTED,
                    operation="test",
                    status=AuditStatus.SELECTED,
                    reason_code=ReasonCode.STRATEGY_CONFIGURED,
                )
            emitter.emit(
                EventType.REQUEST_COMPLETED,
                operation="test",
                status=AuditStatus.COMPLETED,
                reason_code=ReasonCode.REQUEST_COMPLETED,
                started_at=context.started_at,
                duration_ms=float("inf"),
            )
    finally:
        end_audit_request(token)

    assert len(capture.records) == MAX_AUDIT_EVENTS - 1
    assert event_types(capture)[-1] == "audit.emission.failed"


def test_source_events_are_hmac_pseudonymized_and_hard_limited():
    emitter = enabled_emitter(source_limit=2)
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    context.request_started_event_id = "evt_" + ("1" * 32)
    try:
        with capture_audit_logs() as capture:
            for rank in range(10):
                emitter.emit_source(
                    selected=True,
                    source_type="test",
                    source_reference=f"https://secret.example/{rank}?sig=nope",
                    source_rank=rank,
                )
    finally:
        end_audit_request(token)

    assert event_types(capture) == [
        "grounding.source.selected",
        "grounding.source.selected",
    ]
    assert all(record.source_id.startswith("hmac_") for record in capture.records)
    assert "secret.example" not in str([record.__dict__ for record in capture.records])


def test_source_excerpt_is_captured_only_when_explicitly_allowlisted():
    emitter = enabled_emitter(
        sensitive_fields=frozenset({"source_excerpt"})
    )
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    context.request_started_event_id = "evt_" + ("1" * 32)
    try:
        with capture_audit_logs() as capture:
            emitter.emit_source(
                selected=True,
                source_type="azure_ai_search",
                source_reference="document",
                source_excerpt="approved excerpt",
            )
    finally:
        end_audit_request(token)

    assert capture.records[0].source_excerpt == "approved excerpt"


@pytest.mark.asyncio
async def test_public_maf_function_proxy_preserves_contract_and_audits_call():
    from agent_framework import FunctionTool

    async def echo(value: str) -> str:
        return value

    original = FunctionTool(name="echo", description="Echo", func=echo)
    wrapped = wrap_ai_functions([original], tool_kind="mcp")
    emitter = enabled_emitter()
    AuditEmitter._default = emitter
    context, token = begin_audit_request()
    context.request_started_event_id = "evt_" + ("1" * 32)
    try:
        with capture_audit_logs() as capture:
            result = await wrapped[0].invoke(arguments={"value": "hello"}, skip_parsing=True)
    finally:
        end_audit_request(token)

    assert result == "hello"
    assert wrapped[0].name == original.name
    assert wrapped[0].input_model is original.input_model
    assert event_types(capture) == [
        "tool.invocation.started",
        "tool.invocation.completed",
    ]
