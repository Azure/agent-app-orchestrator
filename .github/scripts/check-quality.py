"""Source-only quality checks. Policy authority comes from the PR base, not JSON approvals."""

from __future__ import annotations

import argparse
import ast
from collections import Counter, deque
import configparser
import graphlib
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import sysconfig
import tempfile
import time
import tokenize
import tomllib
import xml.etree.ElementTree as ET


CHECKS = ("lint", "typing", "architecture", "exceptions", "policy")
REPOSITORY = "Azure/gpt-rag-orchestrator"
REQUIRED_JOBS = (*CHECKS, "tests", "frontend")
RECORDS = ("policy.json", "module-surfaces.json", "typing-scope.json",
           "typing-baseline.json", "exceptions.json")
PROTECTED = (".quality/", ".github/scripts/", ".github/workflows/",
             ".github/CODEOWNERS", "requirements-quality.txt", ".importlinter")


class QualityError(Exception):
    """Incomplete analysis or invalid inputs, never a passing check."""


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def syntax(node):
    return digest(ast.dump(node, include_attributes=False))


def finding(rule, path="", line=1, message="", **details):
    return {"rule": rule, "path": path, "line": line, "message": message, **details}


def run(command, *, cwd=None, env=None, timeout=300):
    try:
        return subprocess.run(command, cwd=cwd, env=env, timeout=timeout,
                              capture_output=True, text=True, encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        raise QualityError(f"{command[0]} timed out") from exc
    except OSError as exc:
        raise QualityError(f"Could not execute {command[0]}") from exc


def git(root, *args):
    result = run(["git", "--no-pager", *args], cwd=root)
    if result.returncode:
        raise QualityError(f"git {args[0]} failed")
    return result.stdout


def static_environment(workspace):
    # Tools inherit OS process necessities, not interpreter/search/cache configuration.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(HOME=str(workspace), USERPROFILE=str(workspace),
               TMP=str(workspace), TEMP=str(workspace), TMPDIR=str(workspace),
               XDG_CONFIG_HOME=str(workspace), XDG_CACHE_HOME=str(workspace),
               PYTHONIOENCODING="utf-8", NO_COLOR="1")
    return env


def static_tool(tool, workspace, arguments=()):
    return run([sys.executable, "-I", "-S", str(Path(__file__).resolve()),
                "--static-tool", tool, *arguments], cwd=workspace,
               env=static_environment(workspace))


def tool_interpreter():
    """Expose installed wheels as paths, without site initialization or .pth execution."""
    if not sys.flags.isolated or not sys.flags.no_site:
        raise QualityError("Static analysis must start with python -I -S")
    executable = Path(sys.executable)
    prefix = executable.parent
    if prefix.name.lower() in ("scripts", "bin"):
        prefix = prefix.parent
    variables = {"base": str(prefix), "platbase": str(prefix)}
    paths = {sysconfig.get_path(key, vars=variables) for key in ("purelib", "platlib")}
    if not all(Path(path).is_dir() for path in paths):
        raise QualityError("Missing installed tool site-packages")
    # Python 3.12 -S does not set virtualenv prefixes; mypy's PEP 561 discovery needs them.
    sys.prefix = sys.exec_prefix = str(prefix)
    sys.path.extend(sorted(paths))


def static_tool_main(tool, arguments):
    tool_interpreter()
    source = Path.cwd() / "src"

    def refuse_source_execution(event, args):
        if event == "exec" and Path(args[0].co_filename).resolve().is_relative_to(source):
            raise QualityError("Static tools must not execute candidate source")

    sys.addaudithook(refuse_source_execution)
    if tool == "versions":
        print(json.dumps({name: importlib.metadata.version(name)
                          for name in ("ruff", "mypy", "import-linter", "grimp")}))
    elif tool in ("ruff", "mypy"):
        sys.argv = [tool, *arguments]
        runpy.run_module(tool, run_name="__main__")
    elif tool == "grimp":
        import grimp

        sys.path.append(str(source))
        graph = grimp.build_graph(*arguments, include_external_packages=False,
                                  exclude_type_checking_imports=False, cache_dir=None)
        print(json.dumps({name: sorted(graph.find_modules_directly_imported_by(name))
                          for name in sorted(graph.modules)}))
    elif tool == "import-linter":
        from importlinter.cli import lint_imports_command

        sys.path.append(str(source))
        lint_imports_command(list(arguments))
    else:
        raise QualityError("Unknown static tool")


def static_configuration(config, import_config):
    mypy = config.get("tool", {}).get("mypy", {})
    unsafe = {"plugins", "python_executable", "custom_typeshed_dir"}
    if any(key in mypy for key in unsafe) or mypy.get("mypy_path", "src") != "src":
        raise QualityError("Executable or external mypy configuration is not permitted")
    ruff = config.get("tool", {}).get("ruff", {})
    if "extend" in ruff:
        raise QualityError("External Ruff configuration is not permitted")
    parser = configparser.ConfigParser()
    parser.read_string(import_config)
    for section in parser.values():
        if "contract_types" in section:
            raise QualityError("Executable Import Linter configuration is not permitted")
    roots = parser.get("importlinter", "root_packages", fallback="").split()
    roots += parser.get("importlinter", "root_package", fallback="").split()
    if any(not name.isidentifier() for name in roots):
        raise QualityError("Import Linter configuration requires top-level package roots")


def write_analysis_sources(workspace, modules):
    for module in modules.values():
        dest = workspace / module["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(module["text"], encoding="utf-8")


def validate_records(record):
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1):
        raise QualityError("Unsupported quality record schema")
    entries = record.get("entries", [])
    if not isinstance(entries, list):
        raise QualityError("Record entries must be an array")
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if (len(ids) != len(entries) or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids)):
        raise QualityError("Record identities must be present and unique")
    return record


def exact_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise QualityError(f"Unexpected or missing fields in {label}")


def strings(value, label, *, empty=False):
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)
            or len(value) != len(set(value)) or (not value and not empty)):
        raise QualityError(f"{label} must contain unique nonempty strings")


def evidence_selectors(value):
    strings(value, "evidence_tests")
    if any(not re.fullmatch(r"tests/[\w/]+\.py::[\w:\[\].-]+", v) for v in value):
        raise QualityError("Evidence must name exact pytest test selectors")


def text_fields(value, keys):
    if any(not isinstance(value.get(key), str) or not value[key] for key in keys):
        raise QualityError(f"Expected nonempty text fields: {', '.join(keys)}")


