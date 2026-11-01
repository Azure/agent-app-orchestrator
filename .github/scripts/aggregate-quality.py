"""Validate actual job results and fresh reports in the unprivileged aggregate job."""

import argparse
import json
from pathlib import Path
import runpy
import sys


def main():
    if not sys.flags.isolated or not sys.flags.no_site:
        raise SystemExit("Aggregate must start with python -I -S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--needs", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    args = parser.parse_args()
    checker = runpy.run_path(str(Path(__file__).with_name("check-quality.py")))
    needs = json.loads(args.needs)
    if set(needs) != {"quality", "tests", "frontend"} or any(
        job["result"] != "success" for job in needs.values()
    ):
        raise SystemExit("Required test/frontend/quality job was not successful")
    root = Path(__file__).resolve().parents[2]
    if checker["git"](root, "rev-parse", "HEAD").strip() != args.head_sha:
        raise SystemExit("Aggregate must execute from the exact reviewed head checkout")
    records = checker["load_records"](root)
    expected = checker["report_identity"](
        records, args.repository, args.run_id, args.run_attempt)
    reports = sorted(args.reports.rglob("quality-*.json"))
    if len(reports) != len(checker["CHECKS"]):
        raise SystemExit("Missing or duplicate quality report")
    seen = set()
    inventories = set()
    for path in reports:
        check = path.stem.removeprefix("quality-")
        if check not in checker["CHECKS"] or check in seen:
            raise SystemExit("Unexpected or duplicated check")
        report = json.loads(path.read_text(encoding="utf-8"))
        if not checker["report_valid"](
            report, args.base_sha, args.head_sha, {check}, expected
        ):
            raise SystemExit(f"Failed, stale or invalid {check} report")
        inventories.add(checker["digest"](
            [report["coverage"], report["handler_inventory"]]))
        seen.add(check)
    if len(inventories) != 1:
        raise SystemExit("Quality jobs reported different source inventories")
    print("quality-gate: passed")


if __name__ == "__main__":
    main()
