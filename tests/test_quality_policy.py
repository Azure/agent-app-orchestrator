"""Mutation fixtures for the source-only quality gate; no Azure imports."""

import ast
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


QUALITY = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / ".github/scripts/check-quality.py")
)


def tree(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return QUALITY["collect"](tmp_path, ["src"])


def rules(**overrides):
    return {
        "forbidden": [{"from": ["connectors", "plugins", "telemetry"],
                       "to": ["main", "api"]}],
        "private_access": [],
        "dynamic_imports": [],
        **overrides,
    }


def graph(tmp_path, files, **overrides):
    modules = tree(tmp_path, files)
    edges, findings = QUALITY["architecture"](modules, rules(**overrides))
    return edges, findings


@pytest.mark.parametrize("files", [
    {"a.py": "import b", "b.py": "import a"},
    {"a.py": "import b", "b.py": "import c", "c.py": "import a"},
    {"a.py": "def f():\n import b", "b.py": "import a"},
    {"a.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n import b",
     "b.py": "import a"},
    {"a.py": "import pkg.b", "pkg/__init__.py": "", "pkg/b.py": "import a"},
])
def test_cycles_cover_flat_cross_root_late_and_type_only_imports(tmp_path, files):
    _, findings = graph(tmp_path, files)
    cycles = [f for f in findings if f["rule"] == "cycle"]
    assert cycles
    assert cycles[0]["dependency_path"][0] == cycles[0]["dependency_path"][-1]


def test_relative_sibling_does_not_invent_parent_initialization_edge(tmp_path):
    edges, findings = graph(tmp_path, {
        "pkg/__init__.py": "from . import a",
        "pkg/a.py": "from . import b",
        "pkg/b.py": "VALUE = 1",
    })
    assert not findings
    assert edges["pkg.a"] == {"pkg.b"}
    assert edges["pkg"] == {"pkg.a"}


def test_qualified_child_attribute_does_not_invent_facade_back_edge(tmp_path):
    edges, findings = graph(tmp_path, {
        "pkg/__init__.py": "from . import client",
        "pkg/client.py": "import pkg.impl\nvalue = pkg.impl.value",
        "pkg/impl.py": "value = 1",
    })
    assert not findings
    assert edges["pkg.client"] == {"pkg.impl"}


def test_real_facade_reexports_are_followed(tmp_path):
    _, findings = graph(tmp_path, {
        "a.py": "from pkg import value",
        "pkg/__init__.py": "from .impl import value",
        "pkg/impl.py": "import a\nvalue = 1",
    })
    assert any(f["rule"] == "cycle" for f in findings)


@pytest.mark.parametrize("statement", [
    "import api",
    "from api import route",
    "import helper",
])
def test_forbidden_direct_and_transitive_directions(tmp_path, statement):
    _, findings = graph(tmp_path, {
        "connectors/__init__.py": "",
        "connectors/client.py": statement,
        "helper.py": "import api.route",
        "api/__init__.py": "",
        "api/route.py": "",
    })
    assert any(f["rule"] == "forbidden-import" for f in findings)


@pytest.mark.parametrize("statement", [
    "from pkg._impl import public",
    "import pkg._impl as impl",
    "from pkg import _impl as impl",
    "from pkg.public import _secret as secret",
    "import pkg.public as p\nvalue = p._secret",
    "import pkg.public\nvalue = pkg.public._secret",
    "from pkg.public import Client as C\nvalue = C._secret",
    "import pkg.public as p\nvalue = p.Client._secret",
    "import pkg.public as p\nvalue = getattr(p, '_secret')",
])
def test_private_module_and_member_access(tmp_path, statement):
    _, findings = graph(tmp_path, {
        "consumer.py": statement, "pkg/__init__.py": "",
        "pkg/_impl.py": "public = 1",
        "pkg/public.py": "_secret = 1\nclass Client:\n _secret = 1",
    })
    assert any(f["rule"] == "private-access" for f in findings)


def test_private_facade_forwarding_and_explicit_compatibility_access(tmp_path):
    _, findings = graph(tmp_path, {
        "consumer.py": "from pkg import public",
        "pkg/__init__.py": "from ._impl import public",
        "pkg/_impl.py": "public = 1",
        "compat.py": "from pkg._impl import public",
    }, private_access=[{"importer": "compat", "target": "pkg._impl"}])
    assert not findings


def test_sibling_subpackages_cannot_access_each_others_private_members(tmp_path):
    _, findings = graph(tmp_path, {
        "pkg/__init__.py": "", "pkg/one/__init__.py": "", "pkg/two/__init__.py": "",
        "pkg/one/client.py": "from pkg.two.impl import _secret",
        "pkg/two/impl.py": "_secret = 1",
    })
    assert any(f["rule"] == "private-access" for f in findings)


@pytest.mark.parametrize("statement", [
    "from pkg.two import _secret",
    "from ..two import _secret",
    "import pkg.two as two\nvalue = two._secret",
])
def test_package_initializer_private_members_belong_to_that_package(tmp_path, statement):
    _, findings = graph(tmp_path, {
        "pkg/__init__.py": "", "pkg/one/__init__.py": "",
        "pkg/one/client.py": statement,
        "pkg/two/__init__.py": "_secret = 1",
    })
    assert any(f["rule"] == "private-access" for f in findings)


def test_package_initializer_allows_its_children_and_exact_compatibility_access(tmp_path):
    _, findings = graph(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/two/__init__.py": "_secret = 1",
        "pkg/two/client.py": "from . import _secret",
        "compat.py": "from pkg.two import _secret",
    }, private_access=[{"importer": "compat", "target": "pkg.two._secret"}])
    assert not findings


def test_private_package_itself_remains_visible_to_its_containing_package(tmp_path):
    _, findings = graph(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/client.py": "from . import _internal",
        "pkg/_internal/__init__.py": "_secret = 1",
    })
    assert not findings


@pytest.mark.parametrize("statement", [
    "import importlib\nimportlib.import_module(name)",
    "from importlib import import_module as load\nload(name)",
    "__import__(name)",
    "from builtins import __import__ as load\nload(name)",
])
def test_uninventoried_dynamic_loading_is_not_certified_safe(tmp_path, statement):
    _, findings = graph(tmp_path, {"a.py": statement})
    assert any(f["rule"] == "dynamic-import" for f in findings)


def test_literal_dynamic_import_participates_in_cycles(tmp_path):
    _, findings = graph(tmp_path, {
        "a.py": "import importlib as i\ni.import_module('b')",
        "b.py": "import a",
    })
    assert any(f["rule"] == "cycle" for f in findings)


def test_missing_first_party_target_is_an_error(tmp_path):
    _, findings = graph(tmp_path, {
        "a.py": "import pkg.missing", "pkg/__init__.py": "",
    })
    assert any(f["rule"] == "unresolved-import" for f in findings)


def test_collection_never_executes_runtime_source(tmp_path):
    modules = tree(tmp_path, {"main.py": "raise RuntimeError('do not execute')"})
    assert set(modules) == {"main"}


@pytest.mark.parametrize("header,prefix", [
    ("except:", ""),
    ("except Exception:", ""),
    ("except BaseException:", ""),
    ("except (ValueError, Exception):", ""),
    ("except builtins.Exception:", "import builtins\n"),
    ("except b.BaseException:", "import builtins as b\n"),
    ("except E:", "from builtins import Exception as E\n"),
    ("except E:", "E = Exception\n"),
    ("except* Exception:", ""),
])
@pytest.mark.parametrize("body", ["pass", "raise", "logging.exception('failed')"])
def test_broad_inventory_includes_logged_rethrown_aliased_and_group_handlers(
    tmp_path, header, prefix, body,
):
    modules = tree(tmp_path, {
        "a.py": f"{prefix}try:\n work()\n{header}\n {body}\n",
    })
    handlers = QUALITY["handlers"](modules)
    assert len(handlers) == 1
    assert handlers[0]["caught_types"]


def test_indirect_exception_expression_needs_review(tmp_path):
    modules = tree(tmp_path, {
        "a.py": "try:\n work()\nexcept types_to_catch():\n pass",
    })
    assert QUALITY["handlers"](modules)[0]["caught_types"] == ["<indirect>"]


