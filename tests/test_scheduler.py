"""The scheduler (services/scheduler.py): one pass, the loop, and when it runs."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from api_deejaytools import main
from api_deejaytools.config import Settings, get_settings
from api_deejaytools.services import drive_jobs, scheduler, session_tick, song_builds

# --- fakes ----------------------------------------------------------------------


class FakeSession:
    def __init__(self, n: int) -> None:
        self.n = n
        self.closed = False


class FakeMaker:
    """Hands out numbered sessions, so each step's session can be told apart."""

    def __init__(self) -> None:
        self.made: list[FakeSession] = []

    def __call__(self) -> Any:
        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[FakeSession]:
            s = FakeSession(len(self.made))
            self.made.append(s)
            try:
                yield s
            finally:
                s.closed = True

        return cm()


class Logs:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, m: str, *a: Any, **k: Any) -> None:
        self.lines.append(("info", m))

    def warning(self, m: str, *a: Any, **k: Any) -> None:
        self.lines.append(("warning", m))

    def error(self, m: str, *a: Any, **k: Any) -> None:
        self.lines.append(("error", m))


class Sentry:
    def __init__(self) -> None:
        self.captured: list[tuple[BaseException, dict[str, str]]] = []
        self.tags: dict[str, str] = {}

    @contextlib.contextmanager
    def new_scope(self) -> Iterator[Any]:
        outer = self

        class S:
            def set_level(self, level: str) -> None:
                outer.tags["level"] = level

            def set_tag(self, k: str, v: str) -> None:
                outer.tags[k] = v

        yield S()

    def capture_exception(self, exc: BaseException) -> None:
        self.captured.append((exc, dict(self.tags)))


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the four steps with recorders; each can be told to raise."""
    state: dict[str, Any] = {"calls": [], "raise": set(), "maker": FakeMaker()}

    def step(name: str) -> Any:
        async def run(db: FakeSession) -> int:
            state["calls"].append((name, db.n))
            if name in state["raise"]:
                raise RuntimeError(f"{name} broke")
            return 0

        return run

    monkeypatch.setattr(session_tick, "tick_session_statuses", step("statuses"))
    monkeypatch.setattr(session_tick, "fill_running_sessions", step("fill"))
    monkeypatch.setattr(song_builds, "process_song_uploads", step("builds"))
    monkeypatch.setattr(drive_jobs, "process_drive_jobs", step("drive"))
    monkeypatch.setattr(scheduler, "_session_factory", lambda: state["maker"])
    state["logs"] = Logs()
    state["sentry"] = Sentry()
    monkeypatch.setattr(scheduler, "logger", state["logs"])
    monkeypatch.setattr(scheduler, "sentry_sdk", state["sentry"])
    return state


# --- run_tick -------------------------------------------------------------------


async def test_tick_runs_sessions_then_drive_each_in_its_own_session(
    steps: dict[str, Any],
) -> None:
    await scheduler.run_tick()
    assert steps["calls"] == [("statuses", 0), ("fill", 1), ("builds", 2), ("drive", 3)]
    assert all(s.closed for s in steps["maker"].made)


async def test_session_failure_does_not_stop_drive_work(steps: dict[str, Any]) -> None:
    steps["raise"].add("statuses")
    await scheduler.run_tick()
    # As deejaytools-api: the session steps share one try block.
    assert [c[0] for c in steps["calls"]] == ["statuses", "builds", "drive"]
    errors = [m for lvl, m in steps["logs"].lines if lvl == "error"]
    assert len(errors) == 1 and "tick_failed" in errors[0]
    assert "drive_jobs_tick_failed" not in errors[0]
    assert steps["sentry"].captured == []


async def test_drive_failure_is_logged_and_reported(steps: dict[str, Any]) -> None:
    steps["raise"].add("drive")
    await scheduler.run_tick()
    assert [c[0] for c in steps["calls"]] == ["statuses", "fill", "builds", "drive"]
    errors = [m for lvl, m in steps["logs"].lines if lvl == "error"]
    assert len(errors) == 1 and "drive_jobs_tick_failed" in errors[0]
    ((exc, tags),) = steps["sentry"].captured
    assert str(exc) == "drive broke"
    assert tags == {"level": "error", "subsystem": "drive_jobs"}


async def test_build_failure_is_isolated_and_reported(steps: dict[str, Any]) -> None:
    steps["raise"].add("builds")
    await scheduler.run_tick()
    assert [c[0] for c in steps["calls"]] == ["statuses", "fill", "builds", "drive"]
    errors = [m for lvl, m in steps["logs"].lines if lvl == "error"]
    assert len(errors) == 1 and "song_builds_tick_failed" in errors[0]
    ((exc, tags),) = steps["sentry"].captured
    assert str(exc) == "builds broke"
    assert tags == {"level": "error", "subsystem": "song_builds"}


async def test_both_halves_failing_never_raises(steps: dict[str, Any]) -> None:
    steps["raise"].update({"fill", "builds", "drive"})
    await scheduler.run_tick()
    assert [c[0] for c in steps["calls"]] == ["statuses", "fill", "builds", "drive"]


# --- the loop -------------------------------------------------------------------


async def test_overlap_guard_skips_a_tick_while_one_runs() -> None:
    gate = asyncio.Event()
    runs = 0

    async def slow() -> None:
        nonlocal runs
        runs += 1
        await gate.wait()

    s = scheduler.Scheduler(1000, tick=slow)
    first = asyncio.create_task(s.tick_once())
    await asyncio.sleep(0)
    assert await s.tick_once() is False
    gate.set()
    assert await first is True
    assert await s.tick_once() is True
    assert runs == 2


async def test_loop_ticks_at_start_and_on_the_interval_without_overlap() -> None:
    gate = asyncio.Event()
    starts = 0

    async def tick() -> None:
        nonlocal starts
        starts += 1
        await gate.wait()

    s = scheduler.Scheduler(10, tick=tick)
    s.start()
    await asyncio.sleep(0.005)
    assert starts == 1  # the startup pass, without waiting an interval
    await asyncio.sleep(0.06)
    assert starts == 1  # several intervals passed; all skipped while it ran
    gate.set()
    await asyncio.sleep(0.05)
    assert starts >= 2
    await s.stop()
    assert not s.started
    after = starts
    await asyncio.sleep(0.05)
    assert starts == after


async def test_stop_waits_briefly_then_cancels_a_hung_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler, "SHUTDOWN_GRACE_SECONDS", 0.05)
    cancelled = asyncio.Event()

    async def hung() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    s = scheduler.Scheduler(1000, tick=hung)
    s.start()
    await asyncio.sleep(0.01)
    await asyncio.wait_for(s.stop(), 1)
    assert cancelled.is_set()


async def test_stop_lets_a_quick_tick_finish() -> None:
    finished = asyncio.Event()

    async def quick() -> None:
        await asyncio.sleep(0.02)
        finished.set()

    s = scheduler.Scheduler(1000, tick=quick)
    s.start()
    await asyncio.sleep(0.005)
    await s.stop()
    assert finished.is_set()


async def test_default_tick_is_run_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = asyncio.Event()

    async def fake() -> None:
        ran.set()

    monkeypatch.setattr(scheduler, "run_tick", fake)
    assert await scheduler.Scheduler(1000).tick_once() is True
    assert ran.is_set()


# --- when it runs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "disabled"),
    [("1", True), ("0", False), ("true", False), ("", False), (" 1", False)],
)
def test_disable_scheduler_is_read_as_deejaytools_api_reads_it(
    monkeypatch: pytest.MonkeyPatch, raw: str, disabled: bool
) -> None:
    monkeypatch.setenv("DISABLE_SCHEDULER", raw)
    assert Settings().DISABLE_SCHEDULER is disabled


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISABLE_SCHEDULER", raising=False)
    monkeypatch.delenv("TICK_INTERVAL_MS", raising=False)
    monkeypatch.delenv("TICK_SECRET", raising=False)
    s = Settings(_env_file=None)
    assert (s.DISABLE_SCHEDULER, s.TICK_INTERVAL_MS, s.TICK_SECRET) == (
        False,
        30000,
        None,
    )


def _settings(**over: Any) -> Settings:
    s = get_settings().model_copy()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_enabled_only_outside_tests_and_without_the_flag() -> None:
    assert scheduler.scheduler_enabled(
        _settings(ENVIRONMENT="production", DISABLE_SCHEDULER=False)
    )
    assert not scheduler.scheduler_enabled(
        _settings(ENVIRONMENT="production", DISABLE_SCHEDULER=True)
    )
    assert not scheduler.scheduler_enabled(
        _settings(ENVIRONMENT="test", DISABLE_SCHEDULER=False)
    )


def test_start_scheduler_returns_none_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Any) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr(scheduler.Scheduler, "start", boom)
    assert scheduler.start_scheduler(_settings(DISABLE_SCHEDULER=True)) is None
    assert scheduler.start_scheduler(_settings(ENVIRONMENT="test")) is None


async def test_app_lifespan_never_starts_the_loop_under_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(self: Any) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr(scheduler.Scheduler, "start", boom)
    assert get_settings().ENVIRONMENT == "test"
    async with main.app.router.lifespan_context(main.app):
        pass


async def test_app_lifespan_starts_and_stops_the_loop_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "DISABLE_SCHEDULER", False)
    monkeypatch.setattr(settings, "TICK_INTERVAL_MS", 60_000)
    ticks = 0

    async def fake_tick() -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(scheduler, "run_tick", fake_tick)
    started: list[scheduler.Scheduler] = []
    real_start = scheduler.Scheduler.start

    def spy(self: scheduler.Scheduler) -> None:
        started.append(self)
        real_start(self)

    monkeypatch.setattr(scheduler.Scheduler, "start", spy)
    async with main.app.router.lifespan_context(main.app):
        await asyncio.sleep(0.01)
        assert len(started) == 1 and started[0].started
        assert started[0].interval_ms == 60_000
    assert ticks == 1
    assert not started[0].started
