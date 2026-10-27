import gzip
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from lib.acquisition.store import (
    append_snapshot,
    iter_snapshots,
    prune_snapshots,
    seal_completed_days,
)
from lib.alerts.history import prune_event_logs


ZONE = ZoneInfo("Etc/GMT-8")


def _snapshot(snapshot_id, collected_at, platform="alpha"):
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "collected_at": collected_at,
        "platform": platform,
        "nodes": [],
        "services": [],
    }


def test_append_is_idempotent_and_seals_completed_day(tmp_path, use_env_timezone):
    use_env_timezone("Etc/GMT-8")  # 按环境时区切日
    root = tmp_path / "resources"
    row = _snapshot("one", "2026-08-27T23:55:00+08:00")
    append_snapshot(root, row)
    append_snapshot(root, row)

    assert root.joinpath("2026-08-27.jsonl").read_text().count("\n") == 1

    sealed = seal_completed_days(
        root, now=datetime(2026, 8, 28, 0, 5, tzinfo=ZONE), compression="gzip",
    )
    assert sealed == [root / "2026-08-27.jsonl.gz"]
    assert not root.joinpath("2026-08-27.jsonl").exists()
    with gzip.open(sealed[0], "rt", encoding="utf-8") as stream:
        assert json.loads(stream.readline())["snapshot_id"] == "one"
    assert [item["snapshot_id"] for item in iter_snapshots(root)] == ["one"]


def test_prune_removes_only_days_older_than_retention(tmp_path, use_env_timezone):
    use_env_timezone("Etc/GMT-8")  # 按环境时区切日
    root = tmp_path / "resources"
    append_snapshot(root, _snapshot("old", "2026-08-01T00:00:00+08:00"))
    append_snapshot(root, _snapshot("keep", "2026-08-20T00:00:00+08:00"))

    removed = prune_snapshots(
        root, retention_days=10,
        now=datetime(2026, 8, 28, 12, 0, tzinfo=ZONE),
    )

    assert removed == [root / "2026-08-01.jsonl"]
    assert root.joinpath("2026-08-20.jsonl").is_file()


def test_alert_history_keeps_old_day_containing_active_occurrence(tmp_path):
    root = tmp_path / "alerts"
    root.mkdir()
    active_key = "prometheus:alpha:disk:node1"
    old = root / "2026-01-01.jsonl"
    old.write_text(json.dumps({
        "ts": "2026-01-01T00:00:00+00:00", "type": "new", "key": active_key,
    }) + "\n", encoding="utf-8")
    closed = root / "2026-01-02.jsonl"
    closed.write_text(json.dumps({
        "ts": "2026-01-02T00:00:00+00:00", "type": "new", "key": "closed",
    }) + "\n", encoding="utf-8")

    removed = prune_event_logs(
        root, datetime(2026, 8, 1, tzinfo=ZONE), preserve_keys={active_key},
    )

    assert removed == 1
    assert old.is_file()
    assert not closed.exists()
