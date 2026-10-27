import asyncio
import json
from contextlib import suppress

import pytest

import lib.scheduler as scheduler_module
from lib.acquisition.settings import load_settings, save_settings
from lib.scheduler import Scheduler


def _write_gate(state_dir, enabled):
    state_dir.mkdir()
    state_dir.joinpath("poll-enabled.json").write_text(json.dumps({
        "enabled": enabled,
        "updated_at": "2026-07-24T00:00:00+00:00",
        "updated_by": "web",
    }), encoding="utf-8")
    config_dir = state_dir.parent / "config"
    config_dir.mkdir()
    config_dir.joinpath("alert-sources.yaml").write_text("version: 1\n", encoding="utf-8")
    settings = load_settings(state_dir)
    settings["resources"]["enabled"] = False
    settings["maintenance"]["enabled"] = False
    save_settings(state_dir, settings)


@pytest.mark.parametrize("enabled", [True, False])
def test_start_only_obeys_poll_gate(tmp_path, monkeypatch, enabled):
    state_dir = tmp_path / "state"
    _write_gate(state_dir, enabled)
    monkeypatch.setattr(
        scheduler_module,
        "load_status",
        lambda _path: {"effective": enabled},
    )

    async def scenario():
        scheduler = Scheduler(state_dir, tmp_path)
        await scheduler.start()
        assert not hasattr(scheduler, "_credential_refresh_task")
        assert (scheduler._poll_task is not None) is enabled
        await scheduler.stop()

    asyncio.run(scenario())


