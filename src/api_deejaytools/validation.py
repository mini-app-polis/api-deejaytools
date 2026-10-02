"""Request-validation rules that must match deejaytools-api's zod schemas.

Pydantic's defaults differ from zod's in ways the wire contract notices;
where they do, the zod behaviour is reproduced here and used instead:

- Types are strict. zod does not coerce: ``"5"`` is not a number and
  ``"true"`` is not a boolean. Pydantic's lax mode would accept both.
- Absent is not null. zod's ``.optional()`` accepts a missing key and
  refuses ``null``; only ``.nullable()`` accepts null. ``ZodModel`` refuses
  null for every field not listed in ``_NULLABLE``.
- Email addresses follow zod's own pattern, not email-validator's.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator


class ZodModel(BaseModel):
    """A request body or query validated the way its zod schema validated it.

    Fields that zod declared ``.optional()`` are typed ``X | None = None``
    here; ``null`` for them is refused unless the field is in ``_NULLABLE``.
    Use ``model_fields_set`` to tell an absent field from one sent as null.
    Unknown keys are dropped, as zod's ``z.object`` drops them.
    """

    model_config = ConfigDict(strict=True, extra="ignore")

    _NULLABLE: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _absent_is_not_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nulls = [
                k
                for k, v in data.items()
                if v is None and k in cls.model_fields and k not in cls._NULLABLE
            ]
            if nulls:
                raise ValueError(f"{', '.join(nulls)}: expected a value, received null")
        return data


# zod 4.6's `z.string().email()` pattern (zod/v4/core/regexes.js `email`).
# Pydantic's EmailStr is stricter in places zod is not: it refuses reserved
# domains such as `.test`, which the web app's own tests and the conformance
# suite use.
ZOD_EMAIL = re.compile(
    r"^(?:[A-Za-z0-9_'+\-]+\.)*[A-Za-z0-9_'+\-]*[A-Za-z0-9_+-]"
    r"@(?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}$"
)


def _zod_email(value: str) -> str:
    # fullmatch: Python's $ also matches before a trailing newline.
    if not ZOD_EMAIL.fullmatch(value):
        raise ValueError("Invalid email address")
    return value


Email = Annotated[str, AfterValidator(_zod_email)]
"""A string zod's ``.email()`` would accept."""


def _non_empty(value: str) -> str:
    if len(value) < 1:
        raise ValueError("Too small: expected string to have >=1 characters")
    return value


NonEmptyStr = Annotated[str, AfterValidator(_non_empty)]
"""zod's ``z.string().min(1)``: length checked on the value as sent."""


def _js_integer(value: float) -> int:
    if value != value or value in (float("inf"), float("-inf")) or value != int(value):
        raise ValueError("Invalid input: expected int, received number")
    return int(value)


def _js_number(value: float) -> float:
    # zod's z.number() refuses Infinity and NaN; JSON's 1e400 parses to inf.
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("Invalid input: expected number, received Infinity")
    return int(value) if value == int(value) else value


JsNumber = Annotated[float, AfterValidator(_js_number)]
"""zod's ``z.number()``: any JSON number. Integral values come back as int,
so epoch-millisecond fields store and serialize as integers."""

JsInt = Annotated[float, AfterValidator(_js_integer)]
"""zod's ``z.number().int()``: a JSON number with no fractional part (5.0
included, as JavaScript has one number type)."""


def _non_negative(value: int) -> int:
    if value < 0:
        raise ValueError("Too small: expected number to be >=0")
    return value


NonNegativeJsInt = Annotated[JsInt, AfterValidator(_non_negative)]
"""zod's ``z.number().int().min(0)``."""
