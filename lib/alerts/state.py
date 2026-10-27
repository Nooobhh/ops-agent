"""Alert active/resolved state and single-tick lock."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.atomic_file import atomic_write_json

from .model import AlertEvent
from .source_notify import event_source_id


@dataclass
class DeltaItem:
    key: str
    event: AlertEvent
    entry: Dict[str, Any]
    resurfaced: bool = False


@dataclass
class TickDelta:
    new: List[DeltaItem]
    still: List[DeltaItem]
    resolved: List[DeltaItem]


class AlertsState:
    def __init__(self, path: Path, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        self.path = Path(path)
        self.version = int(data.get("version", 1))
        self.active: Dict[str, Dict[str, Any]] = dict(data.get("active") or {})
        self.resolved_recent: Dict[str, Any] = dict(data.get("resolved_recent") or {})
        self.chronic_groups: Dict[str, Dict[str, Any]] = dict(
            data.get("chronic_groups") or {}
        )
        self.patrol_last_scanned = str(data.get("patrol_last_scanned") or "")
        self.source_fail: Dict[str, int] = dict(data.get("source_fail") or {})
        self.last_tick_ms: Dict[str, int] = dict(data.get("last_tick_ms") or {})
        self.last_tick_at: Optional[str] = data.get("last_tick_at")
        self.upgraded_at: Optional[str] = data.get("upgraded_at")
        self.retrigger_suppress_minutes = int(data.get("retrigger_suppress_minutes", 30))
        self._tick_at: Optional[str] = None

    @classmethod
    def load(cls, path) -> "AlertsState":
        path = Path(path)
        if not path.exists():
            return cls(path)
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        atomic_write_json(self.path, self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "active": self.active,
            "resolved_recent": self.resolved_recent,
            "chronic_groups": self.chronic_groups,
            "patrol_last_scanned": self.patrol_last_scanned,
            "source_fail": self.source_fail,
            "last_tick_ms": self.last_tick_ms,
            "last_tick_at": self.last_tick_at,
            "upgraded_at": self.upgraded_at,
        }

    def transition(self, events: List[AlertEvent], now, failed_sources=None) -> TickDelta:
        now_dt = _ensure_datetime(now)
        now_iso = now_dt.isoformat()
        self._tick_at = now_iso
        self.last_tick_at = now_iso
        self._cleanup_resolved(now_dt)
        failed_sources = set(failed_sources or set())

        new: List[DeltaItem] = []
        still: List[DeltaItem] = []
        resolved: List[DeltaItem] = []
        by_key = {event.key: event for event in events}

        for key, event in by_key.items():
            if key in self.active:
                entry = self.active[key]
                entry["last_seen"] = now_iso
                entry["severity"] = event.severity
                entry["occurrences"] = int(entry.get("occurrences", 0)) + 1
                entry["event"] = asdict(event)
                still.append(DeltaItem(key, event, entry))
                continue
            resurfaced = self._is_resurfaced(key, now_dt)
            entry = self._restore_resolved_entry(key, event, now_iso) if resurfaced \
                else _new_entry(event, now_iso)
            self.active[key] = entry
            if key in self.resolved_recent and not resurfaced:
                self.resolved_recent.pop(key, None)
            new.append(DeltaItem(key, event, entry, resurfaced=resurfaced))

        for key, entry in list(self.active.items()):
            if key in by_key:
                continue
            event = _event_from_entry(entry)
            if entry.get("event", {}).get("source") == "patrol":
                last_seen = _parse_iso(entry.get("last_seen"))
                if last_seen and now_dt - last_seen > timedelta(hours=24):
                    self.active.pop(key, None)
                continue
            if _event_source_id(event) in failed_sources:
                continue
            self.active.pop(key, None)
            self.resolved_recent[key] = {
                "resolved_at": now_iso,
                "entry": copy_entry(entry),
            }
            resolved.append(DeltaItem(key, event, entry))
        self._cleanup_chronic_groups(now_dt)
        return TickDelta(new=new, still=still, resolved=resolved)

    def select_triage_batch(self, limit) -> List[str]:
        tick = self._tick_at
        candidates = []
        for key, entry in self.active.items():
            triage = entry.get("triage") or {}
            status = triage.get("status")
            attempts = int(triage.get("attempts", 0))
            last_attempt = triage.get("last_attempt_at")
            if attempts >= 3 or status in {"done", "not_applicable", "failed"}:
                continue
            if last_attempt and tick and last_attempt >= tick:
                continue
            if status == "pending":
                candidates.append((entry.get("first_seen", ""), key))
        return [key for _, key in sorted(candidates)[:int(limit)]]

    def mark_triage_attempt(self, key, ok, run_id=None, err=None) -> None:
        entry = self.active[key]
        triage = entry.setdefault("triage", {})
        attempts = int(triage.get("attempts", 0)) + 1
        triage["attempts"] = attempts
        triage["last_attempt_at"] = self._tick_at or datetime.now(timezone.utc).isoformat()
        if run_id is not None:
            triage["run_id"] = run_id
        if ok:
            triage["status"] = "done"
            triage.pop("error", None)
        else:
            triage["error"] = str(err or "")
            triage["status"] = "failed" if attempts >= 3 else "pending"

    def _cleanup_resolved(self, now_dt: datetime) -> None:
        suppress = timedelta(minutes=self.retrigger_suppress_minutes)
        for key, item in list(self.resolved_recent.items()):
            parsed = _parse_iso(_resolved_at(item))
            if parsed is None or now_dt - parsed > suppress:
                self.resolved_recent.pop(key, None)

    def _cleanup_chronic_groups(self, now: datetime) -> None:
        """Drop a chronic group only after every host left the resurfacing window."""
        now_dt = _ensure_datetime(now)
        suppress = timedelta(minutes=self.retrigger_suppress_minutes)
        active_groups = {
            _entry_group_key(entry)
            for entry in self.active.values()
        }
        recent_groups = set()
        for item in self.resolved_recent.values():
            if not isinstance(item, dict):
                continue
            resolved_at = _parse_iso(_resolved_at(item))
            entry = item.get("entry")
            if resolved_at is None or not isinstance(entry, dict):
                continue
            if now_dt - resolved_at <= suppress:
                recent_groups.add(_entry_group_key(entry))
        for group_key in list(self.chronic_groups):
            if group_key not in active_groups and group_key not in recent_groups:
                self.chronic_groups.pop(group_key, None)

    def _is_resurfaced(self, key: str, now_dt: datetime) -> bool:
        ts = _resolved_at(self.resolved_recent.get(key))
        parsed = _parse_iso(ts) if ts else None
        return bool(parsed and now_dt - parsed <= timedelta(minutes=self.retrigger_suppress_minutes))

    def _restore_resolved_entry(self, key: str, event: AlertEvent, now_iso: str) -> Dict[str, Any]:
        item = self.resolved_recent.get(key)
        if isinstance(item, dict) and isinstance(item.get("entry"), dict):
            entry = copy_entry(item["entry"])
        else:
            entry = _new_entry(event, now_iso)
        entry["last_seen"] = now_iso
        entry["severity"] = event.severity
        entry["occurrences"] = int(entry.get("occurrences", 0)) + 1
        entry["event"] = asdict(event)
        return entry


def acquire_lock(path, stale_minutes=15) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags)
    except FileExistsError:
        if not _lock_is_stale(path, stale_minutes):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            fd = os.open(str(path), flags)
        except FileExistsError:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    return True


def release_lock(path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def _new_entry(event: AlertEvent, now_iso: str) -> Dict[str, Any]:
    return {
        "first_seen": now_iso,
        "last_seen": now_iso,
        "severity": event.severity,
        "class": "watch",
        "triage": {
            "status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
            "run_id": None,
        },
        "notified_initial_at": None,
        "notified_diagnosis_at": None,
        "occurrences": 1,
        "event": asdict(event),
    }


def copy_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(entry, ensure_ascii=False))


def _resolved_at(item) -> Optional[str]:
    if isinstance(item, dict):
        return item.get("resolved_at")
    return item


def _event_from_entry(entry: Dict[str, Any]) -> AlertEvent:
    data = dict(entry.get("event") or {})
    return AlertEvent(
        source=data.get("source", "prometheus"),
        platform=data.get("platform", "unknown"),
        alertname=data.get("alertname", ""),
        severity=data.get("severity") or entry.get("severity") or "unknown",
        host=data.get("host"),
        host_raw=data.get("host_raw", ""),
        service=data.get("service", ""),
        state=data.get("state", "firing"),
        maintenance=bool(data.get("maintenance", False)),
        detail=data.get("detail", ""),
        source_active_at=data.get("source_active_at"),
        source_last_seen_at=data.get("source_last_seen_at"),
        source_resolved_at=data.get("source_resolved_at"),
    )


def _event_source_id(event: AlertEvent) -> str:
    return event_source_id(event.source, event.platform)


def _entry_group_key(entry: Dict[str, Any]) -> str:
    event = entry.get("event") or {}
    return (
        f"{event.get('source', 'prometheus')}:"
        f"{event.get('platform', 'unknown')}:"
        f"{event.get('alertname', '')}"
    )


def _lock_is_stale(path: Path, stale_minutes: int) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        started = _parse_iso(data.get("started_at"))
    except Exception:
        return True
    if started is None:
        return True
    return datetime.now(timezone.utc) - started > timedelta(minutes=stale_minutes)


def _ensure_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _ensure_datetime(value)
    except Exception:
        return None
