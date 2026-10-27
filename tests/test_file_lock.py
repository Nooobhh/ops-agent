import multiprocessing
import time

import pytest

from lib.file_lock import FileLock


def _hold_lock(path, acquired, seconds, ready):
    # 等对手进程就绪再加锁,否则 spawn 慢的平台上对手的计时起点会落在持锁区间内
    ready.wait(30)
    with FileLock(path, timeout=5):
        acquired.set()
        time.sleep(seconds)


def _measure_lock(path, acquired, output, ready):
    ready.set()
    acquired.wait(5)
    started = time.monotonic()
    with FileLock(path, timeout=5):
        output.put(time.monotonic() - started)


def _timeout_lock(path, acquired, output, ready):
    ready.set()
    acquired.wait(5)
    try:
        with FileLock(path, timeout=0.5):
            output.put("acquired")
    except TimeoutError:
        output.put("timeout")


def test_nested_different_locks_and_parent_creation(tmp_path):
    first = tmp_path / "missing" / "first.lock"
    second = tmp_path / "other" / "second.lock"
    with FileLock(str(first)):
        with FileLock(str(second)):
            assert first.exists()
            assert second.exists()


def test_second_process_waits_until_first_releases(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    ready = ctx.Event()
    output = ctx.Queue()
    path = str(tmp_path / "shared.lock")
    holder = ctx.Process(target=_hold_lock, args=(path, acquired, 1.2, ready))
    waiter = ctx.Process(target=_measure_lock, args=(path, acquired, output, ready))
    holder.start()
    waiter.start()
    holder.join(5)
    waiter.join(5)
    assert holder.exitcode == waiter.exitcode == 0
    assert output.get(timeout=1) >= 1.0


def test_second_process_times_out(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    ready = ctx.Event()
    output = ctx.Queue()
    path = str(tmp_path / "timeout.lock")
    holder = ctx.Process(target=_hold_lock, args=(path, acquired, 1.2, ready))
    waiter = ctx.Process(target=_timeout_lock, args=(path, acquired, output, ready))
    holder.start()
    waiter.start()
    holder.join(5)
    waiter.join(5)
    assert holder.exitcode == waiter.exitcode == 0
    assert output.get(timeout=1) == "timeout"


def test_timeout_error_in_current_process(tmp_path):
    with FileLock(str(tmp_path / "one.lock")):
        with FileLock(str(tmp_path / "two.lock"), timeout=0.1):
            pass
