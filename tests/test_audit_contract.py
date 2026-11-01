import ast
import base64
import hashlib
import json
import re
import tracemalloc
from collections.abc import Mapping, Sequence
from pathlib import Path

import jsonschema
import pytest

from telemetry.audit_contract import (
    AUDIT_EVENT_PREFIX,
    INGESTION_EVENT_TYPES,
    MAX_EVENT_BYTES,
    ROOT_PARENT_EVENT_ID,
    SCHEMA_VERSION,
    SERVICE_NAME,
    AuditConfigurationError,
    AuditSettings,
    EventType,
    format_utc,
    logical_parent_to_wire,
    new_correlation_id,
    new_event_id,
    utc_now,
    wire_parent_to_logical,
)
from telemetry.audit_sanitizer import REDACTED, sanitize_event


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_INGESTION_EVENT_TYPES = frozenset(
    {
        "ingestion.run.started",
        "ingestion.run.completed",
        "ingestion.run.failed",
        "ingestion.run.cancelled",
        "ingestion.document.indexed",
        "ingestion.document.rejected",
        "ingestion.document.deleted",
    }
)
CURRENT_LOGICAL = "audit-event-v2.schema.json"
CURRENT_WIRE = "audit-event-v2.application-insights.schema.json"
PINNED_CONTRACTS = {
    # Current contract: Agent Landing Zone audit-event-v2 (agentlz.audit.*).
    "audit-event-v2": {
        "audit-event-v2.schema.json": "884dfa2441d3313c8ec46a099f60ce86e7abb6cdf88bb5b5da720463edbf5e97",
        "audit-event-v2.application-insights.schema.json": "48416073768c0710b9a1f58640d4e822745f28b3a17b3712a2fe4cd9326c9c07",
    },
    # Historical contract kept hash-pinned for readers of older events.
    "audit-event-v1": {
        "audit-event-v1.schema.json": "825db8ef40a81e2c19e5d80d37c565b6b47fc9a6540e9881d35cc12b8fde5aab",
        "audit-event-v1.application-insights.schema.json": "066c8f5408610ab839d5121d06ca5bc59e8797e551d5c47c875c5ba52f7e0588",
    },
}


def _schema(name):
    return json.loads((ROOT / "contracts" / name).read_text())


class Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None, **_kwargs):
        return self.values.get(key, default)


def _base_event():
    return {
        "schema_version": 2,
        "event_id": new_event_id(),
        "event_type": "request.started",
        "event_time_utc": format_utc(utc_now()),
        "correlation_id": new_correlation_id(),
        "trace_id": "0" * 32,
        "span_id": "0" * 16,
        "parent_event_id": None,
        "service_name": "agent-app-orchestrator",
        "service_version": "3.7.0",
        "environment": "test",
        "operation": "test",
        "status": "started",
        "reason_code": "request_received",
        "capture_mode": "metadata_only",
        "redaction_applied": False,
        "omitted_fields": [],
        "truncated_fields": [],
    }


def _as_application_insights_event(event, prefix=AUDIT_EVENT_PREFIX):
    def stringify(value):
        if value is None:
            return ROOT_PARENT_EVENT_ID
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, dict)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    return {
        "name": f"{prefix}{event['event_type']}",
        "properties": {key: stringify(value) for key, value in event.items()},
    }


def test_producer_constants_match_current_contract():
    logical_schema = _schema(CURRENT_LOGICAL)
    assert SCHEMA_VERSION == logical_schema["properties"]["schema_version"]["const"] == 2
    assert AUDIT_EVENT_PREFIX == "agentlz.audit."
    assert SERVICE_NAME == "agent-app-orchestrator"


