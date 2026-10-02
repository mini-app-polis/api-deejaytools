"""Name casing helpers (deejaytools-api src/lib/text.ts)."""

from __future__ import annotations

from .zod_coerce import js_words


def _upper_first(word: str) -> str:
    return word[:1].upper() + word[1:]


def title_case_words(value: str) -> str:
    """Trim, collapse internal whitespace, and capitalize the first letter of
    each word, keeping the rest ("jtSwing  team jv" -> "JtSwing Team Jv")."""
    return " ".join(_upper_first(w) for w in js_words(value))


def title_case_if_no_caps(value: str) -> str:
    """Trim and collapse whitespace; capitalize a word's first letter only if
    the word has no A-Z capital yet ("jtSwing", "JV", "McX" are kept)."""
    return " ".join(
        w if any("A" <= c <= "Z" for c in w) else _upper_first(w)
        for w in js_words(value)
    )
