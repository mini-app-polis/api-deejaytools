"""Which Discord events each environment posts (DISCORD_NOTIFY_EVENTS)."""

from __future__ import annotations

import pytest
from mini_app_polis import activity

from api_deejaytools.config import Settings, get_settings
from api_deejaytools.services import activity_feed, notifications


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
    assert activity_feed.feed_enabled() is True
    assert notifications._config().report_faults is True


def test_development_drops_data_changes_and_keeps_faults(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert activity_feed.feed_enabled() is False
    assert notifications._config().report_faults is True


def test_the_library_tally_is_always_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changes are this service's own feed; the library middleware only
    reports faults."""
    for environment in ("production", "development", "local"):
        monkeypatch.setattr(settings, "ENVIRONMENT", environment)
        assert notifications._config().report_changes is False


def test_an_event_left_out_of_the_list_is_not_sent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings, "DISCORD_NOTIFY_EVENTS", {"Production": [notifications.DATA_CHANGED]}
    )
    assert notifications.event_enabled(notifications.FAULT, settings) is False
    assert activity_feed.feed_enabled() is True
    assert notifications._config().report_faults is False

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
    assert notifications._config().report_faults is True
    # Still production's channel: the feed posts from nowhere else.
    assert activity_feed.feed_enabled() is False


def test_notify_data_changes_still_gates_an_allowed_event(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "NOTIFY_DATA_CHANGES", False)
    assert activity_feed.feed_enabled() is False
    assert notifications._config().report_faults is True


def test_the_list_is_read_from_the_environment_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_NOTIFY_EVENTS", '{"staging": ["fault"]}')
    get_settings.cache_clear()
    try:
        assert get_settings().DISCORD_NOTIFY_EVENTS == {"staging": ["fault"]}
    finally:
        get_settings.cache_clear()


def test_every_channel_resolves_from_declared_settings() -> None:
    """Each channel's webhook comes from a declared Settings field: its own
    where the service posts to it, else the shared fallback."""
    from mini_app_polis import discord

    from api_deejaytools.config import Settings
    from api_deejaytools.services.notifications import webhook_source

    settings = Settings.model_construct(
        DISCORD_WEBHOOK_URL="https://discord.test/fallback",
        DISCORD_WEBHOOK_URL_ERRORS="https://discord.test/errors",
        DISCORD_WEBHOOK_URL_ACTIVITY="https://discord.test/activity",
    )
    source = webhook_source(settings)
    resolved = {
        channel: discord.webhook_url(channel, source=source)
        for channel in discord.CHANNEL_ENV
    }
    assert resolved[discord.CHANNEL_ERRORS] == "https://discord.test/errors"
    assert resolved[discord.CHANNEL_ACTIVITY] == "https://discord.test/activity"
    for channel, url in resolved.items():
        assert url is not None, channel


def test_activity_posts_to_its_own_channel_when_set() -> None:
    """DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY takes activity off the
    fleet's shared channel; errors stay where they were."""
    from mini_app_polis import discord

    from api_deejaytools.config import Settings
    from api_deejaytools.services.notifications import webhook_source

    settings = Settings.model_construct(
        DISCORD_WEBHOOK_URL="https://discord.test/fallback",
        DISCORD_WEBHOOK_URL_ERRORS="https://discord.test/errors",
        DISCORD_WEBHOOK_URL_ACTIVITY="https://discord.test/activity",
        DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY="https://discord.test/deejaytools",
    )
    source = webhook_source(settings)
    assert (
        discord.webhook_url(discord.CHANNEL_ACTIVITY, source=source)
        == "https://discord.test/deejaytools"
    )
    assert (
        discord.webhook_url(discord.CHANNEL_ERRORS, source=source)
        == "https://discord.test/errors"
    )


def test_activity_falls_back_to_the_shared_channel_when_unset() -> None:
    from mini_app_polis import discord

    from api_deejaytools.config import Settings
    from api_deejaytools.services.notifications import webhook_source

    for own in (None, "", "   "):
        settings = Settings.model_construct(
            DISCORD_WEBHOOK_URL="https://discord.test/fallback",
            DISCORD_WEBHOOK_URL_ACTIVITY="https://discord.test/activity",
            DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY=own,
        )
        assert (
            discord.webhook_url(
                discord.CHANNEL_ACTIVITY, source=webhook_source(settings)
            )
            == "https://discord.test/activity"
        ), own