def validate_surfaces(entries):
    if not isinstance(entries, list) or not entries:
        raise QualityError("Module surfaces must classify runtime modules")
    fields = ("id", "path", "import_name", "area", "public_exports", "private_modules",
              "allowed_importers", "legacy_aliases", "typing_status", "responsibilities",
              "source_revision")
    areas = {"api", "connectors", "orchestration", "plugins", "strategies",
             "telemetry", "util", "composition", "contracts"}
    for entry in entries:
        exact_keys(entry, fields, "module surface")
        text_fields(entry, ("id", "path", "import_name", "area", "typing_status",
                            "responsibilities", "source_revision"))
        for key in ("id", "import_name"):
            if not all(part.isidentifier() for part in entry[key].split(".")):
                raise QualityError("Module surface requires explicit Python identities")
        if (not entry["path"].startswith("src/") or not entry["path"].endswith(".py")
                or "\\" in entry["path"]
                or any(part in ("", ".", "..") for part in entry["path"].split("/"))):
            raise QualityError("Module surface path must be a canonical runtime source path")
        source_name = entry["path"][4:-3].removesuffix("/__init__").replace("/", ".")
        if source_name != entry["import_name"]:
            raise QualityError("Module surface path and import name disagree")
        if entry["area"] not in areas or entry["typing_status"] not in ("inventoried", "blocking"):
            raise QualityError("Unknown module surface area or typing status")
        for key in ("public_exports", "private_modules", "allowed_importers", "legacy_aliases"):
            strings(entry[key], key, empty=True)
            if any(not all(part.isidentifier() for part in name.split("."))
                   for name in entry[key]):
                raise QualityError("Module surfaces cannot grant wildcard or indirect access")
        if not re.fullmatch("[0-9a-f]{40}", entry["source_revision"]):
            raise QualityError("Module surfaces require immutable source provenance")
    for key in ("id", "path", "import_name"):
        if len({entry[key] for entry in entries}) != len(entries):
            raise QualityError(f"Duplicate module surface {key}")
    for key in ("private_modules", "legacy_aliases"):
        claims = [value for entry in entries for value in entry[key]]
        if len(claims) != len(set(claims)):
            raise QualityError(f"Duplicate module surface ownership: {key}")


