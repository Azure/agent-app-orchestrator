"""Invocation-owned exact source hydration and bounded optional presentation."""

import asyncio
import copy
import hashlib
import json
import logging
import re
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from typing import Any, NoReturn, TypeVar
from collections.abc import Awaitable, Callable, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2] / "contracts"
if not ROOT.is_dir():
    ROOT = Path(__file__).resolve().parent / "contracts"
MARKER = "agent_lz_presentation"
MAX_BYTES = 262144
T = TypeVar("T")
_attempt: ContextVar[tuple[str, float, Callable[[], float]] | None] = ContextVar("presentation_attempt", default=None)


def diagnostic(event: str, reason: str, *, source_count: int = 0, block_count: int = 0,
               contribution_count: int = 0) -> None:
    """Value-free diagnostics; async tasks/worker threads inherit attempt context."""
    attempt = _attempt.get()
    correlation, elapsed = (attempt[0], max(0.0, attempt[2]() - attempt[1])) if attempt else (uuid4().hex, 0.0)
    logger.info(event, extra={"reason": reason, "correlation_id": correlation, "elapsed_seconds": elapsed,
                            "source_count": source_count, "block_count": block_count,
                            "contribution_count": contribution_count})


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def nonfinite(value: str) -> NoReturn:
    raise ValueError("nonfinite JSON")


def loads(value: str) -> Any:
    if len(value.encode("utf-8")) > MAX_BYTES:
        raise ValueError("size")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=nonfinite)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((ROOT / f"{name}-v1.schema.json").read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )


def settings(value: Any, agent_id: str) -> tuple[dict[str, Any], str] | None:
    if value is None:
        return None
    try:
        raw = value if isinstance(value, str) else compact(value)
        if len(raw.encode("utf-8")) > 10000:
            raise ValueError("configuration bytes")
        document = loads(raw)
        validator("agent-presentation-settings").validate(document)
    except (ValueError, TypeError, ValidationError):
        raise ValueError("AGENT_PRESENTATION_SETTINGS: invalid configuration") from None
    for candidate in document["agents"].values():
        validate_rules(candidate)
    agent = document["agents"].get(agent_id)
    if agent is None or not agent["enabled"]:
        return None
    return copy.deepcopy(agent), hashlib.sha256(compact(document).encode()).hexdigest()


