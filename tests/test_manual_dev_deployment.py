"""Updating source branches must never implicitly deploy Azure resources."""

from pathlib import Path

def test_dev_deployment_requires_an_explicit_manual_dispatch():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cicd_pipeline.yaml"
    contents = workflow.read_text(encoding="utf-8")
    _, trigger_marker, after_trigger = contents.partition("\non:\n")
    trigger_block, jobs_marker, _ = after_trigger.partition("\njobs:\n")
    assert trigger_marker and jobs_marker
    assert trigger_block.strip() == "workflow_dispatch:"
