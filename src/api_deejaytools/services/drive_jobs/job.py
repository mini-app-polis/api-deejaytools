"""A claimed ``drive_jobs`` row as the runners see it, and the refusal a
runner raises for a job that retrying cannot make succeed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Job:
    id: str
    kind: str
    submission_id: str | None
    file_id: str | None
    attempts: int
    row_keys: tuple[str, ...]
    next_attempt_at: int
    event_id: str | None = None
    user_id: str | None = None


class DriveJobRefused(Exception):
    """A job that retrying cannot make succeed: it is failed at once, with
    this as its ``last_error``. The admin retry runs it again once the cause
    is fixed."""


def _missing_field(job: _Job, field: str) -> RuntimeError:
    return RuntimeError(
        f"{job.kind} job {job.id} has no {field} (row keys: {','.join(job.row_keys)})"
    )