def test_exception_alias_assigned_by_call_needs_review(tmp_path):
    modules = tree(tmp_path, {
        "a.py": "E = choose_errors()\ntry:\n work()\nexcept E:\n pass",
    })
    assert QUALITY["handlers"](modules)[0]["caught_types"] == ["<indirect>"]


def test_narrow_exception_is_not_broad(tmp_path):
    modules = tree(tmp_path, {
        "a.py": "try:\n work()\nexcept (ValueError, TypeError):\n raise",
    })
    assert not QUALITY["handlers"](modules)


def approval(handler):
    return {
        **handler, "id": "test-boundary", "status": "active",
        "boundary": "test boundary", "reason": "translate provider failure",
        "failure_outcome": "propagation", "diagnostic_path": "caller exception",
        "evidence_tests": ["tests/test_failure.py::test_failure"],
        "review": "protected-base review", "review_by_stage": "next-expansion",
    }


def test_exception_records_are_exact_and_require_executed_evidence(tmp_path):
    modules = tree(tmp_path, {"a.py": "try:\n work()\nexcept Exception:\n raise"})
    handlers = QUALITY["handlers"](modules)
    record = approval(handlers[0])
    passed = {"tests/test_failure.py::test_failure"}
    assert not QUALITY["check_exceptions"](handlers, [record], passed)
    assert QUALITY["check_exceptions"](handlers, [record], set())
    changed = tree(tmp_path, {"a.py": "try:\n work()\nexcept Exception:\n pass"})
    assert QUALITY["check_exceptions"](QUALITY["handlers"](changed), [record], passed)
    assert QUALITY["check_exceptions"]([], [record], passed)
    record["status"] = "proposed"
    assert QUALITY["check_exceptions"](handlers, [record], passed)


def test_changed_try_operation_and_duplicate_handlers_cannot_reuse_approval(tmp_path):
    code = "try:\n work()\nexcept Exception:\n raise"
    modules = tree(tmp_path / "before", {"a.py": code})
    handlers = QUALITY["handlers"](modules)
    record = approval(handlers[0])
    passed = {"tests/test_failure.py::test_failure"}
    changed = tree(tmp_path / "after", {"a.py": code.replace("work()", "other()")})
    assert QUALITY["check_exceptions"](QUALITY["handlers"](changed), [record], passed)
    duplicated = tree(tmp_path / "duplicate", {"a.py": code + "\n" + code})
    current = QUALITY["handlers"](duplicated)
    assert QUALITY["check_exceptions"](current, [record], passed)
    assert not QUALITY["approved_handler_sites"](current, [record], passed)


def test_baseline_is_an_individual_multiset_not_a_count():
    finding = {"module_id": "a", "symbol": "f", "source_fingerprint": "context-a",
               "rule": "assignment", "message_fingerprint": "message-a"}
    record = {**finding, "occurrences": 1}
    compare = QUALITY["ratchet"]
    assert compare([finding], [record]) == []
    assert compare([{**finding, "source_fingerprint": "context-b"}], [record])
    assert compare([finding, finding], [record])
    assert compare([], [record])  # Retire absent debt; no reusable allowances.


def test_module_moves_keep_identity_and_ambiguous_copies_fail(tmp_path):
    before = tree(tmp_path / "old", {"old.py": "x: int = 1"})
    after = tree(tmp_path / "new", {"new.py": "x: int = 1"})
    ids, findings = QUALITY["module_identities"](before, after)
    assert ids == {"new": "old"}
    assert not findings
    copies = tree(tmp_path / "copies", {"one.py": "x: int = 1", "two.py": "x: int = 1"})
    assert QUALITY["module_identities"](before, copies)[1]


def test_deleted_annotations_and_new_suppressions_fail(tmp_path):
    before = tree(tmp_path / "old", {"a.py": "def f(x: str) -> str:\n return x"})
    after = tree(tmp_path / "new", {"a.py": "def f(x):\n return x # type: ignore"})
    findings = QUALITY["source_policy"](before, after, {"a": "a"}, {"a"})
    assert {f["rule"] for f in findings} >= {"annotation-removed", "suppression"}


@pytest.mark.parametrize("prefix,decorator", [
    ("import typing", "typing.no_type_check"),
    ("import typing as t", "t.no_type_check"),
    ("from typing import no_type_check", "no_type_check"),
    ("from typing import no_type_check as ignore", "ignore"),
    ("import typing\nignore = typing.no_type_check", "ignore"),
])
def test_ast_typing_suppression_cannot_hide_annotated_wrong_return(tmp_path, prefix, decorator):
    before = tree(tmp_path / "old", {"a.py": "def f() -> int:\n return 1"})
    after = tree(tmp_path / "new", {
        "a.py": f"{prefix}\n@{decorator}\ndef f() -> int:\n return 'wrong'",
    })
    findings = QUALITY["source_policy"](before, after, {"a": "a"}, {"a"})
    assert any(f["rule"] == "suppression" for f in findings)


def test_baseline_schema_rejects_unknown_and_duplicate_entries():
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["validate_records"]({"schema_version": 99, "entries": []})
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["validate_records"](
            {"schema_version": 1, "entries": [{"id": "same"}, {"id": "same"}]}
        )


@pytest.mark.parametrize("value", [
    {"schema_version": True, "entries": []},
    {"schema_version": 1, "entries": [{"id": ""}]},
    {"schema_version": 1, "entries": [{"id": 3}]},
])
def test_invalid_record_identity_is_not_accepted(value):
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["validate_records"](value)


@pytest.mark.parametrize("value", ["skipped", "cancelled", "failure", "neutral", None])
def test_aggregate_rejects_missing_or_non_success_jobs(value):
    results = {name: "success" for name in QUALITY["REQUIRED_JOBS"]}
    results["tests"] = value
    assert not QUALITY["jobs_passed"](results)


def report_context():
    records = QUALITY["load_records"](Path(QUALITY["__file__"]).resolve().parents[2])
    return QUALITY["report_identity"](records, QUALITY["REPOSITORY"], "1234", "1")


def successful_report(check, expected, base="a" * 40, head="b" * 40):
    details = {
        "typing": {"diagnostics": [], "outside_scope_diagnostics": [], "baseline_entries": 0},
        "architecture": {"grimp_modules": 0, "edges": 0},
        "exceptions": {"exception_ids_used": []},
    }.get(check, {})
    return QUALITY["seal_report"]({
        "schema_version": 2, **expected, "base_sha": base, "head_sha": head,
        "bootstrap": False, "coverage": {"total": 1, "blocking": ["src/a.py"], "uncovered": []},
        "handler_inventory": [],
        "checks": {check: {"status": "passed", "findings": [], "duration_seconds": 0.1, **details}},
    })


def test_aggregate_rejects_stale_or_tampered_report():
    expected = report_context()
    report = successful_report("lint", expected)
    assert QUALITY["report_valid"](report, "a" * 40, "b" * 40, {"lint"}, expected)
    assert not QUALITY["report_valid"](report, "a" * 40, "c" * 40, {"lint"}, expected)
    report["checks"]["lint"]["status"] = "violations"
    assert not QUALITY["report_valid"](report, "a" * 40, "b" * 40, {"lint"}, expected)


@pytest.mark.parametrize("metadata", [
    {"schema_version": 999}, {"repository": "another/repository"}, {"bootstrap": True},
    {"toolchain": {}}, {"run_id": "previous-run"}, {"run_attempt": "previous-attempt"},
    {"policy_sha": "self-asserted-policy"}, {"coverage": None},
    {"checks": {"lint": {"status": "passed", "findings": ["actually broken"]}}},
    {"schema_version": True}, {"unexpected": "metadata"},
    {"handler_inventory": [None]}, {"handler_inventory": {}},
    {"coverage": {"total": 1, "blocking": ["src/a.py"], "uncovered": ["src/a.py"]}},
    {"coverage": {"total": True, "blocking": ["src/a.py"], "uncovered": []}},
    {"coverage": {"total": 1, "blocking": ["src/../a.py"], "uncovered": []}},
    {"checks": {"lint": {"status": "passed", "findings": [], "duration_seconds": float("nan")}}},
    {"checks": {"lint": {"status": "passed", "findings": [], "duration_seconds": True}}},
])
def test_aggregate_rejects_resealed_invalid_metadata(metadata):
    expected = report_context()
    report = QUALITY["seal_report"]({**successful_report("lint", expected), **metadata})
    assert not QUALITY["report_valid"](report, "a" * 40, "b" * 40, {"lint"}, expected)


