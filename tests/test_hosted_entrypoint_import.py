"""Fresh native imports cannot depend on Azure or a warmed classic runtime.

Keep explicit negative coverage for retired entrypoints and lazy connector
imports. Import success is not proof of deployed readiness or durability.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _run_fresh_import(import_statement: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    # Keep the probe hermetic: no App Configuration endpoint to reach, no
    # Azure Monitor connection string, auth disabled so nothing else needs a
    # live dependency just to construct the ASGI app object.
    env.pop("APP_CONFIG_ENDPOINT", None)
    env.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
    env["DISABLE_AUTH"] = "true"
    return subprocess.run(
        [sys.executable, "-c", import_statement],
        cwd=SRC_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_hosted_entrypoint_imports_cleanly_on_a_fresh_interpreter() -> None:
    """The actual native Docker module must load without classic bootstrap."""
    completed = _run_fresh_import("import native.hosted")

    assert completed.returncode == 0, (
        "native.hosted failed to import on a fresh interpreter "
        f"(this is what the hosted agent container runs at startup):\n"
        f"{completed.stderr}"
    )
    assert "ImportError" not in completed.stderr
    assert "circular import" not in completed.stderr


def test_classic_hosted_entrypoint_fails_explicitly_without_native_fallback() -> None:
    completed = _run_fresh_import("import api.hosted_entrypoint")
    assert completed.returncode != 0
    assert "retired" in completed.stderr


def test_classic_orchestrator_fails_explicitly_without_native_fallback() -> None:
    completed = _run_fresh_import("import orchestration.orchestrator")
    assert completed.returncode != 0
    assert "retired" in completed.stderr


def test_dependencies_module_alone_imports_without_touching_connectors() -> None:
    """``dependencies`` must not eagerly import ``connectors`` at module
    level -- that eager reference is exactly what created the cycle with
    connector submodules (``cosmosdb``, ``search``, ``aifoundry``, ...) that
    import ``dependencies`` back. ``AppConfigClient`` construction must stay
    deferred inside ``get_config()``.
    """
    completed = _run_fresh_import(
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import dependencies\n"
        "after = sorted(m for m in set(sys.modules) - before if m == 'connectors' or m.startswith('connectors.'))\n"
        "print(json.dumps(after))\n"
    )

    assert completed.returncode == 0, completed.stderr
    import json as _json

    loaded_connectors_modules = _json.loads(completed.stdout)
    assert loaded_connectors_modules == [], (
        "Importing `dependencies` alone must not pull in `connectors` at "
        f"module level (it did: {loaded_connectors_modules}); that eager "
        "coupling is what caused the dependencies<->connectors.cosmosdb "
        "circular import."
    )