def validate_rules(agent: dict[str, Any]) -> None:
    # Rule admission is not delegated to the selection model.
    ids = [rule["id"] for rule in agent.get("rules", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("AGENT_PRESENTATION_SETTINGS: duplicate rule identity")
    for rule in agent.get("rules", []):
        intervals: list[tuple[Decimal, Decimal, bool, bool]] = []
        for case in rule["cases"]:
            lo, hi, lc, hc = Decimal("-Infinity"), Decimal("Infinity"), False, False
            for test in case["tests"]:
                x, op = Decimal(test["threshold"]), test["operator"]
                if op in ("gt", "gte", "eq"):
                    if x > lo:
                        lo, lc = x, op != "gt"
                    elif x == lo:
                        lc = lc and op != "gt"
                if op in ("lt", "lte", "eq"):
                    if x < hi:
                        hi, hc = x, op != "lt"
                    elif x == hi:
                        hc = hc and op != "lt"
            if lo > hi or (lo == hi and not (lc and hc)):
                raise ValueError("AGENT_PRESENTATION_SETTINGS: empty rule interval")
            for a, b, ac, bc in intervals:
                if max(lo, a) < min(hi, b) or (
                    max(lo, a) == min(hi, b) and
                    (lc if lo >= a else ac) and (hc if hi <= b else bc)
                ):
                    raise ValueError("AGENT_PRESENTATION_SETTINGS: overlapping rules")
            intervals.append((lo, hi, lc, hc))


def normalize(value: Any, tool_name: str, result_id: str) -> dict[str, Any]:
    """Typed structural adapter; caller owns successful tool admission."""
    if isinstance(value, dict) and len(compact(value).encode("utf-8")) > MAX_BYTES:
        raise ValueError("source byte bounds")
    value = loads(value) if isinstance(value, str) else copy.deepcopy(value)
    if (not isinstance(value, dict) or not isinstance(value.get("fields"), list) or
        not value["fields"] or not isinstance(value.get("rows"), list) or not value["rows"]):
        raise ValueError("unsupported result")
    if (value.get("isError") is True or value.get("success") is False or value.get("error") is not None or
        value.get("status") in ("failed", "refused", "cancelled", "unauthorized", "error")):
        raise ValueError("unsuccessful result")
    fields = value["fields"]
    if len(fields) > 100 or len(value["rows"]) > 500:
        raise ValueError("source bounds")
    definitions = validator("turn-presentation").schema["$defs"]
    field_validator = Draft202012Validator({
        "type": "object", "required": ["id", "type"],
        "properties": {"id": definitions["Id"],
                       "type": {"enum": ["string", "integer", "decimal", "boolean", "date", "datetime"]},
                       "unit": definitions["Text"]},
    })
    if not all(field_validator.is_valid(field) for field in fields):
        raise ValueError("source field declarations")
    # Do not spread payload metadata into the model or envelope.
    fields = [{k: f[k] for k in ("id", "type", "unit") if k in f} for f in fields]
    ids = [f["id"] for f in fields]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate fields")
    rows = []
    typed = validator("turn-presentation").schema["$defs"]["TypedValue"]
    scalar_validator = Draft202012Validator({
        **typed, "$defs": validator("turn-presentation").schema["$defs"],
    }, format_checker=FormatChecker())
    for index, row in enumerate(value["rows"]):
        if set(row["data"]) != set(ids):
            raise ValueError("ambiguous fields")
        for field in fields:
            if not scalar_validator.is_valid({"type": field["type"], "value": row["data"][field["id"]]}):
                raise ValueError("unsupported exact scalar")
        rows.append({"sourceRowId": f"row-{index}", "data": row["data"]})
        if row.get("entityId") is not None:
            if not Draft202012Validator(definitions["Id"]).is_valid(row["entityId"]):
                raise ValueError("business identifier")
            rows[-1]["entityId"] = row["entityId"]
    return {"sourceId": uuid4().hex, "toolName": tool_name, "resultId": result_id, "fields": fields, "rows": rows}


def escaped(value: Any) -> str:
    text = "—" if value is None else str(value)
    return re.sub(r"([\\`*_{}\[\]()<>|!])", r"\\\1", text).replace("\n", " ")


def guard_primary(text: str) -> str:
    # A model imitation must never remain a standalone machine carrier.
    return text.replace(MARKER, "agent\\_lz\\_presentation") if MARKER in text else text


def indicator(chosen: dict[str, str] | None, kind: str, source: dict[str, Any],
              row: dict[str, Any], rules: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if not chosen:
        return None
    if "sourceField" in chosen:
        field = chosen["sourceField"]
        declared = next((f for f in source["fields"] if f["id"] == field), None)
        if declared is None or declared["type"] != "string":
            return None
        value = row["data"].get(field)
        if value not in (("ok", "warning", "critical") if kind == "status" else ("up", "down", "flat")):
            return None
        provenance: dict[str, Any] = {"origin": "source"}
    else:
        rule = next((r for r in rules if r["id"] == chosen["ruleId"] and
                     r["toolName"] == source["toolName"] and r["kind"] == kind), None)
        if rule is None:
            return None
        field = rule["field"]
        declared = next((f for f in source["fields"] if f["id"] == field), None)
        raw = row["data"].get(field)
        if declared is None or declared["type"] not in ("integer", "decimal") or raw is None:
            return None
        number = Decimal(raw)
        matching = [case for case in rule["cases"] if all({
            "eq": number == Decimal(test["threshold"]),
            "gt": number > Decimal(test["threshold"]),
            "gte": number >= Decimal(test["threshold"]),
            "lt": number < Decimal(test["threshold"]),
            "lte": number <= Decimal(test["threshold"]),
        }[test["operator"]] for test in case["tests"])]
        if len(matching) != 1:
            return None
        value = matching[0]["value"]
        provenance = {"origin": "rule", "ruleId": rule["id"]}
    provenance["binding"] = {"sourceId": source["sourceId"], "sourceRowId": row["sourceRowId"], "field": field}
    return {"value": value, "provenance": provenance}


def hydrate(selection: Any, sources: Sequence[dict[str, Any]], agent_id: str, revision: str,
            rules: Sequence[dict[str, Any]] = ()) -> dict[str, Any] | None:
    if (not isinstance(selection, dict) or set(selection) != {"schemaVersion", "blocks"} or
        selection["schemaVersion"] != 1 or not isinstance(selection["blocks"], list) or
        not 1 <= len(selection["blocks"]) <= 10):
        raise ValueError("aggregate selection bounds")
    registry = {source["sourceId"]: source for source in sources}
    if len(registry) != len(sources):
        raise ValueError("duplicate source identity")
    blocks: list[dict[str, Any]] = []
    fallback: list[str] = []
    for proposed in selection["blocks"]:
        fallback_start = len(fallback)
        try:
            definition = {"cards": "Cards", "table": "Table", "chart": "Chart", "suggestions": "Suggestions"}[proposed["type"]]
            Draft202012Validator({"$ref": f"#/$defs/{definition}",
                                  "$defs": validator("presentation-selection").schema["$defs"]}).validate(proposed)
            block = {"blockId": uuid4().hex, "type": proposed["type"]}
            if "title" in proposed:
                block["title"] = proposed["title"]
            if proposed["type"] == "suggestions":
                block["items"] = [{"prompt": p} for p in proposed["prompts"]]
                fallback.extend(escaped(p) for p in proposed["prompts"])
            elif proposed["type"] == "cards":
                block["cards"] = []
                for card in proposed["cards"]:
                    source = registry[card["sourceId"]]
                    field = next(f for f in source["fields"] if f["id"] == card["field"])
                    row = next(r for r in source["rows"] if r["sourceRowId"] == card["sourceRowId"])
                    item = {"label": card["label"], "value": {"type": field["type"], "value": row["data"][field["id"]]},
                            "binding": {k: card[k] for k in ("sourceId", "sourceRowId", "field")}}
                    if field.get("unit"):
                        item["unit"] = field["unit"]
                    for kind in ("status", "trend"):
                        resolved = indicator(card.get(kind), kind, source, row, rules)
                        if resolved is not None:
                            item[kind] = resolved
                    block["cards"].append(item)
                    fallback.append(f"{escaped(item['label'])}: {escaped(item['value']['value'])} {escaped(item.get('unit', ''))}")
                    fallback.extend(f"{kind}: {item[kind]['value']}" for kind in ("status", "trend") if kind in item)
            else:
                source = registry[proposed["sourceId"]]
                fields = {f["id"]: f for f in source["fields"]}
                block["sourceId"] = source["sourceId"]
                if proposed["type"] == "table":
                    block["columns"] = [
                        {"id": f"c{i}", "label": c["label"], "type": fields[c["field"]]["type"],
                         "sourceField": c["field"], **({"unit": fields[c["field"]]["unit"]} if fields[c["field"]].get("unit") else {})}
                        for i, c in enumerate(proposed["columns"])
                    ]
                    block["rows"] = [{"id": uuid4().hex, "sourceRowId": r["sourceRowId"],
                                      "values": {c["id"]: r["data"][c["sourceField"]] for c in block["columns"]}}
                                     for r in source["rows"]]
                    for saved, source_row in zip(block["rows"], source["rows"]):
                        status = indicator(proposed.get("status"), "status", source, source_row, rules)
                        if status is not None:
                            saved["status"] = status
                        action = proposed.get("rowAction")
                        if (action and source_row.get("entityId") is not None and
                            fields.get(action["identifierField"], {}).get("type") == "string" and
                            source_row["data"].get(action["identifierField"]) == source_row["entityId"]):
                            saved["entityId"] = source_row["entityId"]
                            saved["followUp"] = {"prompt": action["promptPrefix"] + " " + source_row["entityId"]}
                    if any("followUp" in r for r in block["rows"]):
                        block["rowAction"] = {"label": proposed["rowAction"]["label"]}
                    fallback.append(" | ".join(escaped(c["label"]) + " " + escaped(c.get("unit", "")) for c in block["columns"]))
                    fallback.extend(" | ".join(escaped(r["values"][c["id"]]) for c in block["columns"]) +
                                    (f" | Status: {r['status']['value']}" if "status" in r else "")
                                    for r in block["rows"])
                else:
                    if any(r["data"][proposed["categoryField"]] is None for r in source["rows"]):
                        raise ValueError("missing category")
                    block.update(chartType=proposed["chartType"], categoryField=proposed["categoryField"],
                                 categories=[{"label": str(r["data"][proposed["categoryField"]]), "sourceRowId": r["sourceRowId"]}
                                             for r in source["rows"]])
                    block["series"] = []
                    for series in proposed["series"]:
                        field = fields[series["field"]]
                        if field["type"] not in ("integer", "decimal"):
                            raise ValueError("non-numeric chart")
                        block["series"].append({"label": series["label"], "sourceField": field["id"],
                                                "values": [r["data"][field["id"]] for r in source["rows"]],
                                                **({"unit": field["unit"]} if field.get("unit") else {})})
                    if len({s.get("unit") for s in block["series"]}) > 1:
                        raise ValueError("mixed units")
                    if sum(v is not None for s in block["series"] for v in s["values"]) > 1000:
                        raise ValueError("point bounds")
                    if not any(v is not None for s in block["series"] for v in s["values"]):
                        raise ValueError("no measurements")
                    fallback.extend(f"{escaped(c['label'])}: " + ", ".join(
                        f"{escaped(s['label'])} {escaped(s['values'][i])} {escaped(s.get('unit', ''))}" for s in block["series"])
                                    for i, c in enumerate(block["categories"]))
            definition = {"cards": "Cards", "table": "Table", "chart": "Chart", "suggestions": "Suggestions"}[block["type"]]
            check = Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": validator("turn-presentation").schema["$defs"]},
                                         format_checker=FormatChecker())
            if not check.is_valid(block):
                raise ValueError("known block shape")
            blocks.append(block)
        except (KeyError, StopIteration, ValueError, TypeError, ValidationError):
            del fallback[fallback_start:]
            diagnostic("presentation_unit_rejected", "binding_or_type", source_count=len(sources), block_count=1)
    if not blocks:
        return None
    envelope = {"kind": MARKER, "schemaVersion": 1, "presentationId": uuid4().hex, "settingsRevision": revision,
                "markdown": "\n\n".join(fallback), "contributions": [{
                    "contributionId": uuid4().hex, "agentId": agent_id, "workflowOrdinal": 0,
                    "sourceRefs": [{k: s[k] for k in ("sourceId", "toolName", "resultId")} for s in sources],
                    "blocks": blocks,
                }]}
    validator("turn-presentation").validate(envelope)
    if len(compact(envelope).encode("utf-8")) > MAX_BYTES:
        raise ValueError("aggregate byte bounds")
    return envelope


async def supervise(work: Awaitable[T], *, budget: float = 10,
                    clock: Callable[[], float] | None = None) -> T | None:
    """Seal timeout without awaiting uncooperative cancellation."""
    clock = clock or asyncio.get_running_loop().time
    started = clock()
    deadline = started + budget
    token = _attempt.set((uuid4().hex, started, clock))
    task = asyncio.ensure_future(work)
    try:
        done, _ = await asyncio.wait({task}, timeout=budget)
        if done and clock() < deadline:
            return task.result()
        if task.done() and not task.cancelled():
            task.exception()
        diagnostic("presentation_timed_out", "deadline")
        return None
    finally:
        if not task.done():
            task.cancel()
            def observe(child: asyncio.Future[T]) -> None:
                if not child.cancelled():
                    child.exception()
                    diagnostic("presentation_late_completion", "sealed_attempt")
            task.add_done_callback(observe)
        _attempt.reset(token)


def contribution_markdown(contribution: dict[str, Any]) -> str:
    """Projection from hydrated values only, in declared local block order."""
    lines = [f"Agent: {escaped(contribution['agentId'])}"]
    for block in contribution["blocks"]:
        if block.get("title"):
            lines.append(escaped(block["title"]))
        if block["type"] == "cards":
            for card in block["cards"]:
                line = f"{escaped(card['label'])}: {escaped(card['value']['value'])} {escaped(card.get('unit', ''))}"
                for indicator in ("status", "trend"):
                    if indicator in card:
                        line += f" | {indicator.title()}: {escaped(card[indicator]['value'])}"
                lines.append(line)
        elif block["type"] == "table":
            lines.append(" | ".join(escaped(c["label"]) + " " + escaped(c.get("unit", "")) for c in block["columns"]))
            for row in block["rows"]:
                line = " | ".join(escaped(row["values"][c["id"]]) for c in block["columns"])
                if "status" in row:
                    line += f" | Status: {escaped(row['status']['value'])}"
                lines.append(line)
        elif block["type"] == "chart":
            for i, category in enumerate(block["categories"]):
                lines.append(f"{escaped(category['label'])}: " + ", ".join(
                    f"{escaped(s['label'])} {escaped(s['values'][i])} {escaped(s.get('unit', ''))}" for s in block["series"]))
        elif block["type"] == "suggestions":
            lines.extend(escaped(item["prompt"]) for item in block["items"])
    return "\n\n".join(lines)


def merge_presentations(envelopes: Sequence[dict[str, Any] | None]) -> dict[str, Any] | None:
    """Merge trusted native occurrences; invalid producers cannot erase siblings."""
    accepted, revision = [], None
    check = validator("turn-presentation")
    contribution_check = Draft202012Validator(
        {"$ref": "#/$defs/Contribution", "$defs": check.schema["$defs"]}, format_checker=FormatChecker())
    header_check = Draft202012Validator({
        **check.schema, "required": [k for k in check.schema["required"] if k != "contributions"],
        "properties": {k: v for k, v in check.schema["properties"].items() if k != "contributions"},
    }, format_checker=FormatChecker())

    def bindings(value: Any) -> set[str]:
        if isinstance(value, dict):
            return ({value["sourceId"]} if "sourceId" in value else set()).union(
                *(bindings(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(bindings(child) for child in value))
        return set()

    for envelope in envelopes:
        if envelope is None:
            continue
        if len(compact(envelope).encode("utf-8")) > MAX_BYTES:
            diagnostic("presentation_rejected", "aggregate_bytes")
            return None
        if envelope.get("kind") != MARKER or envelope.get("schemaVersion") != 1:
            continue
        if not header_check.is_valid({k: v for k, v in envelope.items() if k != "contributions"}):
            continue
        for contribution in envelope.get("contributions", []):
            if not contribution_check.is_valid(contribution):
                diagnostic("presentation_contribution_rejected", "shape", contribution_count=1)
                continue
            source_ids = {s["sourceId"] for s in contribution["sourceRefs"]}
            if (len(source_ids) != len(contribution["sourceRefs"]) or
                    any(not bindings(b).issubset(source_ids) for b in contribution["blocks"])):
                diagnostic("presentation_contribution_rejected", "source_scope", contribution_count=1)
                continue
            if revision is None:
                revision = envelope["settingsRevision"]
            if envelope["settingsRevision"] != revision:
                diagnostic("presentation_contribution_rejected", "snapshot_revision", contribution_count=1)
                continue
            accepted.append(copy.deepcopy(contribution))
    if not accepted:
        return None
    accepted.sort(key=lambda c: c["workflowOrdinal"])
    blocks = [b for c in accepted for b in c["blocks"]]
    if (len(blocks) > 10 or len({b["blockId"] for b in blocks}) != len(blocks) or
            len({c["workflowOrdinal"] for c in accepted}) != len(accepted) or
            len({c["contributionId"] for c in accepted}) != len(accepted)):
        diagnostic("presentation_rejected", "aggregate_count_or_identity", contribution_count=len(accepted))
        return None
    result = {"kind": MARKER, "schemaVersion": 1, "presentationId": uuid4().hex,
              "settingsRevision": revision, "contributions": accepted,
              "markdown": "\n\n".join(contribution_markdown(c) for c in accepted)}
    if not check.is_valid(result) or len(compact(result).encode("utf-8")) > MAX_BYTES:
        diagnostic("presentation_rejected", "aggregate_bytes_or_shape", contribution_count=len(accepted))
        return None
    return result


def response_projection(messages: Sequence[Any]) -> tuple[str, dict[str, Any] | None]:
    text, presentations = [], []
    for message in messages:
        raw = message.text
        if message.role == "assistant" and raw.startswith('{"kind":"agent_lz_presentation"'):
            value = loads(raw)
            validator("turn-presentation").validate(value)
            presentations.append(value)
            text.append(value["markdown"])
        else:
            text.append(raw)
    # Preserve single-turn carrier identity; merge only genuinely multiple carriers.
    presentation = presentations[0] if len(presentations) == 1 else merge_presentations(presentations)
    return "\n\n".join(filter(None, text)), presentation
