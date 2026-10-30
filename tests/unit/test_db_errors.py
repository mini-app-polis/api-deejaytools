"""Classifying database errors down SQLAlchemy's and asyncpg's wrapping."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from api_deejaytools.db_errors import driver_message, is_unique_violation


class _Driver(Exception):
    def __init__(self, message: str, **attrs: Any) -> None:
        super().__init__(message)
        for name, value in attrs.items():
            setattr(self, name, value)


def _wrapped(driver: BaseException) -> IntegrityError:
    """As SQLAlchemy raises it: the adapter's error, caused by the driver's."""
    adapter = _Driver("adapter")
    adapter.__cause__ = driver
    return IntegrityError("INSERT …", {}, adapter)


def test_a_unique_violation_is_found_down_the_chain() -> None:
    driver = _Driver("dup", sqlstate="23505", constraint_name="users_email_unique")
    exc = _wrapped(driver)
    assert is_unique_violation(exc)
    assert is_unique_violation(exc, "users_email_unique")
    assert not is_unique_violation(exc, "another_constraint")


def test_the_constraint_name_may_sit_on_the_cause() -> None:
    adapter = _Driver("adapter", pgcode="23505")
    adapter.__cause__ = _Driver("driver", constraint_name="songs_slot_unique")
    exc = IntegrityError("INSERT …", {}, adapter)
    assert is_unique_violation(exc, "songs_slot_unique")


def test_other_errors_are_not_unique_violations() -> None:
    assert not is_unique_violation(_wrapped(_Driver("fk", sqlstate="23503")))
    assert not is_unique_violation(ValueError("plain"))


def test_the_driver_message_is_the_servers_own_text() -> None:
    driver = _Driver("invalid byte sequence for encoding", sqlstate="22021")
    assert driver_message(_wrapped(driver)) == "invalid byte sequence for encoding"


def test_without_a_driver_error_the_message_is_the_exceptions() -> None:
    assert driver_message(ValueError("plain")) == "plain"
