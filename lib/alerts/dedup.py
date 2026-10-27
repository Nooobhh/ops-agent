"""Declarative cross-source alert deduplication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


@dataclass(frozen=True)
class DedupRules:
    version: int
    rules: tuple


def load_rules(path) -> DedupRules:
    path = Path(path)
    if not path.exists():
        return DedupRules(version=1, rules=())
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        return DedupRules(version=1, rules=())
    rules = tuple(rule for rule in (data.get("rules") or []) if isinstance(rule, Mapping))
    return DedupRules(version=int(data.get("version", 1)), rules=rules)


def apply(rules: DedupRules, active_entries, *, window_seconds: int = 300) -> dict:
    """Return loser-key to winner-key mappings for simultaneously active pairs."""
    entries = list(_iter_entries(active_entries))
    result = {}
    for rule in rules.rules:
        # match 写两个来源类型各自的选择器,winner 指定保留哪一侧;另一侧同位置同时段的告警被抑制
        match = {str(source).lower(): selector or {} for source, selector in (rule.get("match") or {}).items()}
        winner_source = str(rule.get("winner") or "").lower()
        if len(match) != 2 or winner_source not in match:
            continue
        loser_source = next(source for source in match if source != winner_source)
        winners = [item for item in entries if _matches(item[1], winner_source, match[winner_source])]
        losers = [item for item in entries if _matches(item[1], loser_source, match[loser_source])]
        for loser_key, loser in losers:
            for winner_key, winner in winners:
                if not _same_location(loser, winner):
                    continue
                if not _within_pair_window(loser, winner, window_seconds):
                    continue
                result[loser_key] = winner_key
                break
    return result


def _iter_entries(active_entries) -> Iterable[tuple]:
    if isinstance(active_entries, Mapping):
        for key, entry in active_entries.items():
            yield str(key), _event_data(entry)
        return
    for index, entry in enumerate(active_entries or []):
        event = _event_data(entry)
        key = str(getattr(entry, "key", "") or event.get("key") or index)
        yield key, event


def _event_data(entry) -> dict:
    if isinstance(entry, Mapping):
        if isinstance(entry.get("event"), Mapping):
            return dict(entry["event"])
        return dict(entry)
    data = {}
    for field in (
        "source", "platform", "alertname", "host", "host_raw", "service", "state",
        "source_active_at",
    ):
        if hasattr(entry, field):
            data[field] = getattr(entry, field)
    return data


def _matches(event: Mapping[str, Any], source: str, selector: Mapping[str, Any]) -> bool:
    if str(event.get("source") or "").lower() != source:
        return False
    if str(event.get("platform") or "unknown").lower() == "unknown":
        return False
    for field, expected in selector.items():
        if field == "port":
            if _port(event) != int(expected):
                return False
            continue
        if field == "host":
            actual = event.get("host") or event.get("host_raw")
        else:
            actual = event.get(field)
        if str(actual or "").lower() != str(expected).lower():
            return False
    return True


def _same_location(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_platform = str(left.get("platform") or "unknown").lower()
    right_platform = str(right.get("platform") or "unknown").lower()
    if left_platform != "unknown" and right_platform != "unknown" and left_platform != right_platform:
        return False
    left_host = str(left.get("host") or left.get("host_raw") or "").lower()
    right_host = str(right.get("host") or right.get("host_raw") or "").lower()
    return not left_host or not right_host or left_host == right_host


def _within_pair_window(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    window_seconds: int,
) -> bool:
    left_ts = _parse_source_ts(left.get("source_active_at"))
    right_ts = _parse_source_ts(right.get("source_active_at"))
    if left_ts is None or right_ts is None:
        return True
    return abs((left_ts - right_ts).total_seconds()) <= int(window_seconds)


def _parse_source_ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _port(event: Mapping[str, Any]):
    if event.get("port") is not None:
        try:
            return int(event["port"])
        except (TypeError, ValueError):
            return None
    raw = str(event.get("host_raw") or "")
    if ":" not in raw:
        return None
    value = raw.rsplit(":", 1)[1]
    return int(value) if value.isdigit() else None
