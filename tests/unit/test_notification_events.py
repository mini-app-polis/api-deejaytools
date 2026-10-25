"""Which Discord events each environment posts (DISCORD_NOTIFY_EVENTS)."""

from __future__ import annotations

import pytest
from mini_app_polis import activity

from api_deejaytools.config import Settings, get_settings
from api_deejaytools.services import notifications


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_the_defaults_match_api_kaianolevine_com(settings: Settings) -> None:
    assert settings.DISCORD_NOTIFY_EVENTS == {
        "production": ["data_changed", "fault"],
        "development": ["fault"],
    }
    assert set(notifications.NOTIFY_EVENTS) == {"data_changed", "fault"}


def test_production_posts_data_changes_and_faults(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    config = notifications._config()
    assert config.report_changes is True
    assert config.report_faults is True


def test_development_drops_data_changes_and_keeps_faults(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    config = notifications._config()
    assert config.report_changes is False
    assert config.report_faults is True


def test_an_event_left_out_of_the_list_is_not_sent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings, "DISCORD_NOTIFY_EVENTS", {"Production": [notifications.DATA_CHANGED]}
    )
    assert notifications.event_enabled(notifications.FAULT, settings) is False
    config = notifications._config()
    assert config.report_changes is True
    assert config.report_faults is False

    dispatched: list[object] = []
    monkeypatch.setattr(activity, "dispatch", dispatched.append)
    notifications.report_fault("scheduler · sessions", RuntimeError("x"))
    assert dispatched == []


def test_an_environment_with_no_entry_sends_everything(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    for event in notifications.NOTIFY_EVENTS:
        assert notifications.event_enabled(event, settings) is True
    config = notifications._config()
    assert config.report_changes is True
    assert config.report_faults is True


def test_notify_data_changes_still_gates_an_allowed_event(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "NOTIFY_DATA_CHANGES", False)
    config = notifications._config()
    assert config.report_changes is False
    assert config.report_faults is True


def test_the_list_is_read_from_the_environment_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_NOTIFY_EVENTS", '{"staging": ["fault"]}')
    get_settings.cache_clear()
    try:
        assert get_settings().DISCORD_NOTIFY_EVENTS == {"staging": ["fault"]}
    finally:
        get_settings.cache_clear()