@pytest.mark.parametrize("check,details", [
    ("typing", {"outside_scope_diagnostics": [{}]}),
    ("typing", {"diagnostics": [{}]}),
    ("typing", {"baseline_entries": -1}),
    ("architecture", {"grimp_modules": 2}),
    ("architecture", {"edges": False}),
    ("exceptions", {"exception_ids_used": ["duplicated", "duplicated"]}),
])
def test_aggregate_rejects_malformed_check_details(check, details):
    expected = report_context()
    report = successful_report(check, expected)
    report["checks"][check].update(details)
    assert not QUALITY["report_valid"](
        QUALITY["seal_report"](report), "a" * 40, "b" * 40, {check}, expected)


def test_failed_tools_and_invalid_structured_output_fail_closed():
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["tool_json"](
            subprocess.CompletedProcess(["ruff"], 2, "[]", "tool failed"), "ruff"
        )
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["tool_json"](
            subprocess.CompletedProcess(["ruff"], 0, "not json", ""), "ruff"
        )
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["tool_json"](
            subprocess.CompletedProcess(["ruff"], 1, "[]", ""), "ruff"
        )


def test_tool_timeout_is_an_error():
    with pytest.raises(QUALITY["QualityError"], match="timed out"):
        QUALITY["run"]([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.01)


def test_invalid_cli_writes_error_report(tmp_path):
    output = tmp_path / "quality.json"
    proc = subprocess.run(
        [sys.executable, "-I", "-S", QUALITY["__file__"], "--base-ref", "not-a-ref",
         "--check", "policy", "--report", str(output)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert json.loads(output.read_text())["checks"]["policy"]["status"] == "error"


def test_real_ruff_and_mypy_reject_regressions(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("value: int = 'wrong'\nprint(missing)\n")
    lint = QUALITY["tool_json"](QUALITY["run"]([
        sys.executable, "-m", "ruff", "check", "--isolated", "--select", "F821",
        "--output-format", "json", str(source),
    ]), "ruff")
    assert [(item["code"], item["location"]["row"]) for item in lint] == [("F821", 2)]
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\npython_version = 3.12\n")
    typing = QUALITY["tool_json"](QUALITY["run"]([
        sys.executable, "-m", "mypy", "--config-file", str(config),
        "--no-incremental", "--output", "json", str(source),
    ]), "mypy")
    assert {item["code"] for item in typing if item["severity"] == "error"} == {
        "assignment", "name-defined",
    }


def test_grimp_overlap_never_imports_package_initializers(tmp_path):
    modules = tree(tmp_path, {
        "pkg/__init__.py": "raise RuntimeError('source is not executable')",
        "pkg/a.py": "from . import b",
        "pkg/b.py": "value = 1",
        "flat.py": "import pkg.a",
    })
    edges, findings = QUALITY["architecture"](modules, rules())
    assert not findings
    assert QUALITY["grimp_check"](tmp_path, modules, edges) == 3


@pytest.fixture
def policy_repo(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
    git("init", "--quiet")
    git("config", "user.name", "Quality fixture")
    git("config", "user.email", "quality@example.invalid")
    records = {
        "typing-scope.json": {"module_ids": ["a", "b"], "schema_version": 1},
        "typing-baseline.json": {"entries": [{"id": "old"}], "schema_version": 1},
    }
    for name, data in records.items():
        path = tmp_path / ".quality" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data))
    for name in (".github/scripts/check-quality.py", ".github/CODEOWNERS",
                 ".github/workflows/pr_pipeline.yaml", "tests/test_quality_policy.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# protected\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "fixture")
    return tmp_path, git("rev-parse", "HEAD"), records


@pytest.mark.parametrize("path,text", [
    (".github/scripts/check-quality.py", "print('passed')"),
    (".github/workflows/pr_pipeline.yaml", "jobs: {}\n"),
    ("tests/test_quality_policy.py", "# no quality tests\n"),
    (".github/CODEOWNERS", "# no owners"),
    (".quality/typing-scope.json", '{"module_ids": ["a"], "schema_version": 1}'),
    (".quality/typing-baseline.json",
     '{"entries": [{"id": "old"}, {"id": "new"}], "schema_version": 1}'),
    (".quality/module-surfaces.json", '{"entries": [], "schema_version": 1}'),
])
def test_candidate_cannot_bless_checker_baseline_owner_or_scope_weakening(policy_repo, path, text):
    root, base, records = policy_repo
    (root / path).write_text(text)
    candidate = json.loads(json.dumps(records))
    if path.startswith(".quality"):
        candidate[Path(path).name] = json.loads(text)
    findings = QUALITY["policy_changes"](root, base, records, candidate)
    assert [item["rule"] for item in findings] == ["protected-policy"]


def test_monotonic_scope_expansion_and_debt_retirement_are_allowed(policy_repo):
    root, base, records = policy_repo
    candidate = json.loads(json.dumps(records))
    candidate["typing-scope.json"]["module_ids"].append("new")
    candidate["typing-baseline.json"]["entries"] = []
    for name, value in candidate.items():
        (root / ".quality" / name).write_text(json.dumps(value))
    assert not QUALITY["policy_changes"](root, base, records, candidate)


def test_untracked_policy_file_cannot_avoid_integrity_guard(policy_repo):
    root, base, records = policy_repo
    path = root / ".github/scripts/new-policy.py"
    path.write_text("# candidate-owned evaluator\n")
    findings = QUALITY["policy_changes"](root, base, records, records)
    assert [f["path"] for f in findings] == [".github/scripts/new-policy.py"]


@pytest.mark.parametrize("path", [
    ".github/workflows/issue-type-classifier.lock.yml",
    ".github/workflows/validate-agentic-assets.yml",
    ".github/workflows/other-automation.yml",
])
def test_non_quality_workflows_do_not_trigger_protected_policy_veto(policy_repo, path):
    root, base, records = policy_repo
    (root / path).write_text("jobs: {}\n")
    assert not QUALITY["policy_changes"](root, base, records, records)


@pytest.mark.parametrize("statement", [
    "from pkg.public import missing",
    "from pkg import missing",
    "from pkg.public import missing as alias",
])
def test_unresolved_first_party_export_is_not_a_valid_edge(tmp_path, statement):
    _, findings = graph(tmp_path, {
        "consumer.py": statement, "pkg/__init__.py": "", "pkg/public.py": "value = 1",
    })
    assert any(f["rule"] == "unresolved-import" for f in findings)


def test_real_symbol_reexport_remains_public(tmp_path):
    _, findings = graph(tmp_path, {
        "consumer.py": "from pkg import exported",
        "pkg/__init__.py": "from .public import value as exported",
        "pkg/public.py": "value = 1",
    })
    assert not findings


def test_dynamic_import_inventory_requires_exact_site_targets_and_evidence(tmp_path):
    code = "from importlib import import_module as load\nload(module_name)"
    modules = tree(tmp_path, {"loader.py": code, "target.py": "value = 1"})
    site = next(n for n in ast.walk(modules["loader"]["tree"]) if isinstance(n, ast.Call))
    record = {
        "id": "loader-target", "module_id": "loader",
        "symbol": "<module>",
        "source_fingerprint": QUALITY["syntax"](site), "targets": ["target"],
        "evidence_tests": ["tests/test_loader.py::test_target"], "review": "base review",
    }
    edges, findings = QUALITY["architecture"](
        modules, rules(dynamic_imports=[record]), {"tests/test_loader.py::test_target"})
    assert not findings
    assert edges["loader"] == {"target"}
    assert QUALITY["architecture"](modules, rules(dynamic_imports=[record]))[1]
    assert QUALITY["architecture"](
        tree(tmp_path, {"loader.py": code.replace("module_name", "different_name")}),
        rules(dynamic_imports=[record]), {"tests/test_loader.py::test_target"})[1]


def test_unused_dynamic_import_approval_fails(tmp_path):
    modules = tree(tmp_path, {"a.py": "value = 1"})
    record = {
        "id": "unused", "module_id": "a", "source_fingerprint": "old-site",
        "symbol": "<module>",
        "targets": ["a"], "evidence_tests": ["tests/test_loader.py::test_target"],
        "review": "base review",
    }
    assert QUALITY["architecture"](
        modules, rules(dynamic_imports=[record]), {"tests/test_loader.py::test_target"})[1]


def test_dynamic_site_cannot_duplicate_an_approved_allowance(tmp_path):
    modules = tree(tmp_path, {
        "loader.py": "import importlib\nimportlib.import_module(name)\nimportlib.import_module(name)",
        "target.py": "",
    })
    call = next(n for n in ast.walk(modules["loader"]["tree"]) if isinstance(n, ast.Call))
    record = {
        "id": "once", "module_id": "loader", "symbol": "<module>",
        "source_fingerprint": QUALITY["syntax"](call), "targets": ["target"],
        "evidence_tests": ["tests/test_loader.py::test_target"], "review": "base review",
    }
    assert QUALITY["architecture"](
        modules, rules(dynamic_imports=[record]), {"tests/test_loader.py::test_target"})[1]


@pytest.mark.parametrize("statement", [
    "import importlib\nload = getattr(importlib, 'import_module')\nload(name)",
    "from importlib import import_module\nload = import_module\nload(name)",
])
def test_indirect_loader_alias_needs_review(tmp_path, statement):
    _, findings = graph(tmp_path, {"loader.py": statement})
    assert any(f["rule"] == "dynamic-import" for f in findings)


def test_relative_literal_dynamic_import_resolves_without_running_code(tmp_path):
    edges, findings = graph(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/loader.py": "import importlib\nimportlib.import_module('.target', 'pkg')",
        "pkg/target.py": "raise RuntimeError('never execute source')",
    })
    assert not findings
    assert edges["pkg.loader"] == {"pkg.target"}


@pytest.mark.parametrize("expression", [
    "choose_errors()", "(ValueError, Exception)", "Exception if condition else ValueError",
])
def test_computed_exception_alias_is_reviewed(tmp_path, expression):
    modules = tree(tmp_path, {
        "a.py": f"E = {expression}\ntry:\n work()\nexcept E:\n pass",
    })
    assert QUALITY["handlers"](modules)[0]["caught_types"] == ["<indirect>"]


def test_computed_import_machinery_access_cannot_bypass_inventory(tmp_path):
    _, findings = graph(tmp_path, {
        "a.py": "import importlib\ngetattr(importlib, method_name)(module_name)",
    })
    assert any(f["rule"] == "dynamic-import" for f in findings)


def test_conflicting_local_alias_cannot_hide_broad_global_catch(tmp_path):
    modules = tree(tmp_path, {
        "a.py": "from builtins import Exception as E\n"
                "def helper():\n from builtins import ValueError as E\n"
                "try:\n work()\nexcept E:\n pass",
    })
    with pytest.raises(QUALITY["QualityError"], match="Ambiguous import alias"):
        QUALITY["handlers"](modules)


def test_conflicting_local_module_alias_cannot_hide_private_global_access(tmp_path):
    modules = tree(tmp_path, {
        "a.py": "import pkg as p\n"
                "def helper():\n import other as p\n"
                "value = p._private\n",
        "pkg.py": "_private = 1", "other.py": "value = 1",
    })
    with pytest.raises(QUALITY["QualityError"], match="Ambiguous import alias"):
        QUALITY["architecture"](modules, rules())


def test_conflicting_local_loader_alias_cannot_hide_dynamic_global_import(tmp_path):
    modules = tree(tmp_path, {
        "a.py": "from importlib import import_module as load\n"
                "def helper():\n from json import loads as load\n"
                "load(module_name)\n",
    })
    with pytest.raises(QUALITY["QualityError"], match="Ambiguous import alias"):
        QUALITY["architecture"](modules, rules())


@pytest.mark.parametrize("source", [
    "try:\n work()\nexcept Exception:\n pass\n"
    "def unrelated():\n from builtins import ValueError as Exception\n",
    "__import__(name)\n"
    "def unrelated():\n from json import loads as __import__\n",
])
def test_local_alias_cannot_shadow_implicit_global_builtin(tmp_path, source):
    modules = tree(tmp_path, {"a.py": source})
    with pytest.raises(QUALITY["QualityError"], match="Ambiguous import alias"):
        QUALITY["handlers"](modules)
    with pytest.raises(QUALITY["QualityError"], match="Ambiguous import alias"):
        QUALITY["architecture"](modules, rules())


def test_inherited_typing_suppression_cannot_move_to_another_function(tmp_path):
    before = tree(tmp_path / "old", {
        "a.py": "from typing import no_type_check as ignore\n"
                "@ignore\ndef old() -> int:\n return 'legacy'\n"
                "def fresh() -> int:\n return 1\n",
    })
    after = tree(tmp_path / "new", {
        "a.py": "from typing import no_type_check as ignore\n"
                "def old() -> int:\n return 'legacy'\n"
                "@ignore\ndef fresh() -> int:\n return 'wrong'\n",
    })
    findings = QUALITY["source_policy"](before, after, {"a": "a"}, {"a"})
    assert any(f["rule"] == "suppression" for f in findings)


def test_dynamic_record_cannot_move_between_functions(tmp_path):
    modules = tree(tmp_path, {
        "loader.py": "from importlib import import_module\n"
                     "def original(name):\n return import_module(name)\n"
                     "def copied(name):\n return import_module(name)\n",
        "target.py": "",
    })
    call = next(n for n in ast.walk(modules["loader"]["tree"]) if isinstance(n, ast.Call))
    record = {
        "id": "original", "module_id": "loader", "symbol": "original",
        "source_fingerprint": QUALITY["syntax"](call), "targets": ["target"],
        "evidence_tests": ["tests/test_loader.py::test_target"], "review": "base review",
    }
    _, findings = QUALITY["architecture"](
        modules, rules(dynamic_imports=[record]), {"tests/test_loader.py::test_target"})
    assert [(f["rule"], f["line"]) for f in findings] == [("dynamic-import", 5)]


def test_local_variables_are_not_public_module_exports(tmp_path):
    _, findings = graph(tmp_path, {
        "consumer.py": "from pkg import local",
        "pkg.py": "def f():\n local = 1\n return local",
    })
    assert any(f["rule"] == "unresolved-import" for f in findings)


def test_adopted_audit_records_bind_source_and_proposals_still_fail():
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    records = QUALITY["load_records"](root)["exceptions.json"]["entries"]
    records = [r for r in records if r["module_id"].startswith("telemetry.audit")]
    current = QUALITY["handlers"](QUALITY["collect"](root, ["src"]))
    current = [h for h in current if h["module_id"] in (
        "telemetry.audit", "telemetry.audit_contract", "telemetry.audit_sanitizer")]
    assert len(current) == len(records) == 8
    passed = {test for record in records for test in record["evidence_tests"]}
    proposed_fixture = [{**record, "status": "proposed"} for record in records]
    assert QUALITY["check_exceptions"](current, proposed_fixture, passed)
    reviewed_fixture = [{**record, "status": "active"} for record in records]
    assert not QUALITY["check_exceptions"](current, reviewed_fixture, passed)
    assert QUALITY["check_exceptions"](current, reviewed_fixture, set())


@pytest.mark.parametrize("record_id,outcome", [
    ("appconfig-provider-availability-translation", "failure-translation"),
    ("search-provider-keyword-fallback", "failure-translation"),
    ("search-provider-empty-context-translation", "failure-translation"),
    ("search-connector-strict-anonymous-failure-contract", "failure-translation"),
    ("hosted-turn-error-before-propagation", "propagation"),
    ("hosted-sse-safe-terminal-error-translation", "failure-translation"),
    ("mcp-chat-cleanup-preserves-primary-outcome", "propagation"),
    ("conversation-list-http-failure-translation", "failure-translation"),
    ("conversation-read-http-failure-translation", "failure-translation"),
    ("conversation-rename-http-failure-translation", "failure-translation"),
    ("conversation-delete-http-failure-translation", "failure-translation"),
    ("jwt-verification-fail-closed-http-translation", "failure-translation"),
    ("maf-lite-optional-profile-load", "failure-translation"),
    ("maf-lite-optional-profile-save", "contractual-best-effort-side-effect"),
    ("maf-service-optional-profile-load", "failure-translation"),
    ("maf-service-optional-profile-save", "contractual-best-effort-side-effect"),
    ("multimodal-profile-load-compatibility", "failure-translation"),
    ("multimodal-profile-save-compatibility", "contractual-best-effort-side-effect"),
    ("citation-storage-config-unsigned-compatibility", "contractual-best-effort-side-effect"),
    ("citation-signing-unsigned-compatibility", "contractual-best-effort-side-effect"),
    ("nl2sql-table-list-result-translation", "failure-translation"),
    ("nl2sql-schema-unavailable-compatibility", "failure-translation"),
    ("nl2sql-table-retrieval-result-translation", "failure-translation"),
    ("nl2sql-measure-list-result-translation", "failure-translation"),
    ("nl2sql-query-retrieval-result-translation", "failure-translation"),
    ("nl2sql-dax-result-translation", "failure-translation"),
    ("nl2sql-sql-result-translation", "failure-translation"),
    ("nl2sql-sql-cleanup-preserves-primary-outcome", "contractual-best-effort-side-effect"),
    ("legacy-vector-tool-result-translation", "failure-translation"),
    ("legacy-multimodal-embedding-result-translation", "failure-translation"),
    ("legacy-multimodal-credential-result-translation", "failure-translation"),
    ("legacy-multimodal-search-result-translation", "failure-translation"),
    ("dashboard-admin-auth-translation", "failure-translation"),
    ("dashboard-per-setting-write-isolation", "failure-translation"),
    ("startup-optional-token-prefetch", "contractual-best-effort-side-effect"),
    ("startup-optional-agent-prewarm", "contractual-best-effort-side-effect"),
    ("startup-optional-provider-prewarm", "contractual-best-effort-side-effect"),
    ("request-debug-rendering-side-effect", "contractual-best-effort-side-effect"),
    ("orchestrator-auth-failure-translation", "failure-translation"),
    ("request-context-logging-side-effect", "contractual-best-effort-side-effect"),
    ("history-auth-failure-translation", "failure-translation"),
    ("search-initialization-term-compatibility", "failure-translation"),
    ("search-index-probe-unknown-is-not-empty", "failure-translation"),
    ("search-foundry-strict-anonymous-mcp-contract", "failure-translation"),
    ("search-filepath-nullable-compatibility", "failure-translation"),
    ("optional-log-level-diagnostic-reporting", "contractual-best-effort-side-effect"),
    ("context-noise-filter-retains-unformattable-record", "contractual-best-effort-side-effect"),
    ("legacy-api-key-environment-fallback", "failure-translation"),
    ("optional-jwks-cache-refresh-diagnostics", "contractual-best-effort-side-effect"),
    ("optional-jwt-segment-length-diagnostics", "contractual-best-effort-side-effect"),
    ("optional-jwt-base64-diagnostics", "contractual-best-effort-side-effect"),
    ("optional-unverified-claim-diagnostics", "contractual-best-effort-side-effect"),
    ("optional-graph-audience-preverification-hint", "contractual-best-effort-side-effect"),
    ("optional-graph-audience-signature-hint", "contractual-best-effort-side-effect"),
    ("legacy-optional-graph-group-result", "failure-translation"),
    ("foundry-context-empty-result-translation", "failure-translation"),
    ("multimodal-context-keyword-fallback", "failure-translation"),
    ("legacy-multimodal-retry-without-obo", "failure-translation"),
    ("multimodal-optional-image-classification", "failure-translation"),
    ("multimodal-optional-image-download", "failure-translation"),
    ("foundry-service-token-failure", "failure-translation"),
    ("foundry-mcp-query-credential-failure", "failure-translation"),
    ("foundry-retrieve-request-failure", "failure-translation"),
    ("legacy-maf-service-nullable-provider", "failure-translation"),
    ("legacy-maf-lite-nullable-provider", "failure-translation"),
    ("maf-lite-intent-question-fallback", "failure-translation"),
    ("legacy-multimodal-nullable-provider", "failure-translation"),
    ("multimodal-image-validation-strip", "failure-translation"),
    ("multimodal-intent-question-fallback", "failure-translation"),
    ("optional-conversation-lifecycle-diagnostic", "contractual-best-effort-side-effect"),
    ("legacy-detached-conversation-persistence", "failure-translation"),
    ("legacy-conversation-persistence-scheduling-cleanup", "failure-translation"),
    ("legacy-conversation-persistence-cleanup-diagnostic", "contractual-best-effort-side-effect"),
    ("optional-feedback-question-resolution", "failure-translation"),
    ("agent-provider-pre-output-option-retry", "failure-translation"),
    ("managed-turn-ambiguous-write-reconciliation", "failure-translation"),
    ("managed-tail-lookup-unconfirmed", "failure-translation"),
    ("optional-reusable-agent-prewarm", "contractual-best-effort-side-effect"),
    ("legacy-single-search-context-continuation", "propagation"),
    ("single-direct-stream-contextual-propagation", "propagation"),
    ("single-agent-stream-contextual-propagation", "propagation"),
])
def test_adopted_compatibility_record_is_exact_and_proposals_still_fail(record_id, outcome):
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    records = QUALITY["load_records"](root)["exceptions.json"]["entries"]
    record = next(r for r in records if r["id"] == record_id)
    current = [h for h in QUALITY["handlers"](QUALITY["collect"](root, ["src"]))
               if h["module_id"] == record["module_id"]
               and h["symbol"] == record["symbol"]
               and h["handler_fingerprint"] == record["handler_fingerprint"]]
    assert len(current) == 1
    assert record["status"] == "active"
    assert record["failure_outcome"] == outcome
    passed = set(record["evidence_tests"])
    assert QUALITY["check_exceptions"](current, [{**record, "status": "proposed"}], passed)
    reviewed_fixture = [{**record, "status": "active"}]
    assert not QUALITY["check_exceptions"](current, reviewed_fixture, passed)
    assert QUALITY["check_exceptions"](current, reviewed_fixture, set())


def test_adopted_non_audit_turn_record_still_requires_approval_and_evidence():
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    records = QUALITY["load_records"](root)["exceptions.json"]["entries"]
    record = next(r for r in records if r["id"] == "turn-error-event-before-propagation")
    current = [h for h in QUALITY["handlers"](QUALITY["collect"](root, ["src"]))
               if h["module_id"] == record["module_id"] and h["symbol"] == record["symbol"]]
    assert len(current) == 1
    assert record["failure_outcome"] == "propagation"
    assert record["status"] == "active"
    passed = set(record["evidence_tests"])
    assert QUALITY["check_exceptions"](current, [{**record, "status": "proposed"}], passed)
    assert not QUALITY["check_exceptions"](current, [{**record, "status": "active"}], passed)
    assert QUALITY["check_exceptions"](current, [{**record, "status": "active"}], set())


def test_initial_adoption_activates_only_existing_records_and_preserves_retirements():
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    records = QUALITY["load_records"](root)["exceptions.json"]["entries"]
    retired = {
        "legacy-orchestrator-token-setter", "legacy-text-provider-obo-service-fallback",
        "foundry-context-obo-compatibility", "legacy-multimodal-obo-service-fallback",
        "multimodal-retry-empty-context", "optional-profile-background-extraction",
        "optional-profile-pending-task-flush", "maf-lite-optional-profile-cleanup",
        "multimodal-optional-profile-cleanup",
    }
    assert not retired.intersection(record["id"] for record in records)
    assert len(records) == 92
    approval = "https://github.com/Azure/GPT-RAG/issues/681#issuecomment-5601804634"
    assert all(record["status"] == "active" for record in records)
    assert all(approval in record["review"] for record in records)
    policy = QUALITY["load_records"](root)["policy.json"]
    assert policy["review"]["status"] == "active"
    assert policy["review"]["reference"] == approval
    current = QUALITY["handlers"](QUALITY["collect"](root, ["src"]))
    key = lambda item: (item["module_id"], item["symbol"], item["handler_fingerprint"])
    assert {key(record) for record in records} == {key(handler) for handler in current}


@pytest.mark.parametrize("file,mutate", [
    ("policy.json", lambda data: data.update(extra_setting=True)),
    ("policy.json", lambda data: data["toolchain"].update(ruff=">=0.16")),
    ("policy.json", lambda data: data["contracts"]["private_access"].append(
        {"importer": "*", "target": "anything"})),
    ("typing-scope.json", lambda data: data.update(module_ids="schemas")),
    ("typing-scope.json", lambda data: data.update(move_map=[{"old": "new"}])),
    ("exceptions.json", lambda data: data["entries"][0].update(caught_types="Exception")),
    ("exceptions.json", lambda data: data["entries"][0].update(evidence_tests=[])),
    ("exceptions.json", lambda data: data["entries"][0].update(failure_outcome="ignore everything")),
    ("policy.json", lambda data: data["contracts"]["dynamic_imports"].append({
        "id": "empty-targets", "module_id": "loader", "symbol": "load",
        "source_fingerprint": "a" * 64, "targets": [],
        "evidence_tests": ["tests/test_loader.py::test_target"], "review": "proposal",
    })),
    ("policy.json", lambda data: data["contracts"]["dynamic_imports"].append({
        "id": "empty-evidence", "module_id": "loader", "symbol": "load",
        "source_fingerprint": "a" * 64, "targets": ["target"],
        "evidence_tests": [], "review": "proposal",
    })),
])
def test_policy_documents_are_strictly_validated(tmp_path, file, mutate):
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    (tmp_path / ".quality").mkdir()
    for name in QUALITY["RECORDS"]:
        data = json.loads((root / ".quality" / name).read_text())
        if name == file:
            mutate(data)
        (tmp_path / ".quality" / name).write_text(json.dumps(data))
    (tmp_path / "requirements-quality.txt").write_text(
        (root / "requirements-quality.txt").read_text())
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["load_records"](tmp_path)


def test_evidence_does_not_credit_skipped_failed_or_missing_tests(tmp_path):
    path = tmp_path / "pytest.xml"
    path.write_text(
        '<testsuite><testcase classname="tests.test_failure" name="test_pass"/>'
        '<testcase classname="tests.test_failure" name="test_skip"><skipped/></testcase>'
        '<testcase classname="tests.test_failure" name="test_fail"><failure/></testcase></testsuite>'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_failure.py").write_text("")
    assert QUALITY["evidence_tests"](path, tmp_path) == {"tests/test_failure.py::test_pass"}


def test_junit_class_evidence_resolves_actual_test_module(tmp_path):
    path = tmp_path / "pytest.xml"
    path.write_text(
        '<testsuite><testcase classname="tests.test_quality_policy.TestBoundary" '
        'name="test_failure[case]"/></testsuite>')
    assert QUALITY["evidence_tests"](path) == {
        "tests/test_quality_policy.py::TestBoundary::test_failure[case]"}


@pytest.mark.parametrize("cases", [
    '<testcase classname="tests.test_missing_module" name="test_not_executed"/>',
    '<testcase classname="tests.test_quality_policy" name="test_duplicate"/>'
    '<testcase classname="tests.test_quality_policy" name="test_duplicate"><failure/></testcase>',
    '<testcase classname="tests.test_quality_policy" name="test_duplicate"/>'
    '<testcase classname="tests.test_quality_policy" name="test_duplicate"/>',
])
def test_junit_unknown_or_duplicate_selector_is_not_passing_evidence(tmp_path, cases):
    path = tmp_path / "pytest.xml"
    path.write_text(f"<testsuite>{cases}</testsuite>")
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["evidence_tests"](path)


@pytest.fixture(scope="module")
def protected_aggregate(tmp_path_factory):
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    protected = tmp_path_factory.mktemp("protected-aggregate")
    for path in (
        ".github/scripts/check-quality.py", ".github/scripts/aggregate-quality.py",
        *[f".quality/{name}" for name in QUALITY["RECORDS"]], "requirements-quality.txt",
    ):
        target = protected / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((root / path).read_text(encoding="utf-8"), encoding="utf-8")
    for args in (
        ["init", "--quiet"], ["config", "user.name", "Quality fixture"],
        ["config", "user.email", "quality@example.invalid"],
        ["add", "."], ["commit", "--quiet", "-m", "protected evaluator"],
    ):
        subprocess.run(["git", *args], cwd=protected, capture_output=True, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=protected,
                          capture_output=True, text=True, check=True).stdout.strip()
    return protected, base


@pytest.mark.parametrize("mutation", [
    None, "missing", "stale", "tampered", "skipped", "policy", "all-policy",
    "toolchain", "run_id", "run_attempt", "repository", "schema", "bootstrap",
    "inventory", "duplicate", "wrong-base-checkout",
])
def test_actual_aggregate_fails_closed(tmp_path, protected_aggregate, mutation):
    protected, base = protected_aggregate
    expected = report_context()
    for check in QUALITY["CHECKS"]:
        report = successful_report(check, expected, base)
        if mutation == "all-policy":
            report["policy_sha"] = "c" * 64
        if check == "lint":
            if mutation == "missing":
                continue
            if mutation == "stale":
                report["head_sha"] = "d" * 40
            if mutation == "policy":
                report["policy_sha"] = "c" * 64
            if mutation in ("run_id", "run_attempt"):
                report[mutation] = "999"
            if mutation == "toolchain":
                report["toolchain"] = {**report["toolchain"], "mypy": "1.0.0"}
            if mutation == "repository":
                report["repository"] = "other/repository"
            if mutation == "schema":
                report["schema_version"] = 999
            if mutation == "bootstrap":
                report["bootstrap"] = True
            if mutation == "inventory":
                report["coverage"]["blocking"] = ["src/different.py"]
        report = QUALITY["seal_report"](report)
        if mutation == "tampered" and check == "lint":
            report["head_sha"] = "edited"
        if mutation == "duplicate" and check == "lint":
            (tmp_path / "duplicate").mkdir()
            (tmp_path / "duplicate" / "quality-lint.json").write_text(json.dumps(report))
        (tmp_path / f"quality-{check}.json").write_text(json.dumps(report))
    needs = {name: {"result": "success"} for name in ("tests", "frontend", "quality")}
    if mutation == "skipped":
        needs["tests"]["result"] = "skipped"
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(protected / ".github" / "scripts" / "aggregate-quality.py"),
         "--reports", str(tmp_path),
         "--base-sha", "e" * 40 if mutation == "wrong-base-checkout" else base,
         "--head-sha", "b" * 40, "--repository", QUALITY["REPOSITORY"],
         "--run-id", "1234", "--run-attempt", "1",
         "--needs", json.dumps(needs)],
        capture_output=True, text=True,
    )
    assert (proc.returncode == 0) == (mutation is None), proc.stdout + proc.stderr


def mypy_diagnostic(**overrides):
    return {
        "file": "src/a.py", "line": 1, "column": 0, "end_line": 1, "end_column": 1,
        "message": "Incompatible return type", "hint": None,
        "code": "return-value", "severity": "error", **overrides,
    }


@pytest.mark.parametrize("value", [
    {},
    {k: v for k, v in mypy_diagnostic().items() if k != "severity"},
    mypy_diagnostic(severity="unknown"),
    mypy_diagnostic(line=True),
    mypy_diagnostic(file=None),
    mypy_diagnostic(message=[]),
    mypy_diagnostic(extra="unexpected"),
])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_mypy_malformed_diagnostics_are_errors_not_ignored(value, exit_code):
    result = subprocess.CompletedProcess(["mypy"], exit_code, json.dumps(value), "")
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["tool_json"](result, "mypy")


def test_mypy_valid_notes_and_errors_reconcile_exit_status():
    note = mypy_diagnostic(severity="note")
    error = mypy_diagnostic()
    assert QUALITY["tool_json"](
        subprocess.CompletedProcess(["mypy"], 0, json.dumps(note), ""), "mypy") == [note]
    assert QUALITY["tool_json"](
        subprocess.CompletedProcess(["mypy"], 1, json.dumps(error), ""), "mypy") == [error]
    for code, diagnostic in ((0, error), (1, note)):
        with pytest.raises(QUALITY["QualityError"]):
            QUALITY["tool_json"](
                subprocess.CompletedProcess(["mypy"], code, json.dumps(diagnostic), ""), "mypy")


def test_new_module_stays_covered_across_successive_pr_bases_and_move(tmp_path):
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    records = QUALITY["load_records"](root)
    records["policy.json"]["modules"] = ["seed", "legacy"]
    records["typing-scope.json"]["module_ids"] = ["seed"]
    records["module-surfaces.json"]["entries"] = [
        surface_record(), surface_record("legacy", typing_status="inventoried")]
    records["exceptions.json"]["entries"] = []
    (tmp_path / ".quality").mkdir()
    for name, value in records.items():
        (tmp_path / ".quality" / name).write_text(json.dumps(value))
    (tmp_path / "requirements-quality.txt").write_text(
        (root / "requirements-quality.txt").read_text())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\ncheck_untyped_defs = true\n')
    tree(tmp_path, {"seed.py": "value: int = 1", "legacy.py": "value = 1"})

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True,
                              text=True, check=True).stdout.strip()

    def check(base):
        report_path = tmp_path / ".artifacts" / "quality.json"
        proc = subprocess.run(
            [sys.executable, "-I", "-S", QUALITY["__file__"], "--root", str(tmp_path),
             "--base-ref", base, "--check", "typing", "--report", str(report_path)],
            capture_output=True, text=True,
        )
        return proc.returncode, json.loads(report_path.read_text())

    git("init", "--quiet")
    git("config", "user.name", "Quality fixture")
    git("config", "user.email", "quality@example.invalid")
    git("add", ".")
    git("commit", "--quiet", "-m", "protected inventory")
    initial = git("rev-parse", "HEAD")
    (tmp_path / "src" / "new.py").write_text("def f() -> int:\n return 1\n")
    code, report = check(initial)
    assert code == 0, report
    assert report["coverage"]["blocking"] == ["src/new.py", "src/seed.py"]
    git("add", "src")
    git("commit", "--quiet", "-m", "first PR adds covered module")
    first_pr = git("rev-parse", "HEAD")
    (tmp_path / "src" / "new.py").write_text("def f() -> int:\n return 'wrong'\n")
    code, report = check(first_pr)
    assert code == 1, report
    assert report["coverage"]["blocking"] == ["src/new.py", "src/seed.py"]
    assert report["checks"]["typing"]["findings"][0]["rule"] == "new-type-debt"

    # A mechanically unchanged move cannot turn inherited new code into legacy debt.
    git("add", "src")
    git("commit", "--quiet", "-m", "fixture for move with known type error")
    second_pr = git("rev-parse", "HEAD")
    (tmp_path / "src" / "new.py").rename(tmp_path / "src" / "moved.py")
    code, report = check(second_pr)
    assert code == 1, report
    assert report["coverage"]["blocking"] == ["src/moved.py", "src/seed.py"]


@pytest.fixture
def isolated_policy_repo(tmp_path):
    root = Path(QUALITY["__file__"]).resolve().parents[2]
    records = QUALITY["load_records"](root)
    records["policy.json"]["modules"] = ["seed", "pkg", "pkg.child"]
    records["typing-scope.json"]["module_ids"] = ["seed"]
    records["module-surfaces.json"]["entries"] = [
        surface_record(public_exports=["f"]),
        surface_record("pkg", path="src/pkg/__init__.py", typing_status="inventoried"),
        surface_record("pkg.child", typing_status="inventoried", allowed_importers=["pkg"]),
    ]
    records["exceptions.json"]["entries"] = []
    (tmp_path / ".quality").mkdir()
    for name, value in records.items():
        (tmp_path / ".quality" / name).write_text(json.dumps(value))
    for name in ("requirements-quality.txt",):
        (tmp_path / name).write_text((root / name).read_text())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["F821"]\n'
        '[tool.mypy]\npython_version = "3.12"\nmypy_path = "src"\n'
        'explicit_package_bases = true\ncheck_untyped_defs = true\n')
    (tmp_path / ".importlinter").write_text("[importlinter]\nroot_package = pkg\n")
    tree(tmp_path, {"seed.py": "def f() -> int:\n return 1\n",
                    "pkg/__init__.py": "from .child import value\n", "pkg/child.py": "value = 1\n"})
    for args in (("init", "--quiet"), ("config", "user.name", "Quality fixture"),
                 ("config", "user.email", "quality@example.invalid"),
                 ("add", "."), ("commit", "--quiet", "-m", "protected source")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def isolated_check(root, check, *, env=None):
    report = root / ".artifacts" / "isolation.json"
    proc = subprocess.run(
        [sys.executable, "-I", "-S", QUALITY["__file__"], "--root", str(root),
         "--base-ref", "HEAD", "--check", check, "--report", str(report)],
        cwd=root, env=env, capture_output=True, text=True)
    return proc, json.loads(report.read_text())


@pytest.mark.parametrize("check", ["lint", "typing", "architecture"])
def test_static_tools_never_execute_candidate_shadows_or_startup_hooks(
    isolated_policy_repo, check, tmp_path,
):
    root = isolated_policy_repo
    marker = root / "EXECUTED"
    trap = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('candidate executed')\n"
    for name in ("ruff", "mypy", "grimp", "importlinter", "sitecustomize", "usercustomize"):
        (root / f"{name}.py").write_text(trap)
        (root / "src" / f"{name}.py").write_text(trap)
    # Importable packages must be discovered as data, including raising initializers.
    (root / "src" / "pkg" / "__init__.py").write_text(trap)
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "src"),
           "MYPYPATH": str(root), "PYTHONSTARTUP": str(root / "sitecustomize.py"),
           "RUFF_CACHE_DIR": str(root / "poison-cache"), "MYPY_CACHE_DIR": str(root / "poison-cache")}
    proc, report = isolated_check(root, check, env=env)
    assert not marker.exists(), (proc.stdout, proc.stderr)
    assert proc.returncode == 0, (proc.stdout, proc.stderr, report)
    assert report["checks"][check]["status"] == "passed"
    assert not (root / "poison-cache").exists()
    assert not (root / ".mypy_cache").exists()