def test_runtime_audit_service_name_matches_contract_producer():
    tree = ast.parse((ROOT / "src" / "native" / "tools.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AuditEmitter"
    ]
    assert len(calls) == 1
    (service_name,) = [kw.value for kw in calls[0].keywords if kw.arg == "service_name"]
    assert isinstance(service_name, ast.Constant) and service_name.value == SERVICE_NAME


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        (CURRENT_LOGICAL, "audit_event_v2.json"),
        ("audit-event-v1.schema.json", "audit_event_v1.json"),
    ],
)
def test_golden_event_validates_against_shared_schema(schema_name, fixture_name):
    schema = _schema(schema_name)
    golden = json.loads((ROOT / "tests" / "golden" / fixture_name).read_text())

    jsonschema.Draft202012Validator(schema).validate(golden)


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        (CURRENT_LOGICAL, "audit_event_v2_root.json"),
        ("audit-event-v1.schema.json", "audit_event_v1_root.json"),
    ],
)
def test_root_golden_validates_and_translates_only_at_wire_boundary(
    schema_name, fixture_name
):
    schema = _schema(schema_name)
    golden = json.loads((ROOT / "tests" / "golden" / fixture_name).read_text())

    jsonschema.Draft202012Validator(schema).validate(golden)
    assert golden["parent_event_id"] is None
    assert logical_parent_to_wire(golden["parent_event_id"]) == ROOT_PARENT_EVENT_ID


@pytest.mark.parametrize(
    ("version", "prefix", "fixture_name"),
    [
        ("v2", "agentlz.audit.", "audit_event_v2_ingestion_run.json"),
        ("v2", "agentlz.audit.", "audit_event_v2_ingestion_document.json"),
        ("v1", "gptrag.audit.", "audit_event_v1_ingestion_run.json"),
        ("v1", "gptrag.audit.", "audit_event_v1_ingestion_document.json"),
    ],
)
def test_ingestion_goldens_validate_against_logical_and_wire_schemas(
    version, prefix, fixture_name
):
    logical_schema = _schema(f"audit-event-{version}.schema.json")
    wire_schema = _schema(f"audit-event-{version}.application-insights.schema.json")
    golden = json.loads((ROOT / "tests" / "golden" / fixture_name).read_text())

    jsonschema.Draft202012Validator(logical_schema).validate(golden)
    jsonschema.Draft202012Validator(wire_schema).validate(
        _as_application_insights_event(golden, prefix)
    )


