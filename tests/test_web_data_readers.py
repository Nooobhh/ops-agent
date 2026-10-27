import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState
from lib.alerts.timeline import rebuild
from lib.alerts import timeline as timeline_module
from web.server.data import timeline_reader
from web.server.data.chronic import chronic_keys, is_chronic
from web.server.data.state_reader import active_payload, load_active, load_source_fail
from web.server.data.timeline_reader import (
    count_new_today,
    count_resolved_today,
    count_suppressed_today,
    distribution,
    open_keys,
    query_events,
    suppressed_keys_today,
)


def _event(key, ts, type_, **extra):
    source, platform, alertname, host = key.split(":", 3)
    row = {
        "ts": ts,
        "type": type_,
        "key": key,
        "source": source,
        "platform": platform,
        "alertname": alertname,
        "host": host,
        "class": "watch",
        "detail": {},
    }
    row.update(extra)
    return row


def _timeline(tmp_path, rows):
    source = tmp_path / "jsonl"
    source.mkdir()
    source.joinpath("events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    db = tmp_path / "timeline.db"
    rebuild(db, source)
    return db


def _state(tmp_path):
    state_dir = tmp_path / "state"
    state = AlertsState(state_dir / "alerts.json")
    event = AlertEvent(
        source="prometheus", platform="alpha", alertname="disk_full",
        severity="critical", host="hd01", host_raw="hd01", service="HDFS",
        state="firing", detail={"summary": "disk is full"},
    )
    state.transition([event], datetime(2026, 7, 20, tzinfo=timezone.utc))
    state.source_fail = {"demo:beta": 2}
    state.save()
    return state_dir, event


def test_state_reader_uses_state_contract_and_returns_payload(tmp_path):
    state_dir, event = _state(tmp_path)

    active = load_active(state_dir)

    assert active[event.key]["severity"] == "critical"
    assert active[event.key]["event"]["service"] == "HDFS"
    assert active_payload(active[event.key]) == active[event.key]["event"]
    assert load_source_fail(state_dir) == {"demo:beta": 2}


def test_timeline_reader_queries_rebuilt_fixture(tmp_path):
    key = "prometheus:alpha:disk_full:hd01"
    db = _timeline(tmp_path, [
        _event(key, "2026-07-20T01:00:00+00:00", "new", severity="critical", service="HDFS"),
        _event(key, "2026-07-20T02:00:00+00:00", "resolved", severity="critical", service="HDFS"),
    ])

    rows = query_events(db, key=key)

    assert [row["type"] for row in rows] == ["resolved", "new"]
    assert rows[0]["severity"] == "critical"
    assert query_events(db, filters={"source": "prometheus", "service": "HDFS"}, limit=1) == rows[:1]


def test_open_keys_reuses_occurrence_pairing_semantics(tmp_path):
    open_once = "prometheus:alpha:open_once:hd01"
    closed = "prometheus:alpha:closed:hd02"
    reopened = "prometheus:alpha:reopened:hd03"
    db = _timeline(tmp_path, [
        _event(open_once, "2026-07-20T00:00:00+00:00", "new"),
        _event(closed, "2026-07-20T00:00:00+00:00", "new"),
        _event(closed, "2026-07-20T01:00:00+00:00", "resolved"),
        _event(reopened, "2026-07-20T00:00:00+00:00", "new"),
        _event(reopened, "2026-07-20T01:00:00+00:00", "resolved"),
        _event(reopened, "2026-07-20T02:00:00+00:00", "new"),
    ])

    assert open_keys(db) == {open_once, reopened}


def test_open_keys_never_runs_schema_ddl(tmp_path, monkeypatch):
    key = "prometheus:alpha:open_once:hd01"
    db = _timeline(tmp_path, [
        _event(key, "2026-07-20T00:00:00+00:00", "new"),
    ])

    def reject_schema_write(_conn):
        raise AssertionError("read path attempted schema DDL")

    monkeypatch.setattr(timeline_module, "_ensure_schema", reject_schema_write)

    assert open_keys(db) == {key}


def test_missing_sources_return_empty_values(tmp_path):
    state_dir = tmp_path / "missing-state"
    db = tmp_path / "missing.db"

    assert load_active(state_dir) == {}
    assert load_source_fail(state_dir) == {}
    assert active_payload({}) is None
    assert query_events(db) == []
    assert open_keys(db) == set()
    assert distribution(db, range_key="24h") == []
    assert count_new_today(db, now=datetime.now(timezone.utc)) == 0
    assert count_suppressed_today(db, now=datetime.now(timezone.utc)) == 0


def test_distribution_fills_all_24_hour_buckets(tmp_path, use_env_timezone):
    use_env_timezone("Etc/GMT-8")  # UTC+8,按环境时区切日
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = "prometheus:alpha:recent:hd01"
    db = _timeline(tmp_path, [
        _event(key, (now - timedelta(hours=2)).isoformat(), "new", severity="critical"),
    ])

    buckets = distribution(db, range_key="24h")

    assert len(buckets) == 24
    assert sum(bucket["count"] for bucket in buckets) == 1
    assert sum(bucket["by_severity"].get("critical", 0) for bucket in buckets) == 1
    assert any(bucket["count"] == 0 for bucket in buckets)
    assert {
        datetime.fromisoformat(bucket["bucket"]).utcoffset()
        for bucket in buckets
    } == {timedelta(hours=8)}


@pytest.mark.parametrize(
    ("range_key", "expected_start", "expected_end"),
    [
        (
            "24h",
            datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc),
        ),
        (
            "7d",
            datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc),
        ),
        (
            "30d",
            datetime(2026, 6, 20, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_range_bounds_aligns_to_env_tz_buckets(
        range_key, expected_start, expected_end, use_env_timezone):
    use_env_timezone("Etc/GMT-8")  # UTC+8,按环境时区切日
    now = datetime(2026, 7, 20, 10, 34, 56, tzinfo=timezone.utc)

    assert timeline_reader.range_bounds(range_key, now=now) == (
        expected_start,
        expected_end,
    )


def test_range_bounds_rejects_unknown_range():
    with pytest.raises(ValueError, match="unsupported range"):
        timeline_reader.range_bounds("1h", now=datetime.now(timezone.utc))


def test_is_chronic_includes_threshold_boundary_and_excludes_just_under():
    now = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)

    assert is_chronic(
        {"first_seen": "2026-07-19T10:00:00+00:00"},
        now=now,
        threshold_hours=24,
    ) is True
    assert is_chronic(
        {"first_seen": "2026-07-19T10:01:00+00:00"},
        now=now,
        threshold_hours=24,
    ) is False


@pytest.mark.parametrize("entry", [{}, {"first_seen": ""}, {"first_seen": "nope"}])
def test_is_chronic_invalid_first_seen_is_false(entry):
    assert is_chronic(
        entry,
        now=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
        threshold_hours=24,
    ) is False


def test_chronic_keys_filters_active_entries_and_applies_threshold():
    now = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    active = {
        "old": {"first_seen": "2026-07-19T10:00:00+00:00"},
        "new": {"first_seen": "2026-07-20T09:59:00+00:00"},
        "missing": {},
    }

    assert chronic_keys(active, now=now, threshold_hours=24) == {"old"}
    assert chronic_keys(active, now=now, threshold_hours=1 / 60) == {"old", "new"}


def test_recent_changes_folds_counts_sorts_filters_and_limits(tmp_path):
    repeated_key = "prometheus:alpha:flapping:hd01"
    second_key = "demo:alpha:datanode_down:hd02"
    outside_key = "demo:alpha:old:hd03"
    rows = [
        _event(
            repeated_key,
            (datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc)
             + timedelta(minutes=index)).isoformat(),
            "new" if index % 2 == 0 else "resolved",
            severity="warning",
        )
        for index in range(8)
    ]
    rows.extend([
        _event(
            second_key, "2026-07-20T03:00:00+00:00", "escalated",
            severity="critical",
        ),
        _event(repeated_key, "2026-07-20T04:00:00+00:00", "triaged"),
        _event("prometheus:alpha:source:hd04", "2026-07-20T05:00:00+00:00", "source_error"),
        _event(outside_key, "2026-07-19T23:59:59+00:00", "new"),
    ])
    db = _timeline(tmp_path, rows)

    items, total = timeline_reader.recent_changes(
        db,
        since=datetime(2026, 7, 20, tzinfo=timezone.utc),
        until="2026-07-21T00:00:00+00:00",
    )

    assert timeline_reader.CHANGE_TYPES == ("new", "resolved", "escalated")
    assert total == 2
    assert [item["key"] for item in items] == [second_key, repeated_key]
    assert items[0]["type"] == "escalated"
    assert items[1]["type"] == "resolved"
    assert items[1]["ts"] == "2026-07-20T01:07:00+00:00"
    assert items[1]["change_count"] == 8

    limited, limited_total = timeline_reader.recent_changes(
        db,
        since="2026-07-20T00:00:00+00:00",
        until="2026-07-21T00:00:00+00:00",
        limit=1,
    )
    assert [item["key"] for item in limited] == [second_key]
    assert limited_total == 2


def test_recent_changes_missing_or_corrupt_database_degrades(tmp_path):
    missing = tmp_path / "missing.db"
    corrupt = tmp_path / "timeline.db"
    corrupt.write_bytes(b"not sqlite")
    bounds = {
        "since": "2026-07-20T00:00:00+00:00",
        "until": "2026-07-21T00:00:00+00:00",
    }

    assert timeline_reader.recent_changes(missing, **bounds) == ([], 0)
    assert timeline_reader.recent_changes(corrupt, **bounds) == ([], 0)


def test_today_counts_use_env_tz_calendar_boundary(tmp_path, use_env_timezone):
    use_env_timezone("Etc/GMT-8")  # UTC+8,按环境时区切日
    db = _timeline(tmp_path, [
        _event(
            "prometheus:alpha:yesterday:hd01",
            "2026-07-19T15:30:00+00:00", "new",
            suppressed_by="demo:alpha:winner:hd01",
        ),
        _event(
            "prometheus:alpha:today:hd01",
            "2026-07-19T16:30:00+00:00", "new",
            suppressed_by="demo:alpha:winner:hd01",
        ),
    ])
    now = datetime(2026, 7, 19, 17, 0, tzinfo=timezone.utc)

    assert count_new_today(db, now=now) == 1
    assert suppressed_keys_today(db, now=now) == {
        "prometheus:alpha:today:hd01",
    }


def test_today_counts_count_each_alert_key_once(tmp_path):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    repeated = "prometheus:alpha:flapping:hd01"
    other = "demo:alpha:datanode_down:hd02"
    db = _timeline(tmp_path, [
        _event(repeated, "2026-07-20T01:00:00+00:00", "new"),
        _event(repeated, "2026-07-20T01:05:00+00:00", "resolved"),
        _event(repeated, "2026-07-20T01:10:00+00:00", "new"),
        _event(repeated, "2026-07-20T01:15:00+00:00", "resolved"),
        _event(other, "2026-07-20T02:00:00+00:00", "new"),
    ])

    assert count_new_today(db, now=now) == 2
    assert count_resolved_today(db, now=now) == 1


def test_distribution_counts_key_once_at_first_new_in_range(tmp_path):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    repeated = "prometheus:alpha:flapping:hd01"
    db = _timeline(tmp_path, [
        _event(
            repeated, (now - timedelta(hours=2)).isoformat(), "new",
            severity="critical",
        ),
        _event(
            repeated, (now - timedelta(hours=1)).isoformat(), "new",
            severity="warning",
        ),
    ])

    buckets = distribution(db, range_key="24h")

    assert sum(bucket["count"] for bucket in buckets) == 1
    assert sum(
        bucket["by_severity"].get("critical", 0) for bucket in buckets
    ) == 1
    assert sum(
        bucket["by_severity"].get("warning", 0) for bucket in buckets
    ) == 0


def test_today_counts_and_all_readers_leave_database_unchanged(tmp_path):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    key = "prometheus:alpha:today:hd01"
    db = _timeline(tmp_path, [
        _event(key, "2026-07-20T01:00:00+00:00", "new"),
        _event(key, "2026-07-20T02:00:00+00:00", "notified", suppressed_by="demo:alpha:today:hd01"),
        _event(key, "2026-07-19T23:00:00+00:00", "new"),
    ])
    before_stat = db.stat()
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()

    assert query_events(db, filters={"from": "2026-07-19T00:00:00+00:00"})
    assert open_keys(db) == {key}
    assert distribution(db, range_key="7d")
    assert count_new_today(db, now=now) == 1
    assert count_suppressed_today(db, now=now) == 1

    after_stat = db.stat()
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash


def test_database_degradation_logs_exception_type_and_path(tmp_path, caplog):
    db = tmp_path / "timeline.db"
    db.write_bytes(b"not a sqlite database")
    caplog.set_level(logging.DEBUG)

    assert query_events(db) == []
    assert count_new_today(db, now=datetime.now(timezone.utc)) == 0
    assert suppressed_keys_today(db, now=datetime.now(timezone.utc)) == set()

    assert str(db.resolve()) in caplog.text
    assert "DatabaseError" in caplog.text
