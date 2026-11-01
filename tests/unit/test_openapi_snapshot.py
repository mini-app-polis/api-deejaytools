"""docs/openapi.json is the app's OpenAPI document, regenerated and committed.

deejaytools.com checks the endpoints and schemas it relies on against this
snapshot, so a stale one would hide a breaking change. Regenerate it with
``uv run python scripts/export_openapi.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_openapi", ROOT / "scripts" / "export_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_snapshot_is_current() -> None:
    exporter = _exporter()
    assert exporter.SNAPSHOT.read_text() == exporter.render(), (
        "docs/openapi.json is stale: run "
        "`uv run python scripts/export_openapi.py` and commit the result."
    )


def test_check_mode_reports_a_stale_snapshot(tmp_path: Path) -> None:
    exporter = _exporter()
    exporter.SNAPSHOT = tmp_path / "openapi.json"
    assert exporter.main(["--check"]) == 1
    assert exporter.main([]) == 0
    assert exporter.main(["--check"]) == 0
    assert "version" not in exporter.document()["info"]
