"""Every route guard names its scope as a string literal at the call site.

The fleet's AUTH-003 conformance rule reads a guard's scope from the literal
in the ``require_scope(...)`` / ``require_event_scope(...)`` call. A scope
passed through a module constant reads as unresolvable, so the route
classifies as neither scope-guarded, authenticated-only nor public. That
rule only runs after code lands; this test catches the same thing before
merge.

Guards are enumerated by call, never by route path, so routes registered
through a factory or with variable paths are covered too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROUTERS = Path(__file__).resolve().parents[2] / "src" / "api_deejaytools" / "routers"
GUARDS = frozenset({"require_scope", "require_event_scope"})


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scope_arg(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "scope":
            return kw.value
    return None


def _is_literal_str(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _guard_calls(source: str, filename: str) -> list[tuple[int, str, bool]]:
    """Each guard call in ``source`` as (line, callee, scope is a literal)."""
    calls = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Call):
            name = _callee_name(node.func)
            if name in GUARDS:
                literal = _is_literal_str(_scope_arg(node))
                calls.append((node.lineno, name, literal))
    return calls


def test_every_route_guard_scope_is_a_string_literal() -> None:
    found = 0
    offenders = []
    for path in sorted(ROUTERS.rglob("*.py")):
        for line, name, literal in _guard_calls(path.read_text(), str(path)):
            found += 1
            if not literal:
                rel = path.relative_to(ROUTERS.parents[2])
                offenders.append(f"{rel}:{line} {name}(...)")
    assert found, f"no route guards found under {ROUTERS}"
    assert not offenders, (
        "Pass the scope as a string literal, not a constant or expression, "
        "so AUTH-003 can read it:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("source", "literal"),
    [
        ('require_scope("deejaytools.queue.read")', True),
        ('auth.require_event_scope("deejaytools.queue.read", THIS_EVENT)', True),
        ('require_scope(scope="deejaytools.queue.read")', True),
        ("require_scope(READ_SCOPE)", False),
        ("require_event_scope(READ_SCOPE, THIS_EVENT)", False),
        ('require_scope("deejaytools." + "queue.read")', False),
        ("require_scope()", False),
    ],
)
def test_guard_call_classification(source: str, literal: bool) -> None:
    calls = _guard_calls(source, "<test>")
    assert [c[2] for c in calls] == [literal]
