import multiprocessing
from datetime import datetime
from pathlib import Path

from lib.worklog import WorklogEntry, append_entry


def _append(root, event_key, title):
    append_entry(WorklogEntry(
        status="DONE", category="OPS", title=title, source=event_key,
        output="-", related="-", process=["一", "二", "三"],
        ts=datetime(2026, 7, 17, 12, 0),
    ), Path(root), event_key=event_key)


def test_concurrent_different_keys_preserve_both_entries(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_append, args=(tmp_path, f"auto:key:{n}", f"事件{n}"))
        for n in (1, 2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    content = (tmp_path / "2026-07-17.md").read_text(encoding="utf-8")
    assert content.count("## [DONE]") == 2
    assert "auto:key:1" in content and "auto:key:2" in content


def test_concurrent_same_key_writes_once(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_append, args=(tmp_path, "auto:same", f"事件{n}"))
        for n in (1, 2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    content = (tmp_path / "2026-07-17.md").read_text(encoding="utf-8")
    assert content.count("auto:same") == 1
    assert content.count("## [DONE]") == 1
