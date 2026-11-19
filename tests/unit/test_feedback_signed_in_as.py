"""How the feedback email names a signed-in sender."""

from __future__ import annotations

from api_deejaytools.models import User
from api_deejaytools.services.feedback_delivery import signed_in_as


def _user(**fields: str | None) -> User:
    base: dict[str, object] = {
        "id": "user_1",
        "email": "ada@example.com",
        "display_name": None,
        "first_name": None,
        "last_name": None,
        "created_at": 1,
        "updated_at": 1,
    }
    base.update(fields)
    return User(**base)


def test_not_signed_in() -> None:
    assert signed_in_as(None, None) == "Not signed in"


def test_no_user_on_file_keeps_the_id() -> None:
    assert signed_in_as("user_1", None) == "user_1"


def test_name_email_and_id() -> None:
    user = _user(first_name="Ada", last_name="Lovelace")
    assert signed_in_as("user_1", user) == "Ada Lovelace <ada@example.com> (user_1)"


def test_display_name_when_no_first_or_last() -> None:
    user = _user(display_name="Ada L")
    assert signed_in_as("user_1", user) == "Ada L <ada@example.com> (user_1)"


def test_email_alone_when_no_name() -> None:
    assert signed_in_as("user_1", _user()) == "<ada@example.com> (user_1)"
