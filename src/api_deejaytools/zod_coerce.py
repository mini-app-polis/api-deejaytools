"""zod's ``z.coerce.number()``, which runs JavaScript's ``Number(input)`` first.

A query value arrives as a string, or as a list when its key is repeated
(``zod_query``). ``Number`` of a list is ``Number`` of its comma-joined
string, so one element counts as that element and several are NaN.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any

from pydantic import BeforeValidator

# JavaScript's WhiteSpace and LineTerminator: what String.prototype.trim()
# strips and what \s matches. Not Python's str.strip() set, which also strips
# U+001C-U+001F and U+0085 and keeps U+FEFF.
JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_JS_WHITESPACE = JS_WHITESPACE
_JS_WS_RUN = re.compile(f"[{re.escape(JS_WHITESPACE)}]+")


def js_trim(value: str) -> str:
    """JavaScript's ``value.trim()``."""
    return value.strip(JS_WHITESPACE)


def js_words(value: str) -> list[str]:
    """``value.trim().split(/\\s+/)``, without the one empty word an empty
    string gives."""
    trimmed = js_trim(value)
    return _JS_WS_RUN.split(trimmed) if trimmed else []


_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_RADIX = {"0x": 16, "0o": 8, "0b": 2}
_RADIX_DIGITS = {16: "0123456789abcdef", 8: "01234567", 2: "01"}


def js_number(value: Any) -> float:
    """``Number(value)`` for a string, a list of strings, or a number."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, list):
        value = ",".join("" if v is None else str(v) for v in value)
    if value is None:
        return 0.0
    text = str(value).strip(_JS_WHITESPACE)
    if text == "":
        return 0.0
    if text in ("Infinity", "+Infinity"):
        return math.inf
    if text == "-Infinity":
        return -math.inf
    radix = _RADIX.get(text[:2].lower())
    if radix is not None:
        digits = text[2:].lower()
        if digits and all(c in _RADIX_DIGITS[radix] for c in digits):
            return float(int(digits, radix))
        return math.nan
    if _DECIMAL.match(text):
        return float(text)
    return math.nan


def coerce_number(value: Any) -> float:
    """``Number(value)``, refusing NaN as zod does."""
    number = js_number(value)
    if math.isnan(number):
        raise ValueError("Invalid input: expected number, received NaN")
    return number


CoercedNumber = Annotated[float, BeforeValidator(coerce_number)]
"""``z.coerce.number()``: the value after ``Number(...)``; NaN is refused."""
