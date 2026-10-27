"""Default and operator-overridden resource alert thresholds."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping

import yaml

from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock
from lib.envpack import load_env_pack


OVERRIDE_FILENAME = "resource-alert-settings.json"


class ResourceThresholdError(ValueError):
    pass


_GROUP_META = {
    "cpu": ("CPU 使用率", "最近 24 小时窗口内的节点 CPU 持续负载判定。"),
    "memory": ("可用内存", "按 MemAvailable 百分比判断节点内存压力。"),
    "filesystem": ("磁盘容量趋势", "按挂载点预计触线时间判断磁盘容量风险。"),
    "jvm": ("JVM GC 后 Old Gen", "按 GC 后 Old Gen 使用率和趋势判断 JVM 堆风险。"),
}

_FIELDS = {
    "cpu": (
        ("warning_pct", "警告阈值", "%", "CPU P95 达到该值时进入警告。", 0, 100),
        ("critical_pct", "严重阈值", "%", "最近 3 个采集点都达到该值时进入严重。", 0, 100),
    ),
    "memory": (
        ("warning_available_pct", "警告阈值", "%", "可用内存低于该值时进入警告。", 0, 100),
        ("critical_available_pct", "严重阈值", "%", "可用内存低于该值时进入严重。", 0, 100),
    ),
    "filesystem": (
        ("default_target_pct", "普通挂载点触线", "%", "普通本地挂载点预计达到该使用率时触线。", 0, 100),
        ("root_target_pct", "根目录触线", "%", "根目录预计达到该使用率时触线。", 0, 100),
        ("boot_target_pct", "/boot 触线", "%", "/boot 预计达到该使用率时触线。", 0, 100),
        ("warning_eta_hours", "警告提前量", "小时", "预计在该时间内触线时进入警告。", 0, 8760),
        ("critical_eta_hours", "严重提前量", "小时", "预计在该时间内触线时进入严重。", 0, 8760),
    ),
    "jvm": (
        ("warning_heap_used_pct", "Heap 使用率警告阈值", "%", "当前 Heap 使用率达到该值时进入现状告警。", 0, 100),
        ("critical_heap_used_pct", "Heap 使用率严重阈值", "%", "当前 Heap 使用率达到该值时进入严重现状告警。", 0, 100),
        ("warning_after_gc_pct", "警告阈值", "%", "GC 后 Old Gen 达到该值时进入警告。", 0, 100),
        ("critical_after_gc_pct", "严重阈值", "%", "GC 后 Old Gen 达到该值时进入严重。", 0, 100),
        ("warning_eta_hours", "警告提前量", "小时", "按趋势预计在该时间内达到危险线时进入警告。", 0, 8760),
        ("critical_eta_hours", "严重提前量", "小时", "按趋势预计在该时间内达到危险线时进入严重。", 0, 8760),
    ),
}

_BUSINESS_FIELDS = (
    ("warning_above", "警告阈值", "", "当前值达到该值时进入警告；留空表示未配置。", 0, None),
    ("critical_above", "严重阈值", "", "当前值达到该值时进入严重；留空表示未配置。", 0, None),
)


def load_resource_config(repo_root: Path) -> dict:
    path = load_env_pack(Path(repo_root)).config_file("resource-metrics.yaml")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def load_overrides(state_dir: Path) -> dict:
    path = Path(state_dir) / OVERRIDE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = value.get("overrides") if isinstance(value, Mapping) else {}
    return raw if isinstance(raw, Mapping) else {}


def resource_alert_settings(repo_root: Path, state_dir: Path) -> dict:
    config = load_resource_config(repo_root)
    overrides = normalize_overrides(config, load_overrides(state_dir))
    groups = []

    risk_defaults = config.get("risk_defaults") or {}
    for group_key, fields in _FIELDS.items():
        defaults = risk_defaults.get(group_key) or {}
        groups.append({
            "key": group_key,
            "kind": "resource",
            "label": _GROUP_META[group_key][0],
            "description": _GROUP_META[group_key][1],
            "fields": [
                _field_payload(
                    field_key, label, unit, description,
                    defaults.get(field_key),
                    _get_path(overrides, ("risk_defaults", group_key, field_key)),
                    ("risk_defaults", group_key, field_key),
                )
                for field_key, label, unit, description, _minimum, _maximum in fields
            ],
        })

    for profile_key, profile in sorted((config.get("business_metrics") or {}).items()):
        service_label = str(profile.get("service") or profile_key)
        for metric_key, metric in sorted((profile.get("metrics") or {}).items()):
            metric = metric or {}
            group_key = f"business:{profile_key}:{metric_key}"
            groups.append({
                "key": group_key,
                "kind": "business",
                "label": f"{service_label} · {metric.get('label') or metric_key}",
                "description": f"业务指标 {metric_key} 的风险阈值。",
                "metric": metric_key,
                "unit": metric.get("unit") or "number",
                "fields": [
                    _field_payload(
                        field_key, label, unit or metric.get("unit") or "number", description,
                        metric.get(field_key),
                        _get_path(overrides, ("business_metrics", profile_key, "metrics", metric_key, field_key)),
                        ("business_metrics", profile_key, "metrics", metric_key, field_key),
                    )
                    for field_key, label, unit, description, _minimum, _maximum in _BUSINESS_FIELDS
                ],
            })

    return {
        "schema_version": 1,
        "groups": groups,
        "overrides": overrides,
    }


def save_overrides(repo_root: Path, state_dir: Path, raw_overrides: Mapping[str, Any]) -> dict:
    config = load_resource_config(repo_root)
    overrides = normalize_overrides(config, raw_overrides)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / OVERRIDE_FILENAME
    with FileLock(str(state_dir / f"{OVERRIDE_FILENAME}.lock"), timeout=10):
        atomic_write_json(path, {"schema_version": 1, "overrides": overrides})
    return overrides


def merge_overrides(config: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict:
    result = copy.deepcopy(dict(config))
    normalized = normalize_overrides(result, overrides)
    _deep_merge(result, normalized)
    return result


def normalize_overrides(config: Mapping[str, Any], raw: Mapping[str, Any] | None) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    result = {}
    for path, minimum, maximum in _allowed_fields(config):
        value = _get_path(raw, path)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ResourceThresholdError(f"资源阈值 {'.'.join(path)} 必须是数字") from exc
        if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
            bound = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
            raise ResourceThresholdError(f"资源阈值 {'.'.join(path)} 必须在 {bound} 范围内")
        _set_path(result, path, int(number) if number.is_integer() else number)
    return result


def _allowed_fields(config: Mapping[str, Any]):
    risk_defaults = config.get("risk_defaults") or {}
    for group_key, fields in _FIELDS.items():
        defaults = risk_defaults.get(group_key) or {}
        for field_key, _label, _unit, _description, minimum, maximum in fields:
            if field_key in defaults:
                yield ("risk_defaults", group_key, field_key), minimum, maximum
    for profile_key, profile in (config.get("business_metrics") or {}).items():
        for metric_key, metric in (profile.get("metrics") or {}).items():
            metric = metric or {}
            for field_key, _label, _unit, _description, minimum, maximum in _BUSINESS_FIELDS:
                yield ("business_metrics", profile_key, "metrics", metric_key, field_key), minimum, maximum


def _field_payload(field_key, label, unit, description, default, override, path):
    return {
        "key": field_key,
        "label": label,
        "unit": unit,
        "description": description,
        "default": default,
        "override": override,
        "effective": default if override is None else override,
        "path": list(path),
    }


def _get_path(value: Mapping[str, Any], path):
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _set_path(target: dict, path, value) -> None:
    current = target
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


def _deep_merge(target: dict, source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)

