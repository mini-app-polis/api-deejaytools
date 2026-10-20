"""Every name the production service reads reaches it.

The service only sees the variables .railway/railway.ts declares, and the
next apply removes any variable the file does not declare. A Settings field
missing from the file loses whatever value it holds on the service at the
next apply — the migration role's URL, the Discord fallback webhook, the
switch that mutes the change feed without a deploy.
"""

from __future__ import annotations

import re
from pathlib import Path

from api_deejaytools.config import Settings

RAILWAY_TS = Path(__file__).resolve().parents[2] / ".railway" / "railway.ts"

#: Settings fields deliberately left out of railway.ts, each with the reason.
#: Only a field with a default can be here: the value in config.py is the
#: reviewed one, and it is not set per environment. A field that production
#: sets is declared in railway.ts instead.
NOT_DECLARED: dict[str, str] = {}


def _shared_names() -> set[str]:
    text = RAILWAY_TS.read_text()
    block = text.split("const SHARED = [", 1)[1].split("];", 1)[0]
    return set(re.findall(r'"([A-Z0-9_]+)"', block))


def _env_keys() -> set[str]:
    """Names declared directly in the service's env block (preserve() etc.)."""
    text = RAILWAY_TS.read_text()
    block = text.split("env: {", 1)[1].split("\n    },", 1)[0]
    return set(re.findall(r"^\s+([A-Z0-9_]+):", block, flags=re.MULTILINE))


def _declared_names() -> set[str]:
    return _shared_names() | _env_keys()


def test_the_parser_sees_both_kinds_of_declaration() -> None:
    # Guards the two parsers above: if either stops matching, the checks
    # below would pass vacuously or fail for the wrong reason.
    assert "DEEJAYTOOLS_DATABASE_URL" in _env_keys()
    assert "ENVIRONMENT" in _shared_names()


def test_every_settings_field_is_declared_or_deliberately_left_out() -> None:
    declared = _declared_names()
    missing = sorted(
        name
        for name in Settings.model_fields
        if name not in declared and name not in NOT_DECLARED
    )
    assert missing == [], (
        "Settings fields missing from .railway/railway.ts; the next apply "
        "removes them from the service. Declare each there, or list it in "
        f"NOT_DECLARED with a reason: {missing}"
    )


def test_the_not_declared_list_names_only_optional_undeclared_fields() -> None:
    fields = Settings.model_fields
    declared = _declared_names()
    for name in NOT_DECLARED:
        assert name in fields, f"{name} is no longer a Settings field"
        assert not fields[name].is_required(), f"{name} has no default"
        assert name not in declared, f"{name} is declared in railway.ts"
