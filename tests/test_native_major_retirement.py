"""Physical major-version retirement preserves shared helpers and exact history."""

import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "contracts/native-major-retirement.json").read_text())
EXTRACTION = json.loads((ROOT / "contracts/nl2sql-mcp-extraction.json").read_text())


def historical_json(path: str) -> dict:
    result = subprocess.run(
        ["git", "--no-pager", "show", f"{CONTRACT['source_inventory_revision']}:{path}"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_evidence_job_fetches_immutable_provenance_history():
    pipeline = yaml.safe_load((ROOT / ".github/workflows/pr_pipeline.yaml").read_text())
    checkouts = [
        step for step in pipeline["jobs"]["tests"]["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["fetch-depth"] == 0
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_physical_retirement_matches_source_backed_inventory():
    before = historical_json(".quality/module-surfaces.json")["entries"]
    current = json.loads((ROOT / ".quality/module-surfaces.json").read_text())["entries"]
    removed = {entry["id"] for entry in before if not (ROOT / entry["path"]).exists()}
    assert removed == set(CONTRACT["physically_retired_modules"]) | set(EXTRACTION["physically_retired_modules"])
    assert not removed.intersection(entry["id"] for entry in current)
    assert all((ROOT / entry["path"]).is_file() for entry in current)


def test_handler_history_is_preserved_but_never_a_current_allowance():
    before = historical_json(".quality/exceptions.json")
    history = json.loads((ROOT / CONTRACT["historical_exception_inventory"]).read_text())
    assert before == history
    current = json.loads((ROOT / ".quality/exceptions.json").read_text())
    old_ids = {entry["id"] for entry in history["entries"]}
    current_ids = {entry["id"] for entry in current["entries"]}
    assert old_ids - current_ids == (
        set(CONTRACT["retired_exception_ids"]) | set(EXTRACTION["retired_exception_ids"])
    ) & old_ids
    assert current_ids - old_ids == set()


@pytest.mark.parametrize("path", [
    "src/connectors/foundry_iq.py",
    "src/connectors/search.py", "src/connectors/obo.py", "src/connectors/mcp_client.py",
    "src/plugins/retrieval/plugin.py",
])
def test_retirement_keeps_current_shared_capability_and_security_helpers(path):
    assert (ROOT / path).is_file()
    assert all(
        (ROOT / test).is_file() for test in CONTRACT["retained_capability_security_tests"]
        if test not in EXTRACTION["retired_tests"]
    )


def test_extraction_is_physical_and_preserves_exact_pre_extraction_history():
    result = subprocess.run(
        ["git", "--no-pager", "show", f"{EXTRACTION['source_inventory_revision']}:.quality/module-surfaces.json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    before = json.loads(result.stdout)["entries"]
    removed = {entry["id"] for entry in before if not (ROOT / entry["path"]).exists()}
    assert removed == set(EXTRACTION["physically_retired_modules"])
    assert all(not (ROOT / test).exists() for test in EXTRACTION["retired_tests"])
    assert all((ROOT / test).exists() for test in EXTRACTION["replacement_behavior_tests"])
