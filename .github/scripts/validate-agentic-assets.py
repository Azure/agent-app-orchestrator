#!/usr/bin/env python3
"""Validate repository Copilot agents, skills, and scoped instructions."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
FRONTMATTER_BOUNDARY = "---"
LOCAL_LINK = re.compile(r"\[[^\]]+\]\((?!https?://|mailto:|#)([^)]+)\)")
ASSET_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FIELDS = {
    "agent": {
        "agents",
        "argument-hint",
        "description",
        "disable-model-invocation",
        "handoffs",
        "hooks",
        "infer",
        "mcp-servers",
        "metadata",
        "model",
        "name",
        "target",
        "tools",
        "user-invocable",
    },
    "skill": {
        "allowed-tools",
        "compatibility",
        "description",
        "license",
        "metadata",
        "name",
    },
    "instruction": {"applyTo", "excludeAgent"},
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def relative(path: Path) -> Path:
    return path.relative_to(ROOT)


def read_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_BOUNDARY:
        raise ValueError("missing opening YAML frontmatter boundary")

    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == FRONTMATTER_BOUNDARY
        )
    except StopIteration as exc:
        raise ValueError("missing closing YAML frontmatter boundary") from exc

    frontmatter = "\n".join(lines[1:end])
    try:
        metadata = yaml.load(frontmatter, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(metadata, Mapping) or not all(
        isinstance(key, str) for key in metadata
    ):
        raise ValueError("YAML frontmatter must be a string-keyed mapping")

    if not any(line.strip() for line in lines[end + 1 :]):
        raise ValueError("asset body must not be empty")

    return dict(metadata), text


def require_strings(
    path: Path,
    metadata: dict[str, object],
    fields: tuple[str, ...],
    errors: list[str],
) -> None:
    for field in fields:
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{relative(path)}: {field!r} must be a non-empty string")


def validate_optional_string(
    path: Path,
    metadata: dict[str, object],
    field: str,
    errors: list[str],
    *,
    maximum_length: int | None = None,
) -> None:
    if field not in metadata:
        return
    value = metadata[field]
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{relative(path)}: {field!r} must be a non-empty string")
    elif maximum_length is not None and len(value) > maximum_length:
        errors.append(
            f"{relative(path)}: {field!r} exceeds {maximum_length} characters"
        )


def validate_optional_boolean(
    path: Path,
    metadata: dict[str, object],
    field: str,
    errors: list[str],
) -> None:
    if field in metadata and not isinstance(metadata[field], bool):
        errors.append(f"{relative(path)}: {field!r} must be a boolean")


def validate_string_mapping(
    path: Path,
    metadata: dict[str, object],
    field: str,
    errors: list[str],
) -> None:
    if field not in metadata:
        return
    value = metadata[field]
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        errors.append(
            f"{relative(path)}: {field!r} must be a string-to-string mapping"
        )


def validate_fields(
    path: Path,
    metadata: dict[str, object],
    asset_kind: str,
    errors: list[str],
) -> None:
    unknown = sorted(set(metadata) - ALLOWED_FIELDS[asset_kind])
    if unknown:
        errors.append(
            f"{relative(path)}: unsupported {asset_kind} frontmatter fields: {unknown}"
        )


def validate_mcp_servers(
    path: Path,
    value: object,
    errors: list[str],
) -> None:
    if isinstance(value, Mapping):
        for server_name, configuration in value.items():
            if not isinstance(server_name, str) or not server_name.strip():
                errors.append(
                    f"{relative(path)}: 'mcp-servers' names must be non-empty strings"
                )
            if not isinstance(configuration, Mapping):
                errors.append(
                    f"{relative(path)}: MCP server {server_name!r} must be a mapping"
                )
        return

    if isinstance(value, list):
        if not all(isinstance(configuration, Mapping) for configuration in value):
            errors.append(
                f"{relative(path)}: 'mcp-servers' list items must be mappings"
            )
        return

    errors.append(f"{relative(path)}: 'mcp-servers' must be a mapping or list")


def validate_handoffs(
    path: Path,
    value: object,
    errors: list[str],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{relative(path)}: 'handoffs' must be a mapping list")
        return

    allowed_fields = {"agent", "label", "model", "prompt", "send"}
    for index, handoff in enumerate(value):
        if not isinstance(handoff, Mapping):
            errors.append(
                f"{relative(path)}: handoff {index} must be a mapping"
            )
            continue

        unknown = sorted(set(handoff) - allowed_fields)
        if unknown:
            errors.append(
                f"{relative(path)}: handoff {index} has unsupported fields: {unknown}"
            )
        for field in ("agent", "label"):
            item = handoff.get(field)
            if not isinstance(item, str) or not item.strip():
                errors.append(
                    f"{relative(path)}: handoff {index} {field!r} must be "
                    "a non-empty string"
                )
        for field in ("model", "prompt"):
            item = handoff.get(field)
            if item is not None and (
                not isinstance(item, str) or not item.strip()
            ):
                errors.append(
                    f"{relative(path)}: handoff {index} {field!r} must be "
                    "a non-empty string"
                )
        if "send" in handoff and not isinstance(handoff["send"], bool):
            errors.append(
                f"{relative(path)}: handoff {index} 'send' must be a boolean"
            )


def validate_hooks(
    path: Path,
    value: object,
    errors: list[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{relative(path)}: 'hooks' must be a mapping")
        return

    for event_name, hooks in value.items():
        if not isinstance(event_name, str) or not event_name.strip():
            errors.append(
                f"{relative(path)}: hook event names must be non-empty strings"
            )
        if not isinstance(hooks, list) or not all(
            isinstance(hook, Mapping) for hook in hooks
        ):
            errors.append(
                f"{relative(path)}: hook event {event_name!r} must contain "
                "a mapping list"
            )


def validate_agent(
    path: Path,
    metadata: dict[str, object],
    identifiers: set[str],
    errors: list[str],
) -> None:
    require_strings(path, metadata, ("description",), errors)
    validate_optional_string(path, metadata, "name", errors)
    validate_optional_string(path, metadata, "argument-hint", errors)
    for field in (
        "disable-model-invocation",
        "infer",
        "user-invocable",
    ):
        validate_optional_boolean(path, metadata, field, errors)

    identifier = (
        path.name.removesuffix(".agent.md")
        if path.name.endswith(".agent.md")
        else path.stem
    )
    if (
        path.suffix != ".md"
        or not ASSET_NAME.fullmatch(identifier)
    ):
        errors.append(
            f"{relative(path)}: agent filename must be lowercase kebab-case "
            "with a .md or .agent.md suffix"
        )
    elif identifier in identifiers:
        errors.append(f"{relative(path)}: duplicate agent identifier {identifier!r}")
    identifiers.add(identifier)

    target = metadata.get("target")
    if target is not None and target not in {"github-copilot", "vscode"}:
        errors.append(
            f"{relative(path)}: 'target' must be 'github-copilot' or 'vscode'"
        )

    tools = metadata.get("tools")
    if tools is not None:
        if isinstance(tools, str):
            if not tools.strip():
                errors.append(f"{relative(path)}: 'tools' string must not be empty")
        elif not isinstance(tools, list) or not all(
            isinstance(tool, str) and tool for tool in tools
        ):
            errors.append(
                f"{relative(path)}: 'tools' must be a string or string list"
            )
        elif len(tools) != len(set(tools)):
            errors.append(f"{relative(path)}: duplicate tool names")

    model = metadata.get("model")
    if isinstance(model, str) and not model.strip():
        errors.append(f"{relative(path)}: 'model' string must not be empty")
    elif isinstance(model, list) and (
        not model or not all(isinstance(item, str) and item for item in model)
    ):
        errors.append(
            f"{relative(path)}: 'model' list must contain non-empty strings"
        )
    elif model is not None and not isinstance(model, (str, list)):
        errors.append(f"{relative(path)}: 'model' must be a string or string list")

    agents = metadata.get("agents")
    if agents is not None and (
        not isinstance(agents, list)
        or not all(isinstance(agent, str) and agent for agent in agents)
    ):
        errors.append(f"{relative(path)}: 'agents' must be a string list")

    validate_string_mapping(path, metadata, "metadata", errors)

    mcp_servers = metadata.get("mcp-servers")
    if mcp_servers is not None:
        validate_mcp_servers(path, mcp_servers, errors)

    handoffs = metadata.get("handoffs")
    if handoffs is not None:
        validate_handoffs(path, handoffs, errors)

    hooks = metadata.get("hooks")
    if hooks is not None:
        validate_hooks(path, hooks, errors)


def validate_skill(
    path: Path,
    metadata: dict[str, object],
    names: set[str],
    errors: list[str],
) -> None:
    require_strings(path, metadata, ("name", "description"), errors)
    name = metadata.get("name")
    if not isinstance(name, str):
        return
    if not ASSET_NAME.fullmatch(name):
        errors.append(f"{relative(path)}: skill name must be lowercase kebab-case")
    if len(name) > 64:
        errors.append(f"{relative(path)}: skill name exceeds 64 characters")
    if name != path.parent.name:
        errors.append(f"{relative(path)}: skill name must match directory")
    if name in names:
        errors.append(f"{relative(path)}: duplicate skill {name!r}")
    names.add(name)

    description = metadata.get("description")
    if isinstance(description, str) and len(description) > 1024:
        errors.append(f"{relative(path)}: skill description exceeds 1024 characters")

    validate_optional_string(path, metadata, "license", errors)
    validate_optional_string(
        path,
        metadata,
        "compatibility",
        errors,
        maximum_length=500,
    )
    validate_optional_string(path, metadata, "allowed-tools", errors)
    validate_string_mapping(path, metadata, "metadata", errors)


def validate_instruction(
    path: Path,
    metadata: dict[str, object],
    errors: list[str],
) -> None:
    require_strings(path, metadata, ("applyTo",), errors)
    instruction_name = path.name.removesuffix(".instructions.md")
    if (
        path.name == instruction_name
        or not ASSET_NAME.fullmatch(instruction_name)
    ):
        errors.append(
            f"{relative(path)}: instruction filename must be lowercase kebab-case"
        )

    apply_to = metadata.get("applyTo")
    if not isinstance(apply_to, str):
        return

    patterns = apply_to.split(",")
    for pattern in patterns:
        normalized = pattern.strip()
        if not normalized:
            errors.append(f"{relative(path)}: 'applyTo' has an empty pattern")
            continue
        if "\\" in normalized or normalized.startswith(("/", "~")):
            errors.append(
                f"{relative(path)}: applyTo patterns must be relative POSIX globs: "
                f"{normalized!r}"
            )

    exclude_agent = metadata.get("excludeAgent")
    if exclude_agent is not None and exclude_agent not in {
        "cloud-agent",
        "code-review",
    }:
        errors.append(
            f"{relative(path)}: 'excludeAgent' must be 'cloud-agent' or "
            "'code-review'"
        )


def validate_local_links(path: Path, text: str, errors: list[str]) -> None:
    for target in LOCAL_LINK.findall(text):
        target_path = target.split("#", 1)[0]
        if not target_path:
            continue
        resolved = (path.parent / target_path).resolve()
        if not resolved.is_relative_to(ROOT.resolve()) or not resolved.exists():
            errors.append(
                f"{relative(path)}: local link does not exist: {target}"
            )


def validate_required_guidance(errors: list[str]) -> None:
    for path in (ROOT / "AGENTS.md", ROOT / ".github" / "copilot-instructions.md"):
        try:
            if not path.read_text(encoding="utf-8").strip():
                errors.append(f"{relative(path)}: guidance file must not be empty")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative(path)}: cannot read guidance file: {exc}")


def main() -> int:
    errors: list[str] = []
    agent_identifiers: set[str] = set()
    skill_names: set[str] = set()

    validate_required_guidance(errors)

    groups = (
        (ROOT / ".github" / "skills", "*/SKILL.md", "skill"),
        (ROOT / ".github" / "instructions", "*.instructions.md", "instruction"),
    )

    for directory, pattern, asset_kind in groups:
        paths = sorted(directory.rglob(pattern))
        if not paths:
            errors.append(f"{relative(directory)}: no matching assets")
            continue

        for path in paths:
            try:
                metadata, text = read_frontmatter(path)
            except (OSError, UnicodeError, ValueError) as exc:
                errors.append(f"{relative(path)}: {exc}")
                continue

            validate_fields(path, metadata, asset_kind, errors)
            validate_local_links(path, text, errors)

            if asset_kind == "agent":
                validate_agent(path, metadata, agent_identifiers, errors)
            elif asset_kind == "skill":
                validate_skill(path, metadata, skill_names, errors)
            else:
                validate_instruction(path, metadata, errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    instruction_count = len(
        list((ROOT / ".github" / "instructions").rglob("*.instructions.md"))
    )
    print(
        f"Validated {len(agent_identifiers)} agents, {len(skill_names)} skills, "
        f"and {instruction_count} scoped instructions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