@pytest.mark.parametrize("option", ["plugins", "python_executable"])
def test_executable_mypy_config_is_rejected_before_loading(
    isolated_policy_repo, option,
):
    root = isolated_policy_repo
    marker = root / "EXECUTED"
    (root / "plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    with (root / "pyproject.toml").open("a") as stream:
        stream.write(f'{option} = "plugin.py"\n')
    # Bootstrap/protected configuration must not authorize source execution either.
    subprocess.run(["git", "add", "pyproject.toml"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "unsafe tool config"],
                   cwd=root, check=True, capture_output=True)
    proc, report = isolated_check(root, "typing")
    assert not marker.exists()
    assert proc.returncode == 2, report
    assert "configuration" in report["checks"]["typing"]["message"].lower()


def test_unisolated_cli_cannot_report_success(isolated_policy_repo):
    root = isolated_policy_repo
    report = root / "unsafe.json"
    proc = subprocess.run(
        [sys.executable, QUALITY["__file__"], "--root", str(root),
         "--base-ref", "HEAD", "--check", "lint", "--report", str(report)],
        cwd=root, capture_output=True, text=True)
    assert proc.returncode == 2
    assert "-I -S" in proc.stdout + proc.stderr


def test_installed_startup_hooks_are_not_executed(tmp_path):
    environment = tmp_path / "tool-env"
    subprocess.run([sys.executable, "-I", "-S", "-m", "venv", "--without-pip",
                    str(environment)], check=True, capture_output=True)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    sites = list(environment.rglob("site-packages"))
    assert len(sites) == 1
    site = sites[0]
    pth_marker, site_marker = tmp_path / "PTH", tmp_path / "SITE"
    (site / "startup.pth").write_text(
        f"import pathlib; pathlib.Path({str(pth_marker)!r}).touch()\n")
    (site / "sitecustomize.py").write_text(
        f"import pathlib; pathlib.Path({str(site_marker)!r}).touch()\n")
    versions = {"ruff": "0.16.5", "mypy": "2.3.1", "import-linter": "2.14", "grimp": "3.16"}
    for name, version in versions.items():
        metadata = site / f"{name.replace('-', '_')}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    # Positive control: -I alone still executes installed startup hooks.
    subprocess.run([str(interpreter), "-I", "-c", "pass"], check=True, capture_output=True)
    assert pth_marker.exists() and site_marker.exists()
    pth_marker.unlink()
    site_marker.unlink()
    proc = subprocess.run(
        [str(interpreter), "-I", "-S", QUALITY["__file__"], "--static-tool", "versions"],
        cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == versions
    assert not pth_marker.exists() and not site_marker.exists()


@pytest.mark.parametrize("configuration", [
    "[importlinter]\nroot_package = pkg\ncontract_types = custom: plugin.Contract\n",
    "[importlinter]\nroot_package = pkg.child\n",
])
def test_executable_import_linter_config_is_rejected(isolated_policy_repo, configuration):
    root = isolated_policy_repo
    (root / ".importlinter").write_text(configuration)
    subprocess.run(["git", "add", ".importlinter"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "unsafe import config"],
                   cwd=root, check=True, capture_output=True)
    proc, report = isolated_check(root, "architecture")
    assert proc.returncode == 2, report
    assert "configuration" in report["checks"]["architecture"]["message"].lower()


@pytest.mark.parametrize("check,source,rule", [
    ("lint", "value = undefined_name\n", "F821"),
    ("typing", "def f() -> int:\n return 'wrong'\n", "return-value"),
])
def test_isolated_tools_still_report_real_source_errors(isolated_policy_repo, check, source, rule):
    root = isolated_policy_repo
    (root / "src" / "seed.py").write_text(source)
    # Candidate tool config cannot replace the protected config or load a plugin.
    (root / "pyproject.toml").write_text('[tool.mypy]\nplugins = ["malicious"]\n')
    proc, report = isolated_check(root, check)
    assert proc.returncode == 1, (proc.stdout, proc.stderr, report)
    findings = report["checks"][check]["findings"]
    assert rule in {item.get("identity", {}).get("rule", item["rule"]) for item in findings}


def surface_record(name="seed", **overrides):
    return {
        "id": name, "path": f"src/{name.replace('.', '/')}.py", "import_name": name,
        "area": "composition", "public_exports": ["value"], "private_modules": [],
        "allowed_importers": [], "legacy_aliases": [], "typing_status": "blocking",
        "responsibilities": "Own the fixture's typed integer value.",
        "source_revision": "a" * 40, **overrides,
    }


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("responsibilities"),
    lambda value: value.update(extra=True),
    lambda value: value.update(path="../outside.py"),
    lambda value: value.update(path="src/../outside.py"),
    lambda value: value.update(area="unknown"),
    lambda value: value.update(public_exports=["*"]),
    lambda value: value.update(public_exports=["value", "value"]),
    lambda value: value.update(allowed_importers=["*"]),
    lambda value: value.update(typing_status="ignored"),
    lambda value: value.update(source_revision="HEAD"),
])
def test_module_surface_records_fail_closed(mutation):
    record = surface_record()
    mutation(record)
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["validate_surfaces"]([record])