def test_v2_wire_schema_rejects_retired_event_prefix():
    wire_schema = _schema(CURRENT_WIRE)
    golden = json.loads(
        (ROOT / "tests" / "golden" / "audit_event_v2.json").read_text()
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(wire_schema).validate(
            _as_application_insights_event(golden, "gptrag.audit.")
        )


def test_ingestion_taxonomy_is_exact_across_python_and_both_schemas():
    logical_schema = _schema(CURRENT_LOGICAL)
    wire_schema = _schema(CURRENT_WIRE)
    orchestrator_event_types = {event_type.value for event_type in EventType}
    expected_event_types = orchestrator_event_types | EXPECTED_INGESTION_EVENT_TYPES

    assert INGESTION_EVENT_TYPES == EXPECTED_INGESTION_EVENT_TYPES
    assert set(logical_schema["properties"]["event_type"]["enum"]) == expected_event_types
    assert (
        set(wire_schema["properties"]["properties"]["properties"]["event_type"]["enum"])
        == expected_event_types
    )
    assert {
        name.removeprefix("agentlz.audit.")
        for name in wire_schema["properties"]["name"]["enum"]
        if name.startswith("agentlz.audit.")
    } == expected_event_types
    assert all(
        name.startswith("agentlz.audit.")
        for name in wire_schema["properties"]["name"]["enum"]
    )


def test_legacy_ingestion_aliases_are_rejected_by_both_schemas():
    logical_schema = _schema(CURRENT_LOGICAL)
    wire_schema = _schema(CURRENT_WIRE)
    legacy_aliases = {
        f"ingestion.{scope}.{action}"
        for scope, action in (
            ("request", "started"),
            ("request", "completed"),
            ("request", "failed"),
            ("request", "cancelled"),
            ("document", "selected"),
            ("outcome", "produced"),
            ("outcome", "rejected"),
        )
    }

    for alias in legacy_aliases:
        event = _base_event()
        event["event_type"] = alias
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(logical_schema).validate(event)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(wire_schema).validate(
                _as_application_insights_event(event)
            )


@pytest.mark.parametrize("contract", sorted(PINNED_CONTRACTS))
def test_published_contract_hashes_match_artifacts(contract):
    expected = {}
    for line in (ROOT / "contracts" / f"{contract}.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        expected[name] = digest

    assert expected == PINNED_CONTRACTS[contract]
    for name, digest in expected.items():
        content = (ROOT / "contracts" / name).read_bytes()
        if contract == "audit-event-v1":
            # v1 predates the -text attribute; tolerate CRLF checkouts.
            content = content.replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == digest


def test_ids_and_timestamp_have_canonical_shapes():
    assert re.fullmatch(r"evt_[0-9a-f]{32}", new_event_id())
    assert re.fullmatch(r"req_[0-9a-f]{32}", new_correlation_id())
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
        format_utc(utc_now()),
    )


def test_root_parent_logical_wire_conversion_is_lossless():
    child = "evt_" + ("1" * 32)

    assert logical_parent_to_wire(None) == ROOT_PARENT_EVENT_ID
    assert logical_parent_to_wire(child) == child
    assert wire_parent_to_logical(ROOT_PARENT_EVENT_ID) is None
    assert wire_parent_to_logical(child) == child


def test_disabled_settings_need_no_hmac_key():
    settings = AuditSettings.from_config(Config())

    assert settings.enabled is False
    assert settings.hmac_key is None
    assert settings.sensitive_content_fields == frozenset()
    assert settings.source_event_limit == 25


def test_disabled_settings_ignore_invalid_inactive_values():
    settings = AuditSettings.from_config(
        Config(
            {
                "AUDIT_EVENTS_ENABLED": "false",
                "AUDIT_HMAC_KEY": "invalid",
                "AUDIT_SOURCE_EVENT_LIMIT": "not-an-integer",
                "AUDIT_SENSITIVE_CONTENT_FIELDS": "unknown",
            }
        )
    )

    assert settings.enabled is False


def test_config_provider_controls_environment_precedence(monkeypatch):
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("AUDIT_EVENTS_ENABLED", "false")
    settings = AuditSettings.from_config(
        Config(
            {
                "AUDIT_EVENTS_ENABLED": "true",
                "AUDIT_HMAC_KEY": encoded,
            }
        )
    )

    assert settings.enabled is True


@pytest.mark.parametrize("key", ["", "short", base64.b64encode(b"x" * 31).decode()])
def test_enabled_settings_reject_non_256_bit_hmac_keys(key):
    with pytest.raises(AuditConfigurationError):
        AuditSettings.from_config(
            Config({"AUDIT_EVENTS_ENABLED": "true", "AUDIT_HMAC_KEY": key})
        )


def test_enabled_settings_accept_base64url_256_bit_key():
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    settings = AuditSettings.from_config(
        Config({"AUDIT_EVENTS_ENABLED": "true", "AUDIT_HMAC_KEY": encoded})
    )

    assert settings.hmac_key == b"k" * 32


def test_sensitive_allowlist_rejects_unknown_field_even_when_empty_is_safe():
    empty = AuditSettings.from_config(
        Config(
            {
                "AUDIT_SENSITIVE_CONTENT_ENABLED": "true",
                "AUDIT_SENSITIVE_CONTENT_FIELDS": "",
            }
        )
    )
    assert empty.capture_mode.value == "metadata_only"

    with pytest.raises(AuditConfigurationError):
        AuditSettings.from_config(
            Config(
                {
                    "AUDIT_EVENTS_ENABLED": "true",
                    "AUDIT_HMAC_KEY": base64.urlsafe_b64encode(
                        b"k" * 32
                    ).decode(),
                    "AUDIT_SENSITIVE_CONTENT_FIELDS": "prompt,unknown",
                }
            )
        )


def test_recursive_redaction_handles_cycles_unknown_objects_and_bounds():
    cycle = {}
    cycle["self"] = cycle
    event = _base_event()
    event.update(
        {
            "tool_arguments": {
                "Authorization": "Bearer definitely-secret-token",
                "nested": {
                    "client-secret": "s3cr3t",
                    "safe": "x" * 4000,
                    "cycle": cycle,
                    "unknown": object(),
                },
            },
            "decision_value": "y" * 900,
        }
    )

    result = sanitize_event(
        event,
        additional_redacted_keys=frozenset({"tenant-private-value"}),
    )

    assert "definitely-secret-token" not in result.serialized
    assert "s3cr3t" not in result.serialized
    assert REDACTED in result.serialized
    assert len(result.serialized.encode()) <= MAX_EVENT_BYTES
    assert result.attributes["redaction_applied"] is True
    assert result.attributes["truncated_fields"]
    assert any("cycle" in field or "unknown" in field for field in result.attributes["omitted_fields"])


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "AccountKey=abcdefghijklmnopqrstuvwxyz012345",
        "SharedAccessSignature=abcdefghijklmnopqrstuvwxyz",
        "https://example.test/path?sig=abcdefghijklmnopqrstuvwxyz&sv=2026",
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "eyJabcdefgh.ijklmnop.qrstuvwxyz",
        '{"api_key":"supersecret123"}',
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Cookie: session=supersecret123",
        "https://user:password@example.test/path",
        '{"password":"abc"}',
        '{"api_key":"xyz"}',
    ],
)
def test_prohibited_values_never_reach_serialized_exporter_input(secret):
    event = _base_event()
    event["source_excerpt"] = {"value": secret}

    result = sanitize_event(
        event,
        additional_redacted_keys=frozenset(),
    )

    assert secret not in result.serialized
    assert REDACTED in result.serialized


