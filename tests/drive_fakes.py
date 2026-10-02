"""A stand-in for the Google Drive v3 service client, under a real DriveFacade.

Tests of the Drive layer run the real ``DriveFacade`` (so its process-wide
``FOLDER_CACHE`` and retry wrapper are exercised) over this fake, which keeps
folders and files in memory and records every request. No network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from mini_app_polis.google.drive import DriveFacade

FOLDER_MIME = "application/vnd.google-apps.folder"

_FOLDER_QUERY = re.compile(
    r"^'(?P<parent>[^']*)' in parents and name = '(?P<name>.*)' "
)
_APP_PROP_QUERY = re.compile(
    r"^'(?P<parent>[^']*)' in parents and appProperties has "
    r"\{ key='(?P<key>[^']*)' and value='(?P<value>[^']*)' \}"
)


class _Request:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


class FakeDriveService:
    """Folders, files, their parents and appProperties, plus a call log."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # id -> {"name", "parents", "mimeType", "appProperties"}
        self.files_by_id: dict[str, dict[str, Any]] = {}
        self._next = 0
        self.fail_copy: Exception | None = None
        self.fail_list: Exception | None = None

    # --- helpers for tests -------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next}"

    def add_file(
        self,
        name: str,
        parents: list[str],
        *,
        app_properties: dict[str, str] | None = None,
        file_id: str | None = None,
    ) -> str:
        """Put a file into the fake Drive and return its id."""
        fid = file_id or self._new_id("file")
        self.files_by_id[fid] = {
            "name": name,
            "parents": list(parents),
            "mimeType": "audio/mpeg",
            "appProperties": dict(app_properties or {}),
        }
        return fid

    def folder_id(self, parent: str, name: str) -> str | None:
        """The id of folder ``name`` under ``parent``, if it exists."""
        for fid, f in self.files_by_id.items():
            if (
                f["mimeType"] == FOLDER_MIME
                and f["name"] == name
                and parent in f["parents"]
            ):
                return fid
        return None

    def folder_path(self, folder_id: str) -> list[str]:
        """Names from the root's child down to ``folder_id``."""
        names: list[str] = []
        current: str | None = folder_id
        while current in self.files_by_id:
            f = self.files_by_id[current]
            names.append(f["name"])
            current = f["parents"][0] if f["parents"] else None
        return list(reversed(names))

    def ops(self, name: str) -> list[dict[str, Any]]:
        """Recorded kwargs of every call to ``name`` (e.g. ``files.list``)."""
        return [kw for op, kw in self.calls if op == name]

    # --- the googleapiclient surface the facade uses ------------------------
    def files(self) -> FakeDriveService:
        return self

    def permissions(self) -> _Permissions:
        return _Permissions(self)

    def list(self, **kw: Any) -> _Request:
        self.calls.append(("files.list", kw))

        def run() -> Any:
            if self.fail_list is not None:
                raise self.fail_list
            q = kw["q"]
            m = _APP_PROP_QUERY.match(q)
            if m:
                ids = [
                    fid
                    for fid, f in self.files_by_id.items()
                    if m["parent"] in f["parents"]
                    and f["appProperties"].get(m["key"]) == m["value"]
                ]
                return {"files": [{"id": i} for i in ids]}
            m = _FOLDER_QUERY.match(q)
            assert m, q
            name = m["name"].replace("\\'", "'")
            fid = self.folder_id(m["parent"], name)
            return {"files": [{"id": fid, "name": name}] if fid else []}

        return _Request(run)

    def create(self, **kw: Any) -> _Request:
        self.calls.append(("files.create", kw))

        def run() -> Any:
            body = kw["body"]
            if body.get("mimeType") == FOLDER_MIME:
                fid = self._new_id("folder")
                self.files_by_id[fid] = {
                    "name": body["name"],
                    "parents": list(body["parents"]),
                    "mimeType": FOLDER_MIME,
                    "appProperties": {},
                }
                return {"id": fid}
            fid = self.add_file(
                body["name"], body["parents"], app_properties=body.get("appProperties")
            )
            return {"id": fid}

        return _Request(run)

    def copy(self, **kw: Any) -> _Request:
        self.calls.append(("files.copy", kw))

        def run() -> Any:
            if self.fail_copy is not None:
                raise self.fail_copy
            body = kw["body"]
            fid = self.add_file(
                body["name"], body["parents"], app_properties=body.get("appProperties")
            )
            return {"id": fid}

        return _Request(run)

    def get(self, **kw: Any) -> _Request:
        self.calls.append(("files.get", kw))

        def run() -> Any:
            f = self.files_by_id[kw["fileId"]]
            return {"name": f["name"], "parents": list(f["parents"])}

        return _Request(run)

    def update(self, **kw: Any) -> _Request:
        self.calls.append(("files.update", kw))

        def run() -> Any:
            f = self.files_by_id[kw["fileId"]]
            if "body" in kw and "name" in kw["body"]:
                f["name"] = kw["body"]["name"]
            if "removeParents" in kw:
                removed = kw["removeParents"].split(",")
                f["parents"] = [p for p in f["parents"] if p not in removed]
            if "addParents" in kw:
                f["parents"].append(kw["addParents"])
            return {"id": kw["fileId"]}

        return _Request(run)


class _Permissions:
    def __init__(self, svc: FakeDriveService) -> None:
        self._svc = svc

    def create(self, **kw: Any) -> _Request:
        self._svc.calls.append(("permissions.create", kw))
        return _Request(lambda: {"id": "perm"})


def fake_facade() -> tuple[DriveFacade, FakeDriveService]:
    """A real DriveFacade over a fresh fake service."""
    svc = FakeDriveService()
    return DriveFacade(svc), svc