@pytest.mark.parametrize("field", ["id", "path", "import_name"])
def test_module_surface_records_have_unique_owners(field):
    first, second = surface_record(), surface_record("other")
    second[field] = first[field]
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["validate_surfaces"]([first, second])


def test_surface_identity_survives_successive_moves(tmp_path):
    before = tree(tmp_path / "before", {"renamed.py": "value: int = 1"})
    after = tree(tmp_path / "after", {"renamed_again.py": "value: int = 1"})
    records = [surface_record(import_name="renamed", path="src/renamed.py")]
    identities, findings = QUALITY["module_identities"](before, after, records)
    assert not findings
    assert identities == {"renamed_again": "seed"}


def test_surface_inventory_detects_unclassified_modules_and_stale_exports(tmp_path):
    modules = tree(tmp_path, {"seed.py": "changed: int = 1", "new.py": "value = 1"})
    findings = QUALITY["surface_findings"](
        [surface_record()], modules, {"seed": "seed", "new": "new"}, {"seed", "new"})
    assert {item["rule"] for item in findings} == {"surface-export", "surface-missing"}


def test_surface_move_does_not_drop_annotation_ratchet(tmp_path):
    before = tree(tmp_path / "before", {"renamed.py": "def f() -> int:\n return 1"})
    after = tree(tmp_path / "after", {"renamed.py": "def f():\n return 1"})
    records = [surface_record(import_name="renamed", path="src/renamed.py")]
    identities, _ = QUALITY["module_identities"](before, after, records)
    findings = QUALITY["source_policy"](before, after, identities, {"seed"}, records)
    assert {item["rule"] for item in findings} == {"annotation-removed"}