def test_stop_cancels_poll_task_and_clears_refs(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _write_gate(state_dir, True)
    monkeypatch.setattr(
        scheduler_module,
        "load_status",
        lambda _path: {"effective": True},
    )

    async def scenario():
        scheduler = Scheduler(state_dir, tmp_path)
        await scheduler.start()
        task = scheduler._poll_task
        await scheduler.stop()
        assert task is not None and task.cancelled()
        assert scheduler._poll_task is None
        assert scheduler.is_poll_running() is False

    asyncio.run(scenario())


def test_disable_poll_really_cancels_running_task(tmp_path, monkeypatch):
    async def scenario():
        scheduler = Scheduler(tmp_path / "state", tmp_path)
        started = asyncio.Event()

        async def running_loop(*, startup_delay):
            assert startup_delay == 0
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(scheduler, "_poll_loop", running_loop)
        scheduler.enable_poll()
        task = scheduler._poll_task
        await started.wait()
        scheduler.disable_poll()
        with suppress(asyncio.CancelledError):
            await task
        assert task is not None and task.cancelled()
        assert scheduler.is_poll_running() is False

    asyncio.run(scenario())


def test_enable_poll_starts_immediately_and_is_idempotent(tmp_path, monkeypatch):
    async def scenario():
        scheduler = Scheduler(tmp_path / "state", tmp_path)
        delays = []
        started = asyncio.Event()

        async def running_loop(*, startup_delay):
            delays.append(startup_delay)
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(scheduler, "_poll_loop", running_loop)
        scheduler.enable_poll()
        first_task = scheduler._poll_task
        scheduler.enable_poll()
        await started.wait()
        assert scheduler.is_poll_running() is True
        assert scheduler._poll_task is first_task
        assert delays == [0]
        scheduler.disable_poll()
        with suppress(asyncio.CancelledError):
            await first_task

    asyncio.run(scenario())


def test_disable_then_enable_replaces_task_without_leak(tmp_path, monkeypatch):
    async def scenario():
        scheduler = Scheduler(tmp_path / "state", tmp_path)

        async def running_loop(*, startup_delay):
            assert startup_delay == 0
            await asyncio.Event().wait()

        monkeypatch.setattr(scheduler, "_poll_loop", running_loop)
        scheduler.enable_poll()
        first_task = scheduler._poll_task
        await asyncio.sleep(0)
        scheduler.disable_poll()
        scheduler.enable_poll()
        second_task = scheduler._poll_task
        await asyncio.sleep(0)
        assert first_task is not second_task
        assert first_task is not None and first_task.cancelled()
        assert second_task is not None and not second_task.done()
        scheduler.disable_poll()
        with suppress(asyncio.CancelledError):
            await second_task

    asyncio.run(scenario())


def test_poll_loop_skips_tick_when_gate_is_off(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _write_gate(state_dir, False)
    calls = []
    monkeypatch.setattr(
        scheduler_module,
        "load_status",
        lambda _path: {"effective": False},
    )
    monkeypatch.setattr(
        scheduler_module.alert_poll,
        "run",
        lambda argv: calls.append(argv),
    )

    async def stop_at_next_interval(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", stop_at_next_interval)

    async def scenario():
        scheduler = Scheduler(state_dir, tmp_path)
        with pytest.raises(asyncio.CancelledError):
            await scheduler._poll_loop(startup_delay=0)

    asyncio.run(scenario())
    assert calls == []


def test_poll_loop_runs_tick_with_contract_argv(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    _write_gate(state_dir, True)
    calls = []
    monkeypatch.setattr(
        scheduler_module,
        "load_status",
        lambda _path: {"effective": True},
    )
    monkeypatch.setattr(
        scheduler_module.alert_poll,
        "run",
        lambda argv: calls.append(argv),
    )

    async def stop_at_next_interval(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", stop_at_next_interval)

    async def scenario():
        scheduler = Scheduler(state_dir, tmp_path)
        with pytest.raises(asyncio.CancelledError):
            await scheduler._poll_loop(startup_delay=0)

    asyncio.run(scenario())
    assert calls == [[
        "--state-dir", str(state_dir),
        "--repo-root", str(tmp_path),
    ]]


def test_alert_poll_compatibility_removes_unsupported_repo_root(monkeypatch):
    monkeypatch.delattr(scheduler_module.alert_poll, "run")  # 撤掉 conftest 的 tick 硬闸,测兼容层本身
    calls = []
    monkeypatch.setattr(
        scheduler_module._alert_poll_script,
        "run",
        lambda argv: calls.append(argv) or 0,
    )

    result = scheduler_module.alert_poll.run([
        "--state-dir", "/tmp/state",
        "--repo-root", "/tmp/repo",
    ])

    assert result == 0
    assert calls == [["--state-dir", "/tmp/state"]]


def test_scheduler_has_no_credential_refresh_loop(tmp_path):
    scheduler = Scheduler(tmp_path / "state", tmp_path)

    assert not hasattr(scheduler, "_credential_refresh_loop")
    assert not hasattr(scheduler, "_next_credential_refresh")


def test_weekly_graph_loop_runs_only_for_weekly_graphs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    state_dir = tmp_path / "state"
    _write_gate(state_dir, False)
    monkeypatch.setattr(scheduler_module, "load_status", lambda _path: {"effective": False})
    monkeypatch.setattr(scheduler_module, "_seconds_until_weekly", lambda *_args: 0)
    calls = []

    def manager(graph_id, refresh):
        return SimpleNamespace(decl=SimpleNamespace(id=graph_id, refresh=refresh),
                               trigger=lambda mode: calls.append((graph_id, mode)), stop=lambda: None)

    async def scenario():
        scheduler = Scheduler(state_dir, tmp_path)
        scheduler.attach_graphs((manager("jobs", "weekly"), manager("manual", None)))
        await scheduler.start()
        tasks = list(scheduler._graph_tasks)
        assert [task.get_name() for task in tasks] == ["graph_jobs_weekly_loop"]
        for _ in range(3):
            await asyncio.sleep(0)
        await scheduler.stop()
        assert all(task.done() for task in tasks)

    asyncio.run(scenario())
    assert calls and set(calls) == {("jobs", "full")}
