"""Updating source branches must never implicitly deploy Azure resources."""

import re
from pathlib import Path


def test_dev_deployment_requires_an_explicit_manual_dispatch():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cicd_pipeline.yaml"
    contents = workflow.read_text(encoding="utf-8")
    trigger_block = re.search(r"(?m)^on:\n((?:[ \t]+[^\n]*\n|\n)*)", contents)
    assert trigger_block is not None
    assert trigger_block.group(1).strip() == "workflow_dispatch:"


def test_dev_deployment_token_is_explicitly_read_only():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cicd_pipeline.yaml"
    contents = workflow.read_text(encoding="utf-8")
    permissions_block = re.search(r"(?m)^permissions:\n((?:[ \t]+[^\n]*\n|\n)*)", contents)
    assert permissions_block is not None
    assert permissions_block.group(1).strip() == "contents: read"
    assert re.findall(r"(?m)^[ \t]*permissions:", contents) == ["permissions:"]