def load_records(root):
    result = {}
    for name in RECORDS:
        try:
            result[name] = validate_records(json.loads((root / ".quality" / name).read_text()))
        except (OSError, ValueError) as exc:
            raise QualityError(f"Invalid or missing .quality/{name}") from exc
    policy = result["policy.json"]
    surfaces = result["module-surfaces.json"]
    exact_keys(surfaces, ("schema_version", "entries"), "module surfaces")
    validate_surfaces(surfaces["entries"])
    exact_keys(policy, ("schema_version", "source_revision", "runtime_roots", "modules",
                       "contracts", "toolchain", "required_checks", "review"), "policy")
    text_fields(policy, ("source_revision",))
    strings(policy["modules"], "modules")
    if not re.fullmatch("[0-9a-f]{40}", policy["source_revision"]):
        raise QualityError("Policy must name its immutable source revision")
    exact_keys(policy["toolchain"], ("ruff", "mypy", "import-linter", "grimp"), "toolchain")
    if any(not isinstance(pin, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", pin)
           for pin in policy["toolchain"].values()):
        raise QualityError("Toolchain versions must be exact numeric pins")
    requirements = {
        line.strip() for line in (root / "requirements-quality.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if requirements != {f"{name}=={pin}" for name, pin in policy["toolchain"].items()}:
        raise QualityError("Development requirements and policy pins disagree")
    exact_keys(policy["review"], ("status", "reference", "rationale"), "policy review")
    if policy["review"]["status"] not in ("proposed", "active") or any(
            not isinstance(v, str) or not v for v in policy["review"].values()):
        raise QualityError("Policy review metadata is incomplete")
    contracts = policy["contracts"]
    exact_keys(contracts, ("forbidden", "public_surfaces", "private_ownership",
                          "private_access", "dynamic_imports"), "contracts")
    strings(contracts["public_surfaces"], "public surfaces")
    text_fields(contracts, ("private_ownership",))
    for key in ("forbidden", "private_access", "dynamic_imports"):
        if not isinstance(contracts[key], list):
            raise QualityError(f"{key} must be an array")
    for rule in contracts["forbidden"]:
        exact_keys(rule, ("from", "to"), "forbidden rule")
        strings(rule["from"], "forbidden sources")
        strings(rule["to"], "forbidden targets")
    for rule in contracts["private_access"]:
        exact_keys(rule, ("importer", "target"), "private access rule")
        if any(not isinstance(v, str) or not v or "*" in v for v in rule.values()):
            raise QualityError("Private access permissions must be exact")
    for rule in contracts["dynamic_imports"]:
        exact_keys(rule, ("id", "module_id", "symbol", "source_fingerprint", "targets",
                          "evidence_tests", "review"), "dynamic import")
        strings(rule["targets"], "dynamic targets")
        evidence_selectors(rule["evidence_tests"])
        text_fields(rule, ("id", "module_id", "symbol", "source_fingerprint", "review"))
        if not re.fullmatch("[0-9a-f]{64}", rule["source_fingerprint"]):
            raise QualityError("Dynamic import must bind exact source")
    dynamic_ids = [rule["id"] for rule in contracts["dynamic_imports"]]
    if len(dynamic_ids) != len(set(dynamic_ids)):
        raise QualityError("Dynamic import identities must be unique")
    if policy["runtime_roots"] != ["src"] or policy["required_checks"] != list(REQUIRED_JOBS):
        raise QualityError("Source roots and required checks cannot omit runtime or test coverage")
    scope_record = result["typing-scope.json"]
    exact_keys(scope_record, ("schema_version", "module_ids", "coverage_stage",
                              "planned_expansion", "move_map", "review"), "typing scope")
    scope = scope_record["module_ids"]
    strings(scope, "typing scope")
    strings(scope_record["planned_expansion"], "typing expansion")
    text_fields(scope_record, ("coverage_stage", "review"))
    if scope_record["move_map"] != []:
        raise QualityError("Only mechanically proven one-to-one source moves are supported")
    for name in ("typing-baseline.json", "exceptions.json"):
        exact_keys(result[name], ("schema_version", "entries"), name)
    for entry in result["typing-baseline.json"]["entries"]:
        required = ("module_id", "symbol", "source_fingerprint", "rule",
                    "message_fingerprint", "occurrences", "introduced_at",
                    "rationale", "review", "removal_stage")
        if any(not entry.get(key) for key in required) or type(entry["occurrences"]) is not int:
            raise QualityError("Incomplete typing baseline record")
        if set(entry) - {"id", *required, "path", "line", "message"}:
            raise QualityError("Unknown typing baseline field")
        text_fields(entry, [key for key in required if key != "occurrences"])
        if not all(re.fullmatch("[0-9a-f]{64}", entry[key]) for key in
                   ("source_fingerprint", "message_fingerprint")):
            raise QualityError("Baseline fingerprints must bind exact source and message")
        if not re.fullmatch("[0-9a-f]{40}", entry["introduced_at"]):
            raise QualityError("Baseline must name an immutable introduction revision")
        if entry["module_id"] not in scope or entry["occurrences"] < 1:
            raise QualityError("Baseline debt must belong to blocking scope")
    for entry in result["exceptions.json"]["entries"]:
        required = ("module_id", "symbol", "handler_fingerprint", "caught_types",
                    "boundary", "reason", "failure_outcome", "diagnostic_path",
                    "evidence_tests", "status", "review", "review_by_stage")
        if any(not entry.get(key) for key in required):
            raise QualityError("Incomplete exception record")
        if entry["status"] not in ("active", "proposed"):
            raise QualityError("Invalid exception status")
        if set(entry) - {"id", *required, "path", "line"}:
            raise QualityError("Unknown exception record field")
        text_fields(entry, [key for key in required if key not in ("caught_types", "evidence_tests")])
        if entry["failure_outcome"] not in (
            "propagation", "failure-translation", "cleanup-then-propagation",
            "contractual-best-effort-side-effect",
        ):
            raise QualityError("Unknown exception failure outcome")
        evidence_selectors(entry["evidence_tests"])
        strings(entry["caught_types"], "caught types")
        if not re.fullmatch("[0-9a-f]{64}", entry["handler_fingerprint"]):
            raise QualityError("Handler fingerprint must bind exact source")
    return result


def collect(root, roots):
    modules = {}
    for directory in roots:
        source = root / directory
        if not source.is_dir() or source.is_symlink():
            raise QualityError(f"Missing/unsafe runtime root: {directory}")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise QualityError("Symlinked source is not supported")
        for path in sorted(source.rglob("*.py")):
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
                raise QualityError("Symlinked source is not supported")
            relative = path.relative_to(source)
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__" and len(parts) > 1:
                parts.pop()
            name = ".".join(parts)
            if name in modules:
                raise QualityError(f"Ambiguous module: {name}")
            text = path.read_text(encoding="utf-8-sig")
            try:
                parsed = ast.parse(text, filename=str(relative))
            except SyntaxError as exc:
                raise QualityError(f"Invalid Python: {relative}:{exc.lineno}") from exc
            modules[name] = {
                "name": name, "path": path.relative_to(root).as_posix(),
                "package": path.name == "__init__.py", "tree": parsed, "text": text,
                "fingerprint": syntax(parsed),
            }
    if not modules:
        raise QualityError("Runtime collection was empty")
    return modules


def module_identities(before, after, surfaces=()):
    stable = {entry["import_name"]: entry["id"] for entry in surfaces}
    identities = {name: stable.get(name, name) for name in after if name in before}
    findings = []
    removed = set(before) - set(after)
    added = set(after) - set(before)
    for old in sorted(removed):
        matches = [new for new in added
                   if before[old]["fingerprint"] == after[new]["fingerprint"]]
        reverse = [other for other in removed
                   if before[other]["fingerprint"] == before[old]["fingerprint"]]
        if len(matches) == 1 and len(reverse) == 1:
            identities[matches[0]] = stable.get(old, old)
        else:
            findings.append(finding("module-identity", before[old]["path"],
                                    message="Deletion, split or ambiguous move needs policy review"))
    for name in after:
        identities.setdefault(name, name)
    if len(set(identities.values())) != len(identities):
        findings.append(finding("module-identity",
                                message="Multiple modules cannot consume one stable identity"))
    return identities, findings


def surface_findings(surfaces, modules, identities, scope):
    found = []
    declared = {entry["id"]: entry for entry in surfaces}
    for name, module in modules.items():
        entry = declared.get(identities[name])
        if entry is None:
            found.append(finding("surface-missing", module["path"],
                                 message="Classify new runtime module responsibilities and boundary"))
            continue
        if entry["path"] != module["path"] or entry["import_name"] != name:
            found.append(finding("surface-move", module["path"],
                                 message="Update the one-to-one surface record without changing its id"))
        if not set(entry["public_exports"]) <= exported_names(module):
            found.append(finding("surface-export", module["path"],
                                 message="Declared public export no longer exists"))
        if (entry["typing_status"] == "blocking") != (entry["id"] in scope):
            found.append(finding("surface-typing", module["path"],
                                 message="Surface typing status disagrees with permanent blocking scope"))
        for private in entry["private_modules"]:
            if (private not in modules or not within(private, name)
                    or not any(p.startswith("_") for p in private.split("."))):
                found.append(finding("surface-private", module["path"],
                                     message="Declared private module does not exist"))
        for importer in entry["allowed_importers"]:
            if importer not in modules:
                found.append(finding("surface-importer", module["path"],
                                     message="Declared importer no longer exists"))
        for alias in entry["legacy_aliases"]:
            alias_module, _, member = alias.rpartition(".")
            if alias_module not in modules or member not in exported_names(modules[alias_module]):
                found.append(finding("surface-alias", module["path"],
                                     message="Declared compatibility entrypoint does not exist"))
    for identity in declared.keys() - set(identities.values()):
        found.append(finding("surface-retired", declared[identity]["path"],
                             message="Resolve deleted or split module ownership explicitly"))
    return found


def surface_provenance(root, surfaces):
    findings = []
    for entry in surfaces:
        text = git(root, "show", f"{entry['source_revision']}:{entry['path']}")
        try:
            tree = ast.parse(text, filename=entry["path"])
        except SyntaxError as exc:
            raise QualityError("Invalid Python in surface source provenance") from exc
        if not set(entry["public_exports"]) <= exported_names({"tree": tree}):
            findings.append(finding("surface-provenance", entry["path"],
                                    message="Public surface is absent from declared source revision"))
    return findings


def dotted(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value, aliases)}.{node.attr}"
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
        return f"{dotted(node.args[0], aliases)}.{node.args[1].value}"
    return "<indirect>"


def import_base(module, node):
    if not node.level:
        return node.module or ""
    package = module["name"].split(".")
    if not module["package"]:
        package.pop()
    if node.level > len(package):
        raise QualityError(f"Invalid relative import: {module['path']}:{node.lineno}")
    prefix = package[:len(package) - node.level + 1]
    return ".".join([*prefix, *([node.module] if node.module else [])])


def aliases_for(module):
    aliases = {}
    def bind(name, value):
        if ((name in aliases and aliases[name] != value)
                or (name in ("Exception", "BaseException", "__import__", "getattr")
                    and value not in (name, f"builtins.{name}"))):
            raise QualityError(
                f"Ambiguous import alias {name} in {module['path']}; use distinct aliases")
        aliases[name] = value

    for node in ast.walk(module["tree"]):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bind(alias.asname or alias.name.split(".")[0],
                     alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            base = import_base(module, node)
            for alias in node.names:
                bind(alias.asname or alias.name, f"{base}.{alias.name}")
    # Resolve straightforward aliases conservatively, including chained broad types.
    assignments = [n for n in ast.walk(module["tree"]) if isinstance(n, ast.Assign)]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = dotted(node.value, aliases)
            if value in ("Exception", "BaseException", "builtins.Exception",
                         "builtins.BaseException", "importlib.import_module",
                         "builtins.__import__", "__import__"):
                for target in node.targets:
                    if isinstance(target, ast.Name) and aliases.get(target.id) != value:
                        bind(target.id, value)
                        changed = True
        if not changed:
            break
    for node in assignments:
        if not isinstance(node.value, (ast.Constant, ast.Name, ast.Attribute)):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = "<indirect>"
    return aliases


def within(name, area):
    return name == area or name.startswith(area + ".")


def exported_names(module):
    class Exports(ast.NodeVisitor):
        def __init__(self):
            self.names = set()

        def visit_FunctionDef(self, node):
            self.names.add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Import(self, node):
            self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

        visit_ImportFrom = visit_Import

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                self.names.add(node.id)

        def visit_Lambda(self, node):
            return

        visit_ListComp = visit_Lambda
        visit_SetComp = visit_Lambda
        visit_DictComp = visit_Lambda
        visit_GeneratorExp = visit_Lambda

    visitor = Exports()
    visitor.visit(module["tree"])
    return visitor.names


def architecture(modules, contracts, passed_tests=frozenset(), surfaces=()):
    edges = {name: set() for name in modules}
    locations = {}
    findings = []
    first_party = {name.split(".")[0] for name in modules}
    namespaces = {".".join(name.split(".")[:i]) for name in modules
                  for i in range(1, len(name.split(".")))}
    known = set(modules) | namespaces
    exports = {name: exported_names(module) for name, module in modules.items()}
    declared = {entry["import_name"]: entry for entry in surfaces}
    used_dynamic = set()
    for name, module in modules.items():
        aliases = aliases_for(module)

        def add(target, node, member=""):
            if target.split(".")[0] not in first_party:
                return
            if target not in known:
                findings.append(finding("unresolved-import", module["path"], node.lineno,
                                        f"Cannot resolve first-party module {target}"))
                return
            if target in modules:
                edges[name].add(target)
                locations[name, target] = node.lineno
            private = any(p.startswith("_") and p != "__init__" for p in target.split("."))
            private_member = any(p.startswith("_") and not p.startswith("__")
                                 for p in member.split("."))
            private = private or private_member
            owner = (target if private_member and (
                         target in namespaces or modules.get(target, {}).get("package"))
                     else target.rpartition(".")[0] if "." in target else target)
            same_owner = name == owner or name.startswith(owner + ".")
            approved = any(
                record["importer"] == name and record["target"] in (
                    f"{target}.{member}", *([target] if not private_member else []))
                for record in contracts["private_access"]
            )
            if private and not same_owner and not approved:
                findings.append(finding("private-access", module["path"], node.lineno,
                                        f"{name} cannot access {target}{'.' + member if member else ''}"))
            if target in declared and target != name:
                surface = declared[target]
                if name not in surface["allowed_importers"]:
                    findings.append(finding("surface-consumer", module["path"], node.lineno,
                                            f"Undeclared consumer of {target}: {name}"))
                if (member and not private_member and not same_owner
                        and member.split(".")[0] not in surface["public_exports"]):
                    findings.append(finding("surface-access", module["path"], node.lineno,
                                            f"Undeclared public export {target}.{member.split('.')[0]}"))

        for node in ast.walk(module["tree"]):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name, node)
            elif isinstance(node, ast.ImportFrom):
                base = import_base(module, node)
                for alias in node.names:
                    child = f"{base}.{alias.name}"
                    if alias.name == "*":
                        findings.append(finding("star-import", module["path"], node.lineno,
                                                "Use explicit first-party exports")
                                        ) if base.split(".")[0] in first_party else None
                    if (child not in known and base in modules and alias.name != "*"
                            and alias.name not in exports[base]):
                        findings.append(finding("unresolved-import", module["path"], node.lineno,
                                                f"Cannot resolve first-party export {base}.{alias.name}"))
                    add(child if child in known else base, node,
                        "" if child in known else alias.name)
            elif isinstance(node, ast.Attribute):
                resolved = dotted(node, aliases)
                target = resolved if resolved in known else next(
                    (candidate for candidate in sorted(known, key=len, reverse=True)
                     if resolved.startswith(candidate + ".")), None)
                if target:
                    member = resolved[len(target) + 1:] if resolved != target else ""
                    add(target, node, member)
            elif isinstance(node, ast.Call):
                call = dotted(node.func, aliases)
                resolved = dotted(node, aliases)
                target = resolved if resolved in known else next(
                    (candidate for candidate in sorted(known, key=len, reverse=True)
                     if resolved.startswith(candidate + ".")), None)
                if target:
                    add(target, node, resolved[len(target) + 1:])
                if (call == "getattr" and node.args
                        and dotted(node.args[0], aliases) in ("importlib", "builtins")
                        and (len(node.args) < 2 or not isinstance(node.args[1], ast.Constant))):
                    findings.append(finding("dynamic-import", module["path"], node.lineno,
                                            "Computed access to import machinery requires explicit source review"))
                if call in ("importlib.import_module", "__import__", "builtins.__import__"):
                    target = node.args[0] if node.args else next(
                        (v.value for v in node.keywords if v.arg == "name"), None)
                    if isinstance(target, ast.Constant) and isinstance(target.value, str):
                        if target.value.startswith("."):
                            package = node.args[1] if len(node.args) > 1 else next(
                                (v.value for v in node.keywords if v.arg == "package"), None)
                            if isinstance(package, ast.Constant) and isinstance(package.value, str):
                                level = len(target.value) - len(target.value.lstrip("."))
                                relative = ast.ImportFrom(
                                    module=target.value.lstrip("."), names=[], level=level)
                                relative.lineno = node.lineno
                                add(import_base({"name": package.value, "package": True,
                                                 "path": module["path"]}, relative), node)
                            else:
                                findings.append(finding("dynamic-import", module["path"], node.lineno,
                                                        "Relative dynamic loading needs a literal package"))
                        else:
                            add(target.value, node)
                    else:
                        record = next((r for r in contracts["dynamic_imports"]
                                       if r["module_id"] == name and
                                       r["symbol"] == symbol_at(module, node.lineno) and
                                       r["source_fingerprint"] == syntax(node)), None)
                        if (record is None or record["id"] in used_dynamic
                                or not set(record["evidence_tests"]) <= passed_tests):
                            findings.append(finding("dynamic-import", module["path"], node.lineno,
                                                    "Variable dynamic loading needs policy review"))
                        else:
                            used_dynamic.add(record["id"])
                            for permitted in record["targets"]:
                                add(permitted, node)
    for record in contracts["dynamic_imports"]:
        if record["id"] not in used_dynamic:
            findings.append(finding("dynamic-import", message=f"Unused or unproven dynamic import: {record['id']}"))
    try:
        tuple(graphlib.TopologicalSorter(edges).static_order())
    except graphlib.CycleError as exc:
        # TopologicalSorter returns dependency order; reverse to show import direction.
        cycle = list(reversed(exc.args[1]))
        findings.append(finding("cycle", modules[cycle[0]]["path"],
                                locations.get((cycle[0], cycle[1]), 1),
                                " -> ".join(cycle), dependency_path=cycle))
    for contract in contracts["forbidden"]:
        for source in edges:
            if not any(within(source, area) for area in contract["from"]):
                continue
            queue = deque([[source]])
            seen = {source}
            while queue:
                path = queue.popleft()
                for target in sorted(edges[path[-1]]):
                    chain = [*path, target]
                    if any(within(target, area) for area in contract["to"]):
                        findings.append(finding("forbidden-import", modules[source]["path"],
                                                locations.get((source, chain[1]), 1),
                                                " -> ".join(chain), dependency_path=chain))
                    if target not in seen:
                        seen.add(target)
                        queue.append(chain)
    return edges, findings


def symbol_at(module, line):
    owners = [n for n in ast.walk(module["tree"])
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and n.lineno <= line <= n.end_lineno]
    return ".".join(n.name for n in sorted(owners, key=lambda n: n.lineno)) or "<module>"


def handlers(modules, identities=None):
    result = []
    for name, module in modules.items():
        aliases = aliases_for(module)
        parents = {child: parent for parent in ast.walk(module["tree"])
                   for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(module["tree"]):
            if not isinstance(node, ast.ExceptHandler):
                continue
            values = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            types = sorted(dotted(value, aliases) if value else "<bare>" for value in values)
            broad = {"Exception", "BaseException", "builtins.Exception",
                     "builtins.BaseException", "<bare>", "<indirect>"}
            if any(value in broad for value in types):
                result.append({
                    "module_id": identities[name] if identities is not None else name,
                    "path": module["path"], "line": node.lineno,
                    "symbol": symbol_at(module, node.lineno),
                    "handler_fingerprint": digest([syntax(node), syntax(parents[node])]),
                    "caught_types": types,
                })
    return sorted(result, key=lambda h: (h["path"], h["line"]))


def approved_exceptions(current, records, passed_tests):
    keys = ("module_id", "symbol", "handler_fingerprint", "caught_types")
    counts = Counter(digest({key: h[key] for key in keys}) for h in current)
    approved = []
    for handler in current:
        matches = [r for r in records if all(r.get(key) == handler[key] for key in keys)]
        if (len(matches) == 1 and matches[0]["status"] == "active"
                and counts[digest({key: handler[key] for key in keys})] == 1
                and matches[0]["evidence_tests"]
                and set(matches[0]["evidence_tests"]) <= passed_tests):
            approved.append((handler, matches[0]))
    return approved


def check_exceptions(current, records, passed_tests):
    findings = []
    approved = approved_exceptions(current, records, passed_tests)
    used = {record["id"] for _, record in approved}
    for handler in current:
        if not any(handler is h for h, _ in approved):
            findings.append(finding("broad-handler", handler["path"], handler["line"],
                                    "Requires exact protected approval and passing failure evidence",
                                    handler=handler))
    for record in records:
        if record["id"] not in used:
            findings.append(finding("unused-exception", record.get("path", ""),
                                    message=f"Stale, proposed or unproven exception: {record['id']}"))
    return findings


def approved_handler_sites(current, records, passed_tests):
    return {(h["path"], h["line"]) for h, _ in approved_exceptions(current, records, passed_tests)}


def ratchet(current, baseline):
    keys = ("module_id", "symbol", "source_fingerprint", "rule", "message_fingerprint")
    identity = lambda value: tuple(value[key] for key in keys)
    actual = Counter(identity(value) for value in current)
    allowed = Counter()
    for entry in baseline:
        allowed[identity(entry)] += entry["occurrences"]
    findings = []
    for key, count in (actual - allowed).items():
        findings.append(finding("new-type-debt", message=f"{key[0]}:{key[1]} [{key[3]}]",
                                occurrences=count, identity=dict(zip(keys, key))))
    for key, count in (allowed - actual).items():
        findings.append(finding("retired-type-debt", message=f"Remove stale baseline for {key[0]}:{key[1]}",
                                occurrences=count))
    return findings


def suppressions(module):
    result = Counter()
    aliases = aliases_for(module)
    parents = {child: parent for parent in ast.walk(module["tree"])
               for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(module["tree"]):
        if isinstance(node, (ast.Name, ast.Attribute, ast.Call)) and dotted(node, aliases) in {
            f"{package}.{name}" for package in ("typing", "typing_extensions")
            for name in ("no_type_check", "no_type_check_decorator")
        }:
            context = node
            while context in parents and not isinstance(context, ast.stmt):
                context = parents[context]
            result["typing-suppression", syntax(context)] += 1
    for token in tokenize.generate_tokens(io.StringIO(module["text"]).readline):
        if token.type == tokenize.COMMENT and re.search(
                r"noqa|type:\s*ignore|mypy:|pyright:|ruff:", token.string, re.I):
            statements = [n for n in ast.walk(module["tree"])
                          if isinstance(n, ast.stmt)
                          and n.lineno <= token.start[0] <= n.end_lineno]
            context = (syntax(min(statements, key=lambda n: n.end_lineno - n.lineno))
                       if statements else "<module>")
            result[token.string.strip(), context] += 1
    return result


def annotations(module):
    result = set()
    for node in ast.walk(module["tree"]):
        symbol = symbol_at(module, getattr(node, "lineno", 0))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            args += [a for a in (node.args.vararg, node.args.kwarg) if a]
            result.update((symbol, a.arg) for a in args if a.annotation)
            if node.returns:
                result.add((symbol, "<return>"))
        elif isinstance(node, ast.AnnAssign):
            result.add((symbol, ast.dump(node.target)))
    return result


def source_policy(before, after, identities, scope, surfaces=()):
    findings = []
    stable = {entry["id"]: entry["import_name"] for entry in surfaces}
    for name, module in after.items():
        old = before.get(stable.get(identities[name], identities[name]))
        inherited = suppressions(old) if old else Counter()
        if suppressions(module) - inherited:
            findings.append(finding("suppression", module["path"],
                                    message="New or broadened suppression requires protected policy review"))
        if old and identities[name] in scope and annotations(old) - annotations(module):
            findings.append(finding("annotation-removed", module["path"],
                                    message="Covered annotations cannot be silently removed"))
    return findings


def tool_json(result, tool):
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise QualityError(f"{tool} execution failed: {result.stderr[:1000]}")
    try:
        values = json.loads(result.stdout) if tool == "ruff" else [
            json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError as exc:
        raise QualityError(f"{tool} returned invalid JSON") from exc
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise QualityError(f"{tool} returned an unexpected result")
    if tool == "mypy":
        for value in values:
            validate_mypy_diagnostic(value)
    errors = values if tool == "ruff" else [v for v in values if v["severity"] == "error"]
    if bool(errors) != (result.returncode == 1):
        raise QualityError(f"{tool} exit status contradicts its diagnostics")
    return values


def validate_mypy_diagnostic(value):
    exact_keys(value, ("file", "line", "column", "end_line", "end_column",
                       "message", "hint", "code", "severity"), "mypy diagnostic")
    text_fields(value, ("file", "message", "severity"))
    if (value["severity"] not in ("error", "note")
            or any(type(value[key]) is not int or value[key] < -1
                   for key in ("line", "column"))
            or any(value[key] is not None and (type(value[key]) is not int or value[key] < -1)
                   for key in ("end_line", "end_column"))
            or any(value[key] is not None and not isinstance(value[key], str)
                   for key in ("hint", "code"))
            or (value["severity"] == "error" and not value["code"])):
        raise QualityError("Malformed mypy diagnostic")


def seal_report(report):
    report = {k: v for k, v in report.items() if k != "artifact_integrity"}
    return {**report, "artifact_integrity": digest(report)}


def report_identity(records, repository, run_id, run_attempt):
    if repository != REPOSITORY or any(
            not re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, run_attempt)):
        raise QualityError("Aggregate requires this repository and an exact CI run/attempt")
    return {"repository": repository, "run_id": run_id, "run_attempt": run_attempt,
            "policy_sha": digest(records), "toolchain": records["policy.json"]["toolchain"]}


def nonnegative(value, label):
    if type(value) is not int or value < 0:
        raise QualityError(f"{label} must be a nonnegative integer")


def validate_report(report, base, head, checks, expected):
    exact_keys(report, ("schema_version", "repository", "base_sha", "head_sha", "policy_sha",
                        "run_id", "run_attempt", "bootstrap", "toolchain", "coverage",
                        "handler_inventory", "checks", "artifact_integrity"), "quality report")
    if (type(report["schema_version"]) is not int or report["schema_version"] != 2
            or report["bootstrap"] is not False
            or not all(re.fullmatch("[0-9a-f]{40}", sha) for sha in (base, head))
            or report["base_sha"] != base or report["head_sha"] != head
            or any(report[key] != value for key, value in expected.items())
            or report["artifact_integrity"] != seal_report(report)["artifact_integrity"]):
        raise QualityError("Report schema, integrity or independently expected identity mismatch")
    coverage = report["coverage"]
    exact_keys(coverage, ("total", "blocking", "uncovered"), "coverage")
    nonnegative(coverage["total"], "coverage total")
    strings(coverage["blocking"], "blocking coverage")
    strings(coverage["uncovered"], "uncovered coverage", empty=True)
    sources = coverage["blocking"] + coverage["uncovered"]
    if (coverage["total"] != len(set(sources)) or len(sources) != len(set(sources))
            or any(not path.startswith("src/") or not path.endswith(".py")
                   or "\\" in path or any(p in (".", "..", "") for p in path.split("/"))
                   for path in sources)):
        raise QualityError("Invalid or contradictory source coverage")
    if not isinstance(report["handler_inventory"], list):
        raise QualityError("Handler inventory must be an array")
    sites = set()
    for handler in report["handler_inventory"]:
        exact_keys(handler, ("module_id", "path", "line", "symbol", "handler_fingerprint",
                             "caught_types"), "handler inventory site")
        text_fields(handler, ("module_id", "path", "symbol", "handler_fingerprint"))
        strings(handler["caught_types"], "caught types")
        nonnegative(handler["line"], "handler line")
        site = (handler["path"], handler["line"])
        if (handler["path"] not in sources or handler["line"] == 0 or site in sites
                or not re.fullmatch("[0-9a-f]{64}", handler["handler_fingerprint"])):
            raise QualityError("Invalid handler inventory site")
        sites.add(site)
    exact_keys(report["checks"], checks, "requested checks")
    for check, result in report["checks"].items():
        extra = {
            "typing": ("diagnostics", "outside_scope_diagnostics", "baseline_entries"),
            "architecture": ("grimp_modules", "edges"), "exceptions": ("exception_ids_used",),
        }.get(check, ())
        exact_keys(result, ("status", "findings", "duration_seconds", *extra), f"{check} result")
        if (result["status"] != "passed" or result["findings"] != []
                or type(result["duration_seconds"]) not in (int, float)
                or not math.isfinite(result["duration_seconds"]) or result["duration_seconds"] < 0):
            raise QualityError("A successful check requires empty findings and valid duration")
        if check == "architecture":
            nonnegative(result["edges"], "edge count")
            nonnegative(result["grimp_modules"], "Grimp coverage")
            if result["grimp_modules"] > coverage["total"]:
                raise QualityError("Grimp coverage exceeds runtime coverage")
        elif check == "exceptions":
            strings(result["exception_ids_used"], "used exceptions", empty=True)
        elif check == "typing":
            nonnegative(result["baseline_entries"], "baseline entries")
            for key in ("diagnostics", "outside_scope_diagnostics"):
                if not isinstance(result[key], list):
                    raise QualityError("Typing diagnostics must be arrays")
            for value in result["outside_scope_diagnostics"]:
                validate_mypy_diagnostic(value)
            for value in result["diagnostics"]:
                exact_keys(value, ("module_id", "symbol", "source_fingerprint", "rule",
                                   "message_fingerprint", "path", "line", "message"),
                           "anchored type diagnostic")
                text_fields(value, ("module_id", "symbol", "rule", "message",
                                    "source_fingerprint", "message_fingerprint", "path"))
                nonnegative(value["line"], "diagnostic line")
                if (value["path"] not in coverage["blocking"]
                        or not all(re.fullmatch("[0-9a-f]{64}", value[key]) for key in
                                   ("source_fingerprint", "message_fingerprint"))):
                    raise QualityError("Invalid anchored type diagnostic")


def report_valid(report, base, head, checks, expected):
    try:
        validate_report(report, base, head, checks, expected)
    except QualityError:
        return False
    return True


def jobs_passed(results):
    return set(results) == set(REQUIRED_JOBS) and all(
        results[name] == "success" for name in REQUIRED_JOBS)


def evidence_tests(path, source_root=None):
    if path is None:
        return set()
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise QualityError("Invalid pytest evidence") from exc
    cases = list(root.iter("testcase"))
    if not cases:
        raise QualityError("Failure evidence has no test cases")
    source_root = source_root or Path.cwd()
    modules = {".".join(p.relative_to(source_root).with_suffix("").parts): p
               for p in (source_root / "tests").rglob("*.py")}
    seen, passed = set(), set()
    for case in cases:
        text_fields(case.attrib, ("classname", "name"))
        classname = case.attrib["classname"]
        module = next((name for name in sorted(modules, key=len, reverse=True)
                       if classname == name or classname.startswith(name + ".")), None)
        if module is None:
            raise QualityError(f"Unknown pytest evidence module: {classname}")
        classes = classname[len(module):].strip(".").split(".") if classname != module else []
        selector = "::".join([modules[module].relative_to(source_root).as_posix(),
                              *classes, case.attrib["name"]])
        if selector in seen:
            raise QualityError(f"Ambiguous duplicate pytest evidence: {selector}")
        seen.add(selector)
        if not any(n.tag in ("failure", "error", "skipped") for n in case):
            passed.add(selector)
    return passed


def policy_changes(root, base, base_records, candidate):
    findings = []
    changed = set(git(root, "diff", "--name-only", base, "--").splitlines())
    changed.update(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    for path in sorted(changed):
        if path.startswith(PROTECTED):
            # Debt retirement is safe only if every remaining record is byte-equivalent.
            if path == ".quality/typing-baseline.json":
                old = base_records["typing-baseline.json"]["entries"]
                if all(entry in old for entry in candidate["typing-baseline.json"]["entries"]):
                    continue
            if path == ".quality/typing-scope.json":
                old = base_records["typing-scope.json"]
                new = candidate["typing-scope.json"]
                if (set(old["module_ids"]) <= set(new["module_ids"])
                        and {k: v for k, v in old.items() if k != "module_ids"}
                        == {k: v for k, v in new.items() if k != "module_ids"}):
                    continue
            findings.append(finding("protected-policy", path,
                                    message="Policy change requires independent maintainer review; cannot self-approve"))
    return findings


def grimp_check(root, modules, edges):
    packages = [name for name, m in modules.items()
                if m["package"] and "." not in name and name != "__init__"]
    with tempfile.TemporaryDirectory(prefix="orchestrator-grimp-") as directory:
        workspace = Path(directory)
        write_analysis_sources(workspace, modules)
        result = static_tool("grimp", workspace, packages)
    if result.returncode or result.stderr.strip():
        raise QualityError("Grimp execution failed: " + result.stderr[-1000:])
    graph = json.loads(result.stdout)
    if not isinstance(graph, dict) or not set(graph) <= set(modules):
        raise QualityError("Grimp discovered modules omitted by AST collection")
    missing = [(source, target) for source in graph
               for target in graph[source]
               if target not in edges[source]]
    if missing:
        raise QualityError(f"AST/Grimp import disagreement: {missing}")
    return len(graph)


def type_findings(values, modules, identities, root, scope):
    blocking, outside = [], []
    by_path = {m["path"]: name for name, m in modules.items()}
    for value in values:
        if value.get("severity") != "error":
            continue
        path = Path(value["file"])
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError as exc:
                raise QualityError("mypy diagnostic outside repository") from exc
        name = by_path.get(path.as_posix())
        if name is None:
            raise QualityError(f"mypy diagnostic has unknown source: {path}")
        if identities[name] not in scope:
            outside.append(value)
            continue
        module = modules[name]
        line = value["line"]
        statements = [n for n in ast.walk(module["tree"])
                      if isinstance(n, ast.stmt) and n.lineno <= line <= n.end_lineno]
        if not statements:
            raise QualityError("Unanchored mypy diagnostic")
        context = min(statements, key=lambda n: n.end_lineno - n.lineno)
        blocking.append({
            "module_id": identities[name], "symbol": symbol_at(module, line),
            "source_fingerprint": syntax(context), "rule": value["code"],
            "message_fingerprint": digest(value["message"]),
            "path": module["path"], "line": line, "message": value["message"],
        })
    return blocking, outside


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("all", *CHECKS), default="all")
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--test-results", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    requested = CHECKS if args.check == "all" else (args.check,)
    report = {"schema_version": 2, "repository": REPOSITORY,
              "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
              "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
              "base_sha": None, "head_sha": None, "policy_sha": None, "checks": {}}
    exit_code = 0
    try:
        if not sys.flags.isolated or not sys.flags.no_site:
            raise QualityError("Run static analysis with python -I -S to disable startup hooks")
        base = git(root, "rev-parse", "--verify", args.base_ref + "^{commit}").strip()
        head = git(root, "rev-parse", "HEAD").strip()
        report.update(base_sha=base, head_sha=head)
        candidate = load_records(root)
        with tempfile.TemporaryDirectory(prefix="orchestrator-quality-") as directory:
            trusted = Path(directory)
            files = git(root, "ls-tree", "-r", "--name-only", base).splitlines()
            bootstrap = ".quality/policy.json" not in files
            # Reconstruct data only. Never check out or execute base runtime modules.
            for path in files:
                if path.startswith(("src/", ".quality/")) or path in (
                        "pyproject.toml", ".importlinter", "requirements-quality.txt"):
                    dest = trusted / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(git(root, "show", f"{base}:{path}"), encoding="utf-8")
            records = candidate if bootstrap else load_records(trusted)
            policy = records["policy.json"]
            report["policy_sha"] = digest(records)
            report["bootstrap"] = bootstrap
            versions_result = static_tool("versions", trusted)
            if versions_result.returncode or versions_result.stderr.strip():
                raise QualityError("Installed tool discovery failed: " + versions_result.stderr[-1000:])
            installed = json.loads(versions_result.stdout)
            report["toolchain"] = {}
            for package, pin in policy["toolchain"].items():
                version = installed[package]
                if version != pin:
                    raise QualityError(f"{package} requires {pin}, found {version}")
                report["toolchain"][package] = version
            modules = collect(root, policy["runtime_roots"])
            before = collect(trusted, policy["runtime_roots"])
            surfaces = records["module-surfaces.json"]["entries"]
            identities, identity_findings = module_identities(before, modules, surfaces)
            scope = set(records["typing-scope.json"]["module_ids"])
            scope.update(candidate["typing-scope.json"]["module_ids"])
            scope.update(entry["id"] for entry in surfaces if entry["typing_status"] == "blocking")
            # The protected adoption inventory is immutable across successive PR bases.
            scope.update(identities[n] for n in modules
                         if n not in policy["modules"] or identities[n] not in policy["modules"])
            if not scope <= set(identities.values()):
                identity_findings.append(finding("typing-scope", message="Covered module missing"))
            report["coverage"] = {
                "total": len(modules), "blocking": sorted(
                    m["path"] for n, m in modules.items() if identities[n] in scope),
                "uncovered": sorted(m["path"] for n, m in modules.items() if identities[n] not in scope),
            }
            config_root = root if bootstrap else trusted
            base_config = tomllib.loads((config_root / "pyproject.toml").read_text())
            head_config = tomllib.loads((root / "pyproject.toml").read_text())
            analysis = trusted / "analysis"
            analysis.mkdir()
            write_analysis_sources(analysis, modules)
            import_config = (config_root / ".importlinter").read_text() if (
                config_root / ".importlinter").exists() else ""
            static_configuration(base_config, import_config)
            (analysis / "pyproject.toml").write_text(
                (config_root / "pyproject.toml").read_text(), encoding="utf-8")
            (analysis / ".importlinter").write_text(import_config, encoding="utf-8")
            current_handlers = handlers(modules, identities)
            report["handler_inventory"] = current_handlers
            for check in requested:
                start = time.monotonic()
                found = []
                details = {}
                try:
                    if check == "policy":
                        if bootstrap:
                            found.append(finding("bootstrap-review", ".quality/policy.json",
                                message="No protected base policy exists. Bootstrap requires maintainer review and administrator activation."))
                        else:
                            found.extend(policy_changes(root, base, records, candidate))
                            for tool in ("ruff", "mypy", "importlinter"):
                                if base_config.get("tool", {}).get(tool) != head_config.get("tool", {}).get(tool):
                                    found.append(finding("protected-policy", "pyproject.toml",
                                                         message=f"{tool} configuration changed"))
                        found.extend(identity_findings)
                        found.extend(surface_findings(candidate["module-surfaces.json"]["entries"],
                                                      modules, identities, scope))
                        found.extend(surface_provenance(root, candidate["module-surfaces.json"]["entries"]))
                        found.extend(source_policy(before, modules, identities, scope, surfaces))
                        for path in (root / "src").rglob("*"):
                            if path.name in ("pyproject.toml", "mypy.ini", ".mypy.ini",
                                             "ruff.toml", ".ruff.toml", ".importlinter") or path.suffix == ".pyi":
                                found.append(finding("nested-policy", path.relative_to(root).as_posix(),
                                                     message="Nested quality configuration is not permitted"))
                    elif check == "architecture":
                        edges, found = architecture(modules, policy["contracts"],
                                                    evidence_tests(args.test_results, root), surfaces)
                        details["grimp_modules"] = grimp_check(root, modules, edges)
                        details["edges"] = sum(map(len, edges.values()))
                        result = static_tool("import-linter", analysis, [
                            "--config", str(analysis / ".importlinter"), "--no-cache"])
                        if result.returncode not in (0, 1):
                            raise QualityError("Import Linter execution failed")
                        if result.returncode:
                            found.append(finding("import-linter", message=result.stdout[-3000:]))
                    elif check == "exceptions":
                        found = check_exceptions(current_handlers, records["exceptions.json"]["entries"],
                                                 evidence_tests(args.test_results, root))
                        details["exception_ids_used"] = [
                            r["id"] for r in records["exceptions.json"]["entries"]
                            if not any(f.get("message", "").endswith(r["id"]) for f in found)]
                    elif check == "lint":
                        approved = approved_handler_sites(
                            current_handlers, records["exceptions.json"]["entries"],
                            evidence_tests(args.test_results, root))
                        values = tool_json(static_tool("ruff", analysis, [
                            "check", "--no-cache",
                            "--config", str(analysis / "pyproject.toml"),
                            "--output-format", "json", *[m["path"] for m in modules.values()],
                        ]), "ruff")
                        for value in values:
                            path = Path(value["filename"]).relative_to(analysis).as_posix()
                            if (value["code"] in ("BLE001", "E722")
                                    and (path, value["location"]["row"]) in approved):
                                continue
                            found.append(finding(value["code"], path, value["location"]["row"],
                                                 value["message"]))
                    elif check == "typing":
                        values = tool_json(static_tool("mypy", analysis, [
                            "--config-file", str(analysis / "pyproject.toml"), "--no-incremental",
                            "--cache-dir", str(analysis / "mypy-cache"),
                            "--python-executable", sys.executable,
                            "--output", "json", *report["coverage"]["blocking"],
                        ]), "mypy")
                        for value in values:
                            if Path(value["file"]).is_absolute():
                                value["file"] = Path(value["file"]).relative_to(analysis).as_posix()
                        blocking, outside = type_findings(values, modules, identities, analysis, scope)
                        baseline = candidate["typing-baseline.json"]["entries"]
                        trusted_baseline = records["typing-baseline.json"]["entries"]
                        # Candidate additions are never allowances, even if policy runs separately.
                        baseline = [entry for entry in baseline if entry in trusted_baseline]
                        found = ratchet(blocking, baseline)
                        details.update(diagnostics=blocking, outside_scope_diagnostics=outside,
                                       baseline_entries=len(candidate["typing-baseline.json"]["entries"]))
                    report["checks"][check] = {
                        "status": "violations" if found else "passed",
                        "findings": found, "duration_seconds": round(time.monotonic() - start, 3),
                        **details,
                    }
                    exit_code = max(exit_code, int(bool(found)))
                except (QualityError, ValueError, OSError, KeyError, TypeError) as exc:
                    report["checks"][check] = {"status": "error", "message": str(exc)}
                    exit_code = 2
    except (QualityError, ValueError, OSError, KeyError, TypeError, configparser.Error,
            importlib.metadata.PackageNotFoundError) as exc:
        for check in requested:
            report["checks"].setdefault(check, {"status": "error", "message": str(exc)})
        exit_code = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(seal_report(report), indent=2) + "\n", encoding="utf-8")
    for check, result in report["checks"].items():
        print(f"{check}: {result['status']} ({len(result.get('findings', []))} findings)")
        if result.get("message"):
            print(result["message"])
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--static-tool":
        static_tool_main(sys.argv[2], sys.argv[3:])
        raise SystemExit(0)
    raise SystemExit(main())
