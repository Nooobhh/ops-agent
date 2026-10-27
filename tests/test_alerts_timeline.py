import json
import sqlite3
from pathlib import Path

import pytest

from lib.alerts import timeline as timeline_module
from lib.alerts.timeline import occurrences, project, prune, rebuild


KEY = "prometheus:alpha:disk_full:hd01"


def event(ts, type_, key=KEY, **extra):
    row = {
        "ts": ts,
        "type": type_,
        "key": key,
        "source": "prometheus",
        "platform": "alpha",
        "alertname": "disk_full",
        "host": "hd01",
        "class": "watch",
        "detail": {},
    }
    row.update(extra)
    return row


def row_count(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _create_v1_events_table(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                key TEXT NOT NULL,
                source TEXT NOT NULL,
                platform TEXT NOT NULL,
                alertname TEXT NOT NULL,
                host TEXT,
                alert_class TEXT,
                detail TEXT NOT NULL,
                suppressed_by TEXT
            )
        """)


def test_occurrences_returns_closed_and_open_intervals(tmp_path):
    db = tmp_path / "timeline.db"
    rows = [
        event("2026-01-01T00:00:00+00:00", "new"),
        event("2026-01-02T00:00:00+00:00", "resolved"),
        event("2026-01-03T00:00:00+00:00", "new"),
    ]
    project(db, rows)

    assert occurrences(db, KEY) == [
        {"started_at": "2026-01-01T00:00:00+00:00", "resolved_at": "2026-01-02T00:00:00+00:00"},
        {"started_at": "2026-01-03T00:00:00+00:00", "resolved_at": None},
    ]


def test_project_is_idempotent_for_the_same_rows(tmp_path):
    db = tmp_path / "timeline.db"
    rows = [event("2026-01-01T00:00:00+00:00", "new")]

    assert project(db, rows) == 1
    assert project(db, rows) == 0
    assert row_count(db) == 1


def test_event_id_v1_is_stable_when_storage_fields_are_added():
    original = event("2026-01-01T00:00:00+00:00", "new")
    enriched = {**original, "severity": "critical", "service": "HDFS"}

    assert timeline_module._normalize_row(original)[0] == \
        timeline_module._normalize_row(enriched)[0]


def test_existing_schema_upgrade_and_reprojection_do_not_duplicate(tmp_path):
    db = tmp_path / "timeline.db"
    original = event("2026-01-01T00:00:00+00:00", "new")
    normalized = timeline_module._normalize_row(original)
    _create_v1_events_table(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO events
               (event_id, ts, type, key, source, platform, alertname, host,
                alert_class, detail, suppressed_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            normalized[:11],
        )

    assert project(db, [{**original, "severity": "critical", "service": "HDFS"}]) == 0
    assert row_count(db) == 1
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        stored = conn.execute(
            "SELECT severity, service FROM events WHERE event_id = ?", (normalized[0],),
        ).fetchone()
    assert {"severity", "service"} <= columns
    assert stored == (None, None)


def test_new_events_store_severity_and_service(tmp_path):
    db = tmp_path / "timeline.db"
    project(db, [event(
        "2026-01-01T00:00:00+00:00", "new",
        severity="critical", service="HDFS",
    )])

    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT severity, service FROM events").fetchone()
    assert stored == ("critical", "HDFS")


def test_rebuild_old_jsonl_leaves_new_columns_null(tmp_path):
    root = tmp_path / "alerts"
    root.mkdir()
    root.joinpath("2026-01-01.jsonl").write_text(
        json.dumps(event("2026-01-01T00:00:00+00:00", "new")) + "\n",
        encoding="utf-8",
    )
    db = tmp_path / "timeline.db"

    assert rebuild(db, root) == 1
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT severity, service FROM events").fetchone()
    assert stored == (None, None)


def test_prune_keeps_old_unresolved_occurrence_start(tmp_path):
    db = tmp_path / "timeline.db"
    started = "2025-04-01T00:00:00+00:00"
    project(db, [event(started, "new")])

    assert prune(db, "2025-04-11T00:00:00+00:00") == 0
    assert occurrences(db, KEY) == [{"started_at": started, "resolved_at": None}]


def test_prune_keeps_old_start_when_occurrence_resolved_after_cutoff(tmp_path):
    db = tmp_path / "timeline.db"
    started = "2025-04-01T00:00:00+00:00"
    resolved = "2025-07-07T00:00:00+00:00"
    project(db, [event(started, "new"), event(resolved, "resolved")])

    assert prune(db, "2025-04-11T00:00:00+00:00") == 0
    assert occurrences(db, KEY) == [{"started_at": started, "resolved_at": resolved}]


def test_prune_deletes_entire_closed_occurrence_before_cutoff(tmp_path):
    db = tmp_path / "timeline.db"
    project(db, [
        event("2025-04-01T00:00:00+00:00", "new"),
        event("2025-04-06T00:00:00+00:00", "triaged"),
        event("2025-04-07T00:00:00+00:00", "resolved"),
    ])

    assert prune(db, "2025-04-11T00:00:00+00:00") == 3
    assert occurrences(db, KEY) == []


def test_rebuild_matches_incremental_projection_and_replaces_corrupt_db(tmp_path):
    rows = [
        event("2026-01-01T00:00:00+00:00", "new"),
        event("2026-01-02T00:00:00+00:00", "resolved"),
    ]
    expected_db = tmp_path / "incremental.db"
    project(expected_db, rows)
    root = tmp_path / "alerts"
    root.mkdir()
    (root / "2026-01-01.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    rebuilt_db = tmp_path / "rebuilt.db"
    rebuilt_db.write_bytes(b"not a sqlite database")

    assert rebuild(rebuilt_db, root) == len(rows)
    assert occurrences(rebuilt_db, KEY) == occurrences(expected_db, KEY)
    assert row_count(rebuilt_db) == row_count(expected_db)


def test_all_timeline_connections_are_closed_before_rebuild_replace(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    real_replace = timeline_module.os.replace
    connections = []

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    def windows_style_replace(source, destination):
        assert all(conn.closed for conn in connections)
        real_replace(source, destination)

    monkeypatch.setattr(timeline_module.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(timeline_module.os, "replace", windows_style_replace)

    db = tmp_path / "timeline.db"
    rows = [event("2026-01-01T00:00:00+00:00", "new")]
    project(db, rows)
    occurrences(db, KEY)
    prune(db, "2025-01-01T00:00:00+00:00")
    root = tmp_path / "alerts"
    root.mkdir()
    (root / "2026-01-01.jsonl").write_text(
        json.dumps(rows[0]) + "\n", encoding="utf-8",
    )

    assert rebuild(db, root) == 1
    assert connections
    assert all(conn.closed for conn in connections)


def test_rebuild_cleanup_failure_does_not_mask_replace_error(tmp_path, monkeypatch):
    db = tmp_path / "timeline.db"
    root = tmp_path / "alerts"
    root.mkdir()
    real_unlink = Path.unlink

    def fail_replace(source, destination):
        raise PermissionError("replace failed")

    def fail_temp_cleanup(path, *args, **kwargs):
        if ".rebuild-" in path.name:
            raise PermissionError("cleanup failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(timeline_module.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)

    with pytest.raises(PermissionError, match="replace failed"):
        rebuild(db, root)


def test_project_normalizes_timestamps_to_utc_before_occurrence_ordering(tmp_path):
    db = tmp_path / "timeline.db"
    project(db, [
        event("2026-01-01T00:30:00+01:00", "new"),
        event("2026-01-01T00:00:00+00:00", "resolved"),
    ])

    assert occurrences(db, KEY) == [{
        "started_at": "2025-12-31T23:30:00+00:00",
        "resolved_at": "2026-01-01T00:00:00+00:00",
    }]


def test_prune_removes_old_keyless_and_orphan_rows_but_keeps_open_occurrence(tmp_path):
    db = tmp_path / "timeline.db"
    orphan_key = "prometheus:alpha:orphan:hd02"
    open_key = "prometheus:alpha:still_open:hd03"
    project(db, [
        event("2025-01-01T00:00:00+00:00", "new"),
        event("2025-01-02T00:00:00+00:00", "resolved"),
        event("2025-01-03T00:00:00+00:00", "notified", key=""),
        event("2025-01-04T00:00:00+00:00", "source_error", key=""),
        event("2025-01-05T00:00:00+00:00", "resolved", key=orphan_key),
        event("2025-01-06T00:00:00+00:00", "new", key=open_key),
    ])

    assert prune(db, "2025-04-01T00:00:00+00:00") == 5
    conn = sqlite3.connect(db)
    try:
        remaining = conn.execute("SELECT type, key FROM events").fetchall()
    finally:
        conn.close()
    assert remaining == [("new", open_key)]