def test_unknown_optional_fields_are_omitted_for_major_version_one():
    event = _base_event()
    event["future_field"] = "reader must ignore this"

    result = sanitize_event(event, additional_redacted_keys=frozenset())

    assert "future_field" not in result.attributes
    assert "future_field" in result.attributes["omitted_fields"]


def test_recursive_key_denylist_covers_prohibited_credential_classes():
    event = _base_event()
    event["tool_arguments"] = {
        "proxy-auth": "secret-a",
        "cookies": "secret-b",
        "refresh_token": "secret-c",
        "id-token": "secret-d",
        "api.client.password": "secret-e",
        "database_connection_string": "secret-f",
        "sas": "secret-g",
        "certificate_credential": "secret-h",
    }

    result = sanitize_event(event, additional_redacted_keys=frozenset())

    for secret in "abcdefgh":
        assert f"secret-{secret}" not in result.serialized
    assert result.serialized.count(REDACTED) >= 8


def test_additional_redaction_keys_cannot_corrupt_required_identifiers():
    event = _base_event()
    event["tool_arguments"] = {"nested_id": "redact-me"}

    result = sanitize_event(
        event,
        additional_redacted_keys=frozenset({"id"}),
    )

    assert result.attributes["event_id"].startswith("evt_")
    assert result.attributes["correlation_id"].startswith("req_")
    assert "redact-me" not in result.serialized


def test_audit_config_lookup_failure_retains_disabled_defaults():
    class UnavailableConfig:
        def get(self, key, default=None):
            raise RuntimeError("synthetic-private-config")

    settings = AuditSettings.from_config(UnavailableConfig())
    assert settings.enabled is False
    assert settings.sensitive_content_enabled is False
    assert settings.actor_pseudonym_enabled is False


@pytest.mark.parametrize("failure_phase", ["items", "iteration"])
def test_audit_mapping_failure_omits_unreadable_value_without_content(failure_phase):
    class UnreadableMapping(dict):
        def items(self):
            if failure_phase == "items":
                raise RuntimeError("synthetic-private-value")

            def values():
                yield "safe", 1
                raise RuntimeError("synthetic-private-value")

            return values()

    event = _base_event()
    event["tool_arguments"] = UnreadableMapping()
    result = sanitize_event(event, additional_redacted_keys=frozenset())
    assert "tool_arguments" in result.attributes["omitted_fields"]
    assert "tool_arguments" not in result.attributes
    assert "synthetic-private-value" not in json.dumps(result.attributes)


