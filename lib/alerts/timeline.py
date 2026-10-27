"""SQLite projection and lifecycle queries for the alert event stream."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)


def project(db_path, rows) -> int:
    """Project event rows into SQLite and return the number newly inserted."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [_normalize_row(row) for row in rows]
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            _ensure_schema(conn)
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO events
                    (event_id, ts, type, key, source, platform, alertname, host,
                     alert_class, detail, suppressed_by, severity, service)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
            return conn.total_changes - before


def occurrences(db_path, key) -> list:
    """Return closed and open occurrences for an alert key in time order."""
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                "SELECT rowid, ts, type FROM events WHERE key = ?",
                (str(key),),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_read_error("occurrences", path, exc)
        return []
    return _pair_occurrences(rows)


def open_occurrences(db_path) -> dict:
    """Return open occurrence evidence for every key using the shared pairing logic."""
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                """
                SELECT rowid, ts, type, key FROM events
                WHERE key <> '' AND type IN ('new', 'resolved')
                """
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_read_error("open_occurrences", path, exc)
        return {}
    grouped = {}
    for rowid, ts, type_, key in rows:
        grouped.setdefault(key, []).append((rowid, ts, type_))
    result = {}
    for key, keyed_rows in grouped.items():
        intervals = _pair_occurrences(keyed_rows)
        if intervals and intervals[-1].get("resolved_at") is None:
            latest = max(keyed_rows, key=lambda row: (_as_datetime(row[1]), row[0]))
            result[key] = {
                "started_at": intervals[-1]["started_at"],
                "last_event_at": latest[1],
            }
    return result


def _pair_occurrences(rows) -> list:
    rows = sorted(rows, key=lambda row: (_as_datetime(row[1]), row[0]))

    result = []
    started_at = None
    for _, ts, type_ in rows:
        if type_ == "new":
            if started_at is not None:
                result.append({"started_at": started_at, "resolved_at": None})
            started_at = ts
        elif type_ == "resolved" and started_at is not None:
            result.append({"started_at": started_at, "resolved_at": ts})
            started_at = None
    if started_at is not None:
        result.append({"started_at": started_at, "resolved_at": None})
    return result


def _log_read_error(operation: str, path: Path, exc: sqlite3.DatabaseError) -> None:
    logger.debug(
        "timeline read degraded: operation=%s exception=%s path=%s",
        operation,
        type(exc).__name__,
        path.resolve(),
        exc_info=exc,
    )


def prune(db_path, cutoff_ts) -> int:
    """Delete only complete occurrences resolved before the retention cutoff."""
    path = Path(db_path)
    if not path.exists():
        return 0
    cutoff = _as_datetime(cutoff_ts)
    delete_ids = []
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            _ensure_schema(conn)
            keys = [row[0] for row in conn.execute("SELECT DISTINCT key FROM events")]
            for key in keys:
                rows = conn.execute(
                    "SELECT rowid, ts, type FROM events WHERE key = ?",
                    (key,),
                ).fetchall()
                rows.sort(key=lambda row: (_as_datetime(row[1]), row[0]))
                if not key:
                    delete_ids.extend(
                        rowid for rowid, ts, _ in rows if _as_datetime(ts) < cutoff
                    )
                    continue

                current = None
                for rowid, ts, type_ in rows:
                    if type_ == "new":
                        current = [rowid]
                        continue
                    if current is None:
                        if _as_datetime(ts) < cutoff:
                            delete_ids.append(rowid)
                        continue
                    current.append(rowid)
                    if type_ != "resolved":
                        continue
                    if _as_datetime(ts) < cutoff:
                        delete_ids.extend(current)
                    current = None
            if delete_ids:
                conn.executemany(
                    "DELETE FROM events WHERE rowid = ?",
                    ((rowid,) for rowid in delete_ids),
                )
            return len(delete_ids)


def rebuild(db_path, jsonl_root) -> int:
    """Atomically rebuild a projection from the append-only JSONL source."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.rebuild-{os.getpid()}")
    main_failed = False
    try:
        if temp.exists():
            temp.unlink()
        rows = []
        root = Path(jsonl_root)
        if root.exists():
            for jsonl_path in sorted(root.glob("*.jsonl")):
                with jsonl_path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if line.strip():
                            rows.append(json.loads(line))
        inserted = project(temp, rows)
        os.replace(temp, path)
        return inserted
    except BaseException:
        main_failed = True
        raise
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                if not main_failed:
                    raise


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
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
            suppressed_by TEXT,
            severity TEXT,
            service TEXT
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    for name in ("severity", "service"):
        if name not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_key_ts ON events(key, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_platform_ts ON events(platform, ts)")


def _normalize_row(row):
    data = dict(row or {})
    key = str(data.get("key") or "")
    parsed_source, parsed_platform, parsed_alertname, parsed_host = _key_fields(key)
    detail = data.get("detail", {})
    if isinstance(detail, str):
        detail_text = detail
    else:
        detail_text = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw_ts = data.get("ts")
    normalized_ts = _as_datetime(raw_ts).isoformat() if raw_ts else ""
    identity_values = (
        normalized_ts,
        str(data.get("type") or ""),
        key,
        str(data.get("source") or parsed_source),
        str(data.get("platform") or parsed_platform),
        str(data.get("alertname") or parsed_alertname),
        data.get("host") if data.get("host") is not None else parsed_host,
        str(data.get("alert_class") or data.get("class") or ""),
        detail_text,
        data.get("suppressed_by"),
    )
    digest = _event_id_v1(identity_values)
    return (digest,) + identity_values + (data.get("severity"), data.get("service"))


def _event_id_v1(identity_values) -> str:
    """Hash only the original v1 identity fields.

    Storage columns added after v1 must never be included here: doing so would
    give replayed historical events new IDs and break projection idempotency.
    """
    return hashlib.sha256(
        json.dumps(identity_values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _key_fields(key: str):
    parts = key.split(":", 3)
    if len(parts) != 4:
        return "", "", "", None
    return parts[0], parts[1], parts[2], parts[3] or None


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