def test_surface_move_cannot_duplicate_stable_identity(tmp_path):
    before = tree(tmp_path / "before", {"renamed.py": "value: int = 1"})
    after = tree(tmp_path / "after", {"renamed.py": "value: int = 1", "seed.py": "other = 2"})
    records = [surface_record(import_name="renamed", path="src/renamed.py")]
    _, findings = QUALITY["module_identities"](before, after, records)
    assert {item["rule"] for item in findings} == {"module-identity"}


def test_surface_move_preserves_exact_exception_owner(tmp_path):
    modules = tree(tmp_path, {"renamed.py": "try:\n work()\nexcept Exception:\n raise"})
    inventory = QUALITY["handlers"](modules, {"renamed": "seed"})
    assert inventory[0]["module_id"] == "seed"


@pytest.mark.parametrize("overrides,rule", [
    ({"legacy_aliases": ["missing.value"]}, "surface-alias"),
    ({"legacy_aliases": ["alias.missing"]}, "surface-alias"),
    ({"private_modules": ["unrelated._secret"]}, "surface-private"),
    ({"allowed_importers": ["missing"]}, "surface-importer"),
    ({"typing_status": "inventoried"}, "surface-typing"),
])
def test_surface_relationships_and_typing_are_source_backed(tmp_path, overrides, rule):
    modules = tree(tmp_path, {"seed.py": "value: int = 1", "alias.py": "from seed import value"})
    records = [surface_record(**overrides), surface_record("alias", typing_status="inventoried")]
    findings = QUALITY["surface_findings"](records, modules, {n: n for n in modules}, {"seed"})
    assert rule in {item["rule"] for item in findings}


