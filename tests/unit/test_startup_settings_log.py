"""Starting the app logs each effective setting once, with a value only for
names on SETTINGS_ALLOWLIST."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI

from api_deejaytools.config import Settings, get_settings

SECRET = "tick-secret-never-logged"


@pytest.fixture
def startup_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SETTINGS_ALLOWLIST", "AWS_REGION, TICK_INTERVAL_MS")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("TICK_INTERVAL_MS", "15000")
    monkeypatch.setenv("TICK_SECRET", SECRET)
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SENTRY_DSN_APIS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_startup_logs_each_setting_once_and_values_only_when_allowed(
    startup_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    from api_deejaytools.main import lifespan

    caplog.set_level(logging.INFO, logger="mini_app_polis")
    async with lifespan(FastAPI()):
        pass

    lines = [r.getMessage() for r in caplog.records]
    # An allowlisted default, an allowlisted value from the environment, a
    # set name that is not allowlisted, and an unset one.
    assert "AWS_REGION=us-east-1 (default)" in lines
    assert "TICK_INTERVAL_MS=15000 (set)" in lines
    assert "TICK_SECRET (set)" in lines
    assert "BREVO_API_KEY (unset)" in lines
    assert not any(SECRET in line for line in lines)
    assert not any(SECRET in str(r.args) for r in caplog.records)
    for name in Settings.model_fields:
        logged = [ln for ln in lines if ln.split("=")[0].split(" (")[0] == name]
        assert len(logged) == 1, f"{name} logged {len(logged)} times"
