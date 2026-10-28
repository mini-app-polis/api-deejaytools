"""An empty setting means "use the code default", not a value to parse."""

from __future__ import annotations

import pytest

from api_deejaytools.config import Settings


def test_an_empty_setting_falls_back_to_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEJAYTOOLS_DATABASE_URL", "postgresql://user@localhost/db")
    monkeypatch.setenv("DEEJAYTOOLS_CORS_ORIGINS", "")

    settings = Settings()

    assert settings.DEEJAYTOOLS_CORS_ORIGINS == ["http://localhost:5173"]
