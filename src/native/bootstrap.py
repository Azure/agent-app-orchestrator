"""Shared artifact metadata; host-specific telemetry remains with its official host."""

from pathlib import Path

from .config import ConfigView

_packaged_version = Path(__file__).parent / "VERSION"
_source_version = Path(__file__).parents[2] / "VERSION"
_service_version = (_packaged_version if _packaged_version.is_file() else _source_version).read_text(
    encoding="utf-8",
).strip()


def service_version() -> str:
    return _service_version


def configure_worker_telemetry(config: ConfigView) -> None:
    from telemetry import Telemetry

    Telemetry.configure_basic(config)
    Telemetry.configure_monitoring(
        config, "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "agent-app-orchestrator", service_version(),
    )
