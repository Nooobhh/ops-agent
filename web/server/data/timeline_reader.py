"""Read-only queries over the rebuildable alert timeline projection."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.alerts.timeline import occurrences, open_occurrences
from lib.envpack import env_timezone


_FILTER_COLUMNS = {
    "source": "source",
    "platform": "platform",
    "host": "host",
    "severity": "severity",
    "service": "service",
    "type": "type",
}

CHANGE_TYPES = ("new", "resolved", "escalated")
_FACET_COLUMNS = ("source", "platform", "severity", "service")
logger = logging.getLogger(__name__)


def query_events(db_path: Path, *, key=None, filters=None, limit=None, offset=None) -> list:
    path = Path(db_path)
    if not path.exists():
        return []
    clauses = []
    params = []
    if key is not None:
        clauses.append("key = ?")
        params.append(str(key))
    for name, value in (filters or {}).items():
        if value is None or value == "":
            continue
        if name == "from":
            clauses.append("ts >= ?")
            params.append(_as_datetime(value).isoformat())
        elif name == "to":
            clauses.append("ts <= ?")
            params.append(_as_datetime(value).isoformat())
        elif name in _FILTER_COLUMNS:
            clauses.append(f"{_FILTER_COLUMNS[name]} = ?")
            params.append(str(value))
    sql = "SELECT rowid, * FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC, rowid DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
        if offset is not None:
            sql += " OFFSET ?"
            params.append(max(0, int(offset)))
    elif offset is not None:
        sql += " LIMIT -1 OFFSET ?"
        params.append(max(0, int(offset)))
    try:
        with closing(_connect_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("query_events", path, exc)
        return []
    return [_row_dict(row) for row in rows]


def open_keys(db_path: Path) -> set:
    return set(open_key_details(db_path))


def open_key_details(db_path: Path) -> dict:
    return open_occurrences(Path(db_path))


def range_bounds(range_key: str, *, now: datetime) -> tuple[datetime, datetime]:
    local_now = _as_datetime(now).astimezone(env_timezone())
    if range_key == "24h":
        current = local_now.replace(minute=0, second=0, microsecond=0)
        start = current - timedelta(hours=23)
        end = current + timedelta(hours=1)
    elif range_key in {"7d", "30d"}:
        count = 7 if range_key == "7d" else 30
        current = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = current - timedelta(days=count - 1)
        end = current + timedelta(days=1)
    else:
        raise ValueError(f"unsupported range: {range_key}")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def recent_changes(db_path: Path, *, since, until,
                   limit: int | None = None) -> tuple[list[dict], int]:
    path = Path(db_path)
    if not path.is_file():
        return [], 0
    params = [*CHANGE_TYPES, _as_datetime(since).isoformat(), _as_datetime(until).isoformat()]
    sql = """
        WITH ranked AS (
            SELECT rowid, *,
                   ROW_NUMBER() OVER (
                       PARTITION BY key ORDER BY ts DESC, rowid DESC
                   ) AS rn,
                   COUNT(*) OVER (PARTITION BY key) AS change_count,
                   SUM(CASE WHEN type = 'new' THEN 1 ELSE 0 END)
                       OVER (PARTITION BY key) AS occurrence_count
            FROM events
            WHERE key <> '' AND type IN (?, ?, ?) AND ts >= ? AND ts < ?
        )
        SELECT ranked.*, COUNT(*) OVER () AS folded_total
        FROM ranked
        WHERE rn = 1
        ORDER BY ts DESC, rowid DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    try:
        with closing(_connect_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("recent_changes", path, exc)
        return [], 0
    total = int(rows[0]["folded_total"]) if rows else 0
    items = []
    for row in rows:
        item = _row_dict(row)
        item.pop("rn", None)
        item.pop("folded_total", None)
        items.append(item)
    return items, total


def distinct_field_values(db_path: Path, *, fields, since, until) -> dict[str, set[str]]:
    wanted = [
        field for field in dict.fromkeys(fields)
        if field in _FACET_COLUMNS
    ]
    result = {field: set() for field in wanted}
    path = Path(db_path)
    if not path.is_file() or not wanted:
        return result
    selections = [
        f"""
        SELECT '{field}' AS field, TRIM({field}) AS value FROM recent
        WHERE {field} IS NOT NULL AND TRIM({field}) <> ''
        GROUP BY TRIM({field})
        """
        for field in wanted
    ]
    sql = f"""
        WITH recent AS (
            SELECT {', '.join(wanted)} FROM events WHERE ts >= ? AND ts < ?
        )
        {' UNION ALL '.join(selections)}
    """
    params = (_as_datetime(since).isoformat(), _as_datetime(until).isoformat())
    try:
        with closing(_connect_readonly(path)) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("distinct_field_values", path, exc)
        return result
    for field, value in rows:
        result[field].add(value)
    return result


def distribution(db_path: Path, *, range_key: str) -> list:
    path = Path(db_path)
    if not path.exists():
        return []
    start, end = range_bounds(range_key, now=datetime.now(timezone.utc))
    local_start = start.astimezone(env_timezone())
    if range_key == "24h":
        unit = "hour"
        count = 24
        starts = [local_start + timedelta(hours=index) for index in range(count)]
    else:
        unit = "day"
        count = 7 if range_key == "7d" else 30
        starts = [local_start + timedelta(days=index) for index in range(count)]

    rows = query_events(path, filters={"type": "new", "from": starts[0], "to": end})
    buckets = {
        start: {"bucket": start.isoformat(), "count": 0, "by_severity": {}}
        for start in starts
    }
    first_new_by_key = {}
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        current = first_new_by_key.get(key)
        if current is None or row["ts"] < current["ts"]:
            first_new_by_key[key] = row
    for row in first_new_by_key.values():
        parsed = _as_datetime(row["ts"]).astimezone(env_timezone())
        bucket_start = parsed.replace(
            minute=0, second=0, microsecond=0,
        ) if unit == "hour" else parsed.replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        bucket = buckets.get(bucket_start)
        if bucket is None:
            continue
        bucket["count"] += 1
        severity = row.get("severity")
        if severity:
            by_severity = bucket["by_severity"]
            by_severity[severity] = by_severity.get(severity, 0) + 1
    return list(buckets.values())


def count_new_today(db_path: Path, *, now) -> int:
    return _count_today(db_path, now=now, clause="type = 'new'")


def count_resolved_today(db_path: Path, *, now) -> int:
    return _count_today(db_path, now=now, clause="type = 'resolved'")


def count_suppressed_today(db_path: Path, *, now) -> int:
    return len(suppressed_keys_today(db_path, now=now))


def suppressed_keys_today(db_path: Path, *, now) -> set:
    path = Path(db_path)
    if not path.exists():
        return set()
    start, end = _beijing_day_bounds(now)
    try:
        with closing(_connect_readonly(path)) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT key FROM events
                WHERE suppressed_by IS NOT NULL AND ts >= ? AND ts < ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("suppressed_keys_today", path, exc)
        return set()
    return {row[0] for row in rows if row[0]}


def _count_today(db_path: Path, *, now, clause: str) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    start, end = _beijing_day_bounds(now)
    try:
        with closing(_connect_readonly(path)) as conn:
            return int(conn.execute(
                f"""
                SELECT COUNT(DISTINCT key) FROM events
                WHERE key <> '' AND {clause} AND ts >= ? AND ts < ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0])
    except sqlite3.DatabaseError as exc:
        _log_database_error("count_today", path, exc)
        return 0


def timeline_available(db_path: Path) -> bool:
    return timeline_health(db_path)["status"] == "ok"


def timeline_health(db_path: Path) -> dict:
    path = Path(db_path)
    if not path.is_file():
        return {"status": "unavailable", "detail": "时间线数据库不存在"}
    try:
        with closing(_connect_readonly(path)) as conn:
            conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        _log_database_error("timeline_health", path, exc)
        code = getattr(exc, "sqlite_errorcode", None)
        recovery_codes = {
            getattr(sqlite3, "SQLITE_READONLY_RECOVERY", -1),
            getattr(sqlite3, "SQLITE_READONLY_ROLLBACK", -2),
        }
        journal = path.with_name(path.name + "-journal")
        if journal.exists() and code in recovery_codes:
            return {
                "status": "recovering",
                "detail": "时间线正在恢复中，请稍候（下次轮询将自动修复）",
            }
        return {"status": "unavailable", "detail": "时间线数据库不可用"}
    return {"status": "ok", "detail": None}


def latest_event_at(db_path: Path) -> str | None:
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        with closing(_connect_readonly(path)) as conn:
            row = conn.execute(
                "SELECT ts FROM events ORDER BY ts DESC, rowid DESC LIMIT 1"
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        _log_database_error("latest_event_at", path, exc)
        return None
    return row[0] if row else None


def latest_source_errors(db_path: Path) -> dict:
    path = Path(db_path)
    if not path.is_file():
        return {}
    try:
        with closing(_connect_readonly(path)) as conn:
            rows = conn.execute(
                """
                SELECT key, ts, detail FROM events
                WHERE type = 'source_error'
                ORDER BY ts DESC, rowid DESC
                """
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("latest_source_errors", path, exc)
        return {}
    result = {}
    for key, ts, detail in rows:
        if key and key not in result:
            result[key] = {"ts": ts, "detail": detail}
    return result


def query_historical_summaries(db_path: Path, *, filters=None, exclude_keys=None,
                               limit=None, offset=0) -> tuple[list, int]:
    path = Path(db_path)
    if not path.is_file():
        return [], 0
    filters = filters or {}
    clauses = [
        "key <> ''",
        "type IN ('new', 'resolved', 'escalated', 'triaged', 'triage_failed')",
    ]
    params = []
    for name in ("from", "to"):
        value = filters.get(name)
        if not value:
            continue
        operator = ">=" if name == "from" else "<="
        clauses.append(f"ts {operator} ?")
        params.append(_as_datetime(value).isoformat())
    outer = ["latest.rn = 1"]
    for name in ("source", "platform", "host", "severity", "service"):
        value = filters.get(name)
        if value:
            outer.append(f"latest.{name} = ?")
            params.append(str(value))
    excluded = sorted(str(key) for key in (exclude_keys or set()))
    if excluded:
        outer.append("latest.key NOT IN (" + ",".join("?" for _ in excluded) + ")")
        params.extend(excluded)

    ctes = f"""
        WITH ranked AS (
            SELECT rowid, *, ROW_NUMBER() OVER (
                PARTITION BY key ORDER BY ts DESC, rowid DESC
            ) AS rn
            FROM events WHERE {' AND '.join(clauses)}
        ), latest_suppressed AS (
            SELECT key, suppressed_by, ROW_NUMBER() OVER (
                PARTITION BY key ORDER BY ts DESC, rowid DESC
            ) AS rn
            FROM events WHERE suppressed_by IS NOT NULL
        ), lifecycle AS (
            SELECT key,
                   MAX(CASE WHEN type = 'new' THEN ts END) AS last_new,
                   SUM(CASE WHEN type = 'new' THEN 1 ELSE 0 END) AS occurrence_count
            FROM events GROUP BY key
        )
    """
    joins = """
        FROM ranked AS latest
        LEFT JOIN latest_suppressed AS suppression
          ON suppression.key = latest.key AND suppression.rn = 1
        LEFT JOIN lifecycle ON lifecycle.key = latest.key
    """
    where = " WHERE " + " AND ".join(outer)
    try:
        with closing(_connect_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            total = int(conn.execute(
                ctes + "SELECT COUNT(*) " + joins + where, params,
            ).fetchone()[0])
            sql = ctes + """
                SELECT latest.rowid, latest.event_id, latest.ts, latest.type,
                       latest.key, latest.source, latest.platform, latest.alertname,
                       latest.host, latest.alert_class, latest.detail,
                       latest.suppressed_by, latest.severity, latest.service,
                       suppression.suppressed_by AS historical_suppressed_by,
                       lifecycle.last_new, lifecycle.occurrence_count
            """ + joins + where + """
                ORDER BY COALESCE(lifecycle.last_new, latest.ts) DESC,
                         latest.rowid DESC
            """
            row_params = list(params)
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                row_params.extend([max(0, int(limit)), max(0, int(offset))])
            rows = conn.execute(sql, row_params).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("query_historical_summaries", path, exc)
        return [], 0
    return [_row_dict(row) for row in rows], total


def historical_missing_fields(db_path: Path, *, filters=None, exclude_keys=None,
                              fields=None) -> set:
    path = Path(db_path)
    wanted = [
        name for name in ("severity", "service")
        if name in set(fields or ())
    ]
    if not path.is_file() or not wanted:
        return set()
    filters = filters or {}
    clauses = [
        "key <> ''",
        "type IN ('new', 'resolved', 'escalated', 'triaged', 'triage_failed')",
    ]
    params = []
    for name in ("from", "to"):
        value = filters.get(name)
        if not value:
            continue
        clauses.append(f"ts {'>=' if name == 'from' else '<='} ?")
        params.append(_as_datetime(value).isoformat())
    outer = ["latest.rn = 1"]
    for name in ("source", "platform", "host"):
        value = filters.get(name)
        if value:
            outer.append(f"latest.{name} = ?")
            params.append(str(value))
    excluded = sorted(str(key) for key in (exclude_keys or set()))
    if excluded:
        outer.append("latest.key NOT IN (" + ",".join("?" for _ in excluded) + ")")
        params.extend(excluded)
    aggregates = ", ".join(
        f"MAX(CASE WHEN latest.{name} IS NULL THEN 1 ELSE 0 END)"
        for name in wanted
    )
    sql = f"""
        WITH ranked AS (
            SELECT rowid, *, ROW_NUMBER() OVER (
                PARTITION BY key ORDER BY ts DESC, rowid DESC
            ) AS rn
            FROM events WHERE {' AND '.join(clauses)}
        )
        SELECT {aggregates} FROM ranked AS latest
        WHERE {' AND '.join(outer)}
    """
    try:
        with closing(_connect_readonly(path)) as conn:
            row = conn.execute(sql, params).fetchone()
    except sqlite3.DatabaseError as exc:
        _log_database_error("historical_missing_fields", path, exc)
        return set()
    return {
        name for name, missing in zip(wanted, row or ()) if missing
    }


def latest_events_for_keys(db_path: Path, keys, *, filters=None) -> dict:
    path = Path(db_path)
    wanted = sorted({str(key) for key in keys if key})
    if not path.is_file() or not wanted:
        return {}
    clauses = ["key IN (" + ",".join("?" for _ in wanted) + ")"]
    params = list(wanted)
    for name in ("from", "to"):
        value = (filters or {}).get(name)
        if value:
            clauses.append(f"ts {'>=' if name == 'from' else '<='} ?")
            params.append(_as_datetime(value).isoformat())
    sql = f"""
        WITH ranked AS (
            SELECT rowid, *, ROW_NUMBER() OVER (
                PARTITION BY key ORDER BY ts DESC, rowid DESC
            ) AS rn
            FROM events WHERE {' AND '.join(clauses)}
        )
        SELECT rowid, * FROM ranked WHERE rn = 1
    """
    try:
        with closing(_connect_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("latest_events_for_keys", path, exc)
        return {}
    result = {}
    for row in rows:
        item = _row_dict(row)
        item.pop("rn", None)
        result[item["key"]] = item
    return result


def latest_suppressions_for_keys(db_path: Path, keys) -> dict:
    path = Path(db_path)
    wanted = sorted({str(key) for key in keys if key})
    if not path.is_file() or not wanted:
        return {}
    sql = f"""
        WITH ranked AS (
            SELECT key, suppressed_by, ROW_NUMBER() OVER (
                PARTITION BY key ORDER BY ts DESC, rowid DESC
            ) AS rn
            FROM events
            WHERE suppressed_by IS NOT NULL
              AND key IN ({','.join('?' for _ in wanted)})
        )
        SELECT key, suppressed_by FROM ranked WHERE rn = 1
    """
    try:
        with closing(_connect_readonly(path)) as conn:
            rows = conn.execute(sql, wanted).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("latest_suppressions_for_keys", path, exc)
        return {}
    return {key: winner for key, winner in rows}


def suppression_peers(db_path: Path, key: str) -> dict:
    path = Path(db_path)
    if not path.is_file():
        return {"suppressed_by": None, "suppresses": []}
    try:
        with closing(_connect_readonly(path)) as conn:
            rows = conn.execute(
                """
                SELECT key, suppressed_by FROM events
                WHERE (key = ? AND suppressed_by IS NOT NULL)
                   OR suppressed_by = ?
                ORDER BY ts DESC, rowid DESC
                """,
                (str(key), str(key)),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        _log_database_error("suppression_peers", path, exc)
        return {"suppressed_by": None, "suppresses": []}
    winner = next((row[1] for row in rows if row[0] == key), None)
    losers = sorted({row[0] for row in rows if row[1] == key and row[0] != key})
    return {"suppressed_by": winner, "suppresses": losers}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _log_database_error(operation: str, path: Path, exc: sqlite3.DatabaseError) -> None:
    logger.debug(
        "timeline read degraded: operation=%s exception=%s path=%s",
        operation,
        type(exc).__name__,
        path.resolve(),
        exc_info=exc,
    )


def _beijing_day_bounds(now) -> tuple[datetime, datetime]:
    local_now = _as_datetime(now).astimezone(env_timezone())
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _row_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    result.pop("rowid", None)
    detail = result.get("detail")
    if isinstance(detail, str):
        try:
            result["detail"] = json.loads(detail)
        except json.JSONDecodeError:
            pass
    return result


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