def test_surface_declared_consumers_and_public_boundary_are_enforced(tmp_path):
    modules = tree(tmp_path, {"seed.py": "value = 1\ninternal = 2",
                              "consumer.py": "from seed import internal"})
    records = [surface_record(), surface_record("consumer", public_exports=[])]
    _, findings = QUALITY["architecture"](modules, rules(), surfaces=records)
    assert {item["rule"] for item in findings} == {"surface-consumer", "surface-access"}


def test_surface_cannot_claim_unrelated_private_module(tmp_path):
    modules = tree(tmp_path, {"seed.py": "value = 1", "unrelated/_secret.py": "value = 2"})
    records = [surface_record(private_modules=["unrelated._secret"]),
               surface_record("unrelated._secret", typing_status="inventoried")]
    findings = QUALITY["surface_findings"](records, modules, {n: n for n in modules}, {"seed"})
    assert {item["rule"] for item in findings} == {"surface-private"}


def test_source_provenance_requires_real_export_in_frozen_revision(isolated_policy_repo):
    root = isolated_policy_repo
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                              text=True, capture_output=True).stdout.strip()
    record = surface_record(public_exports=["f"], source_revision=revision)
    assert not QUALITY["surface_provenance"](root, [record])
    record["public_exports"] = ["missing"]
    assert QUALITY["surface_provenance"](root, [record])[0]["rule"] == "surface-provenance"
    record["source_revision"] = "a" * 40
    with pytest.raises(QUALITY["QualityError"]):
        QUALITY["surface_provenance"](root, [record])