@pytest.mark.parametrize("field", ["tool_arguments", "omitted_fields", "truncated_fields"])
def test_audit_sequence_failure_is_omitted_and_never_serialized(field):
    class UnreadableSequence(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            raise RuntimeError("synthetic-private-value")

        def __iter__(self):
            raise RuntimeError("synthetic-private-value")

    event = _base_event()
    event[field] = UnreadableSequence()
    result = sanitize_event(event, additional_redacted_keys=frozenset())
    assert field in result.attributes["omitted_fields"]
    assert "synthetic-private-value" not in json.dumps(result.attributes)


def test_sanitizer_uses_bounded_iteration_for_virtual_containers():
    class CountingSequence(Sequence):
        def __init__(self):
            self.iterations = 0

        def __len__(self):
            return 10**12

        def __getitem__(self, index):
            self.iterations += 1
            if self.iterations > 33:
                raise AssertionError("sequence was iterated beyond its bound")
            return index

    class CountingMapping(Mapping):
        def __init__(self):
            self.iterations = 0

        def __len__(self):
            return 10**12

        def __getitem__(self, key):
            raise KeyError(key)

        def __iter__(self):
            raise AssertionError("items() must be used")

        def items(self):
            for index in range(10**12):
                self.iterations += 1
                if self.iterations > 65:
                    raise AssertionError("mapping was iterated beyond its bound")
                yield str(index), index

    class UnknownInfiniteIterable:
        def __iter__(self):
            raise AssertionError("unknown iterables must not be inspected")

    sequence = CountingSequence()
    mapping = CountingMapping()
    event = _base_event()
    event["tool_arguments"] = {
        "sequence": sequence,
        "mapping": mapping,
        "unknown": UnknownInfiniteIterable(),
    }

    tracemalloc.start()
    try:
        result = sanitize_event(event, additional_redacted_keys=frozenset())
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sequence.iterations == 33
    assert mapping.iterations == 65
    assert peak_bytes < 2 * 1024 * 1024
    assert "tool_arguments.unknown" in result.attributes["omitted_fields"]
    assert "tool_arguments.sequence" in result.attributes["truncated_fields"]
    assert "tool_arguments.mapping" in result.attributes["truncated_fields"]


def test_sanitizer_bounds_work_before_scanning_oversized_strings():
    event = _base_event()
    event["source_excerpt"] = "\x01" * (8 * 1024 * 1024)

    tracemalloc.start()
    try:
        result = sanitize_event(event, additional_redacted_keys=frozenset())
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.attributes["source_excerpt"] == ""
    assert "source_excerpt" in result.attributes["truncated_fields"]
    assert peak_bytes < 2 * 1024 * 1024


def test_oversized_nested_key_is_omitted_before_redaction_classification():
    event = _base_event()
    key = ("a" * 512) + "_password"
    event["tool_arguments"] = {key: "short-sensitive-credential"}

    result = sanitize_event(event, additional_redacted_keys=frozenset())

    assert "short-sensitive-credential" not in result.serialized
    assert any(
        field.startswith("tool_arguments.")
        for field in result.attributes["omitted_fields"]
    )
    assert all(
        len(field) <= 512
        for field in (
            result.attributes["omitted_fields"]
            + result.attributes["truncated_fields"]
        )
    )


@pytest.mark.parametrize("container_kind", ["mapping", "sequence"])
@pytest.mark.parametrize("failure_stage", ["preparation", "iteration"])
def test_sanitizer_container_failure_discards_partial_data_and_releases_identity(
    container_kind, failure_stage,
):
    def failing_iterator(item):
        yield item
        raise RuntimeError("synthetic-private-error")

    class OnceFailingMapping(dict):
        failed = False

        def items(self):
            if not self.failed:
                self.failed = True
                if failure_stage == "preparation":
                    raise RuntimeError("synthetic-private-error")
                return failing_iterator(("partial", "synthetic-private-value"))
            return super().items()

    class OnceFailingSequence(list):
        failed = False

        def __iter__(self):
            if not self.failed:
                self.failed = True
                if failure_stage == "preparation":
                    raise RuntimeError("synthetic-private-error")
                return failing_iterator("synthetic-private-value")
            return super().__iter__()

    value = (
        OnceFailingMapping(safe=True)
        if container_kind == "mapping"
        else OnceFailingSequence(["safe"])
    )
    event = _base_event()
    event["tool_arguments"] = {"failed": value, "reused": value}
    result = sanitize_event(event, additional_redacted_keys=frozenset())
    assert json.loads(result.attributes["tool_arguments"]) == {
        "reused": {"safe": True} if container_kind == "mapping" else ["safe"],
    }
    assert result.attributes["omitted_fields"] == ["tool_arguments.failed"]
    assert "synthetic-private" not in result.serialized
