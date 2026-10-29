"""Feedback addresses: required in production, optional elsewhere."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api_deejaytools.config import Settings


@pytest.fixture(autouse=True)
def _database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEJAYTOOLS_DATABASE_URL", "postgresql://user@localhost/db")
    monkeypatch.delenv("FEEDBACK_FROM_EMAIL", raising=False)
    monkeypatch.delenv("FEEDBACK_TO_EMAIL", raising=False)


def test_both_addresses_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FEEDBACK_FROM_EMAIL", "from@example.test")
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "to@example.test")
    assert Settings().feedback_addresses == ("from@example.test", "to@example.test")


@pytest.mark.parametrize("given", ["FEEDBACK_FROM_EMAIL", "FEEDBACK_TO_EMAIL", None])
def test_production_refuses_to_start_without_both(
    monkeypatch: pytest.MonkeyPatch, given: str | None
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    if given:
        monkeypatch.setenv(given, "someone@example.test")
    with pytest.raises(ValidationError, match="FEEDBACK_FROM_EMAIL"):
        Settings()


def test_elsewhere_either_may_be_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("FEEDBACK_FROM_EMAIL", "from@example.test")
    assert Settings().feedback_addresses is None
