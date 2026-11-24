"""Required governance checks must run without cloud access or PR privileges."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load_workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_quality_gate_runs_for_every_required_event_without_path_filters():
    workflow = load_workflow("quality-gate.yml")
    # PyYAML's YAML 1.1 parser treats the unquoted Actions "on" key as True.
    triggers = workflow[True]
    assert set(triggers) == {"pull_request", "push", "merge_group", "workflow_dispatch"}
    for event in ("pull_request", "push"):
        assert set(triggers[event]["branches"]) == {"main", "develop"}
        assert not {"paths", "paths-ignore"}.intersection(triggers[event])
    assert workflow["permissions"] == {"contents": "read"}
    gate = workflow["jobs"]["quality-gate"]
    assert set(gate["needs"]) == set(workflow["jobs"]) - {"quality-gate"}
    assert gate["if"] == "always()"
    assert "github.event.before" in gate["env"]["BASE_SHA"]
    assert "github.sha" in gate["env"]["BASE_SHA"]
    assert gate["env"]["BASE_SHA"] == workflow["jobs"]["quality"]["env"]["BASE_SHA"]


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", None, "missing"])
def test_final_gate_executes_fail_closed_dependency_check(result):
    gate = load_workflow("quality-gate.yml")["jobs"]["quality-gate"]
    script = gate["steps"][0]["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    for dependency in gate["needs"]:
        needs = {name: {"result": "success"} for name in gate["needs"]}
        if result == "missing":
            del needs[dependency]
        else:
            needs[dependency]["result"] = result
        process = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "JOB_RESULTS": json.dumps(needs)},
            capture_output=True, text=True,
        )
        assert (process.returncode == 0) is (result == "success"), process.stderr


@pytest.mark.parametrize(("branch", "repository", "allowed"), [
    ("release/1.2.3", "Azure/agent-app-orchestrator", True),
    ("release/10.20.30", "Azure/agent-app-orchestrator", True),
    ("release/1.2.3", "fork/agent-app-orchestrator", False),
    ("release/v1.2.3", "Azure/agent-app-orchestrator", False),
    ("release/1.2.3-rc1", "Azure/agent-app-orchestrator", False),
    ("release/1.2.3/extra", "Azure/agent-app-orchestrator", False),
    ("feature/change", "Azure/agent-app-orchestrator", False),
    ("develop", "Azure/agent-app-orchestrator", False),
    ("release/1.2.3\nother", "Azure/agent-app-orchestrator", False),
    ("$(exit 0)", "Azure/agent-app-orchestrator", False),
])
def test_release_guard_executes_exact_same_repository_policy(branch, repository, allowed):
    workflow = load_workflow("release-branch-guard.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow[True]["pull_request_target"]["branches"] == ["main"]
    steps = workflow["jobs"]["release-branch-required"]["steps"]
    assert len(steps) == 1
    process = subprocess.run(
        [shutil.which("bash"), "-c", steps[0]["run"]],
        env={**os.environ, "HEAD_REF": branch, "HEAD_REPOSITORY": repository,
             "GITHUB_REPOSITORY": "Azure/agent-app-orchestrator"},
        capture_output=True, text=True,
    )
    assert (process.returncode == 0) is allowed, process.stderr
