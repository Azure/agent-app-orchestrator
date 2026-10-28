"""Updating source branches must never implicitly deploy Azure resources."""

from pathlib import Path

import yaml


def test_dev_deployment_requires_an_explicit_manual_dispatch():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cicd_pipeline.yaml"
    definition = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(definition["on"]) == {"workflow_dispatch"}
    assert definition["jobs"]["deploy"]["environment"] == "dev"
