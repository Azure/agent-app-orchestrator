"""Optional diagnostic reporting cannot invent a resolved configuration value."""

import logging
import asyncio
from unittest.mock import MagicMock

import pytest

from telemetry.telemetry import Telemetry
from boundary_fixtures import actual_config, _retry_failure


@pytest.mark.parametrize("mode", ["environment", "configured", "missing", "retry", "provider-error", "logging-error", "cancelled"])
def test_log_level_diagnostics_preserve_sources_without_false_default(actual_config, monkeypatch, caplog, mode):
    marker = "synthetic-private-diagnostic-detail"
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    if mode == "environment":
        monkeypatch.setenv("LOG_LEVEL", " debug ")
        actual_config.get_config_with_retry = MagicMock(side_effect=RuntimeError(marker))
    elif mode == "configured":
        actual_config.client["LOG_LEVEL"] = "warning"
    elif mode == "retry":
        actual_config.get_config_with_retry = MagicMock(side_effect=_retry_failure())
    elif mode == "provider-error":
        actual_config.get_config_with_retry = MagicMock(side_effect=RuntimeError(marker))
    elif mode == "logging-error":
        monkeypatch.setattr(logging.getLogger(), "info", MagicMock(side_effect=RuntimeError(marker)))
    elif mode == "cancelled":
        failure = asyncio.CancelledError(marker)
        actual_config.get_config_with_retry = MagicMock(side_effect=failure)
        with pytest.raises(asyncio.CancelledError) as raised:
            Telemetry.log_log_level_diagnostics(actual_config)
        assert raised.value is failure
        return
    Telemetry.log_log_level_diagnostics(actual_config)
    messages = [record.getMessage() for record in caplog.records]
    if mode in {"provider-error", "logging-error"}:
        assert not any(message.startswith("Resolved LOG_LEVEL=") for message in messages)
        assert any(record.levelno == logging.WARNING for record in caplog.records)
    else:
        level, source = {
            "environment": ("DEBUG", "env"),
            "configured": ("WARNING", "appconfig"),
            "missing": ("INFO", "default"),
            "retry": ("INFO", "default"),
        }[mode]
        assert f"Resolved LOG_LEVEL={level} (source={source})" in messages
        assert any(message.startswith("Effective root logger level:") for message in messages)
    if mode == "environment":
        actual_config.get_config_with_retry.assert_not_called()
    assert marker not in caplog.text
