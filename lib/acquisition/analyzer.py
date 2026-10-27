"""Conservative local resource risk analysis over persisted snapshots."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lib.atomic_file import atomic_write_json

from .store import iter_snapshots


def analyze_resource_history(root: Path, config: dict, *, now=None) -> dict:
    now_dt = _as_datetime(now or datetime.now(timezone.utc))
    rows = list(iter_snapshots(root, since=now_dt - timedelta(days=7)))
    excluded_platforms = {
        str(value).strip().lower()
        for value in ((config.get("node") or {}).get("excluded_platforms") or [])
    }
    rows = [
        row for row in rows
        if str(row.get("platform") or "").lower() not in excluded_platforms
    ]
    findings = []
    findings.extend(_node_pressure(rows, config))
    findings.extend(_filesystem_eta(rows, config, now_dt))
    findings.extend(_jvm_pressure(rows, config))
    findings.extend(_business_risks(rows, config))
    forecast_status = _forecast_status(rows, config)
    for item in forecast_status:
        matching = [
            finding for finding in findings
            if finding.get("platform") == item["platform"]
            and finding.get("resource") == "filesystem"
        ]
        item["finding_count"] = len(matching)
        item["health"] = (
            "critical" if any(row.get("severity") == "critical" for row in matching)
            else "warning" if matching else "healthy"
        )
    return {
        "schema_version": 1,
        "generated_at": now_dt.isoformat(),
        "window_started_at": (now_dt - timedelta(days=7)).isoformat(),
        "snapshot_count": len(rows),
        "forecast_status": forecast_status,
        "findings": sorted(findings, key=lambda item: (
            {"critical": 0, "warning": 1, "watch": 2}.get(item["severity"], 3),
            item["key"],
        )),
    }


def save_risks(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)


def _node_pressure(rows, config):
    rules = config.get("risk_defaults") or {}
    cpu_cfg = rules.get("cpu") or {}
    mem_cfg = rules.get("memory") or {}
    node_series = defaultdict(list)
    for snapshot in _recent_rows(rows, hours=24):
        collected_at = _safe_datetime(snapshot.get("collected_at"))
        if collected_at is None:
            continue
        for node in snapshot.get("nodes") or []:
            key = (snapshot.get("platform"), node.get("node"))
            node_series[key].append((collected_at, node.get("metrics") or {}))
    findings = []
    min_samples = max(int(cpu_cfg.get("min_samples", 6)), int(mem_cfg.get("min_samples", 6)))
    for (platform, node), values in node_series.items():
        values.sort(key=lambda item: item[0])
        recent = values[-max(min_samples, 6):]
        if len(recent) < min_samples:
            continue
        cpu = [item[1].get("cpu_used_pct") for item in recent]
        cpu = [float(value) for value in cpu if value is not None]
        available = []
        for _, metrics in recent:
            total = metrics.get("memory_total_bytes")
            free = metrics.get("memory_available_bytes")
            if total and free is not None:
                available.append(100 * free / total)
        if len(cpu) >= min_samples:
            p95 = _percentile(cpu, 0.95)
            severity = None
            if min(cpu[-3:]) >= float(cpu_cfg.get("critical_pct", 90)):
                severity = "critical"
            elif p95 >= float(cpu_cfg.get("warning_pct", 80)):
                severity = "warning"
            if severity:
                findings.append(_finding(
                    f"resource_forecast:{platform}:cpu:{node}", severity,
                    platform, node, "cpu",
                    observed_at=[item[0] for item in recent],
                    p95=p95, samples=len(cpu),
                    warning_pct=float(cpu_cfg.get("warning_pct", 80)),
                    critical_pct=float(cpu_cfg.get("critical_pct", 90)),
                ))
        if len(available) >= min_samples:
            low = min(available)
            severity = None
            if max(available[-3:]) <= float(mem_cfg.get("critical_available_pct", 8)):
                severity = "critical"
            elif low <= float(mem_cfg.get("warning_available_pct", 15)):
                severity = "warning"
            if severity:
                findings.append(_finding(
                    f"resource_forecast:{platform}:memory:{node}", severity,
                    platform, node, "memory",
                    observed_at=[item[0] for item in recent], available_pct=low,
                    samples=len(available),
                    warning_available_pct=float(
                        mem_cfg.get("warning_available_pct", 15),
                    ),
                    critical_available_pct=float(
                        mem_cfg.get("critical_available_pct", 8),
                    ),
                ))
    return findings


def _filesystem_eta(rows, config, now_dt):
    fs_cfg = ((config.get("risk_defaults") or {}).get("filesystem") or {})
    min_hours = int(fs_cfg.get("min_hours", 12))
    series = defaultdict(list)
    for snapshot in rows:
        collected = _as_datetime(snapshot.get("collected_at"))
        for node in snapshot.get("nodes") or []:
            for fs in node.get("filesystems") or []:
                if fs.get("kind") != "local" or fs.get("readonly") not in {0, 0.0, None}:
                    continue
                available = fs.get("available_bytes")
                size = fs.get("size_bytes")
                if available is None or not size:
                    continue
                key = (
                    snapshot.get("platform"), node.get("node"),
                    fs.get("device"), fs.get("mountpoint"),
                )
                series[key].append((collected, float(available), float(size)))
    findings = []
    for key, values in series.items():
        hourly = _hourly_medians(values)
        if len(hourly) < min_hours:
            continue
        slopes = []
        for index, left in enumerate(hourly):
            for right in hourly[index + 1:]:
                seconds = (right[0] - left[0]).total_seconds()
                if seconds > 0:
                    slopes.append((right[1] - left[1]) / seconds)
        if not slopes:
            continue
        slope = statistics.median(slopes)
        if slope >= -1024 * 1024 / 3600:  # Ignore growth below 1 MiB/hour.
            continue
        platform, node, device, mountpoint = key
        latest_at, available, size = hourly[-1]
        target_pct = float(fs_cfg.get(
            "root_target_pct" if mountpoint == "/" else (
                "boot_target_pct" if mountpoint == "/boot" else "default_target_pct"
            ), 80,
        ))
        target_available = size * (1 - target_pct / 100)
        seconds = (target_available - available) / slope
        if seconds <= 0:
            eta_hours = 0.0
        else:
            eta_hours = seconds / 3600
        warning_hours = float(fs_cfg.get("warning_eta_hours", 168))
        critical_hours = float(fs_cfg.get("critical_eta_hours", 24))
        if eta_hours > warning_hours:
            continue
        severity = "critical" if eta_hours <= critical_hours else "warning"
        findings.append(_finding(
            f"resource_forecast:{platform}:disk:{node}:{mountpoint}", severity,
            platform, node, "filesystem", alert_type="forecast",
            device=device, mountpoint=mountpoint,
            observed_at=[item[0] for item in values],
            target_pct=target_pct, eta_hours=round(eta_hours, 2),
            slope_bytes_per_hour=round(slope * 3600, 2), samples=len(hourly),
            confidence="medium" if len(hourly) >= 24 else "low",
        ))
    return findings


def _jvm_pressure(rows, config):
    jvm_cfg = ((config.get("risk_defaults") or {}).get("jvm") or {})
    heap_series = defaultdict(list)
    after_gc_series = defaultdict(list)
    window_hours = int(jvm_cfg.get("trend_window_hours", 168))
    for snapshot in _recent_rows(rows, hours=window_hours):
        collected_at = _safe_datetime(snapshot.get("collected_at"))
        if collected_at is None:
            continue
        for service in snapshot.get("services") or []:
            key = (
                snapshot.get("platform"), service.get("node"), service.get("job"),
                service.get("role") or "",
            )
            metrics = service.get("metrics") or {}
            heap_used_pct = metrics.get("heap_used_pct")
            if heap_used_pct is not None:
                heap_series[key].append((collected_at, float(heap_used_pct)))
            after_gc = service.get("old_gen_after_gc_bytes") or {}
            maximum = metrics.get("heap_max_bytes")
            if maximum and after_gc:
                after_gc_series[key].append((
                    collected_at, 100 * sum(after_gc.values()) / maximum,
                ))
    findings = []
    heap_warning = float(jvm_cfg.get("warning_heap_used_pct", 80))
    heap_critical = float(jvm_cfg.get("critical_heap_used_pct", 90))
    for (platform, node, job, role), timed_values in heap_series.items():
        timed_values.sort(key=lambda item: item[0])
        values = [item[1] for item in timed_values]
        heap_state = _jvm_heap_state(
            timed_values,
            warning_pct=heap_warning,
            critical_pct=heap_critical,
        )
        level = heap_state["status"]
        if level in {"warning", "critical"}:
            recent_median = heap_state["recent_median_pct"]
            findings.append(_finding(
                f"resource_current:{platform}:jvm_heap:{node}:{job}:{role}", level,
                platform, node, "jvm", alert_type="current",
                observed_at=heap_state["active_observed_at"],
                job=job, role=role or None, metric="heap_used_pct",
                heap_used_pct=round(values[-1], 3),
                recent_median_pct=(round(recent_median, 3)
                                   if recent_median is not None else None),
                samples=len(values), warning_pct=heap_warning,
                danger_pct=heap_critical, sample_at=timed_values[-1][0].isoformat(),
                alert_state=level,
            ))

    for (platform, node, job, role), timed_values in after_gc_series.items():
        timed_values.sort(key=lambda item: item[0])
        values = [item[1] for item in timed_values]
        if len(values) < 6:
            continue
        trend = _jvm_trend(timed_values, jvm_cfg)
        level = None
        latest = values[-1]
        critical_pct = float(jvm_cfg.get("critical_after_gc_pct", 85))
        warning_pct = float(jvm_cfg.get("warning_after_gc_pct", 70))
        critical_eta = float(jvm_cfg.get("critical_eta_hours", 24))
        warning_eta = float(jvm_cfg.get("warning_eta_hours", 168))
        eta_hours = trend.get("eta_hours")
        recent_median = statistics.median(values[-3:])
        if (
            min(values[-3:]) >= critical_pct
            or (eta_hours is not None and eta_hours <= critical_eta)
        ):
            level = "critical"
        elif (
            recent_median >= warning_pct
            or (eta_hours is not None and eta_hours <= warning_eta)
        ):
            level = "warning"
        if level:
            findings.append(_finding(
                f"resource_forecast:{platform}:jvm:{node}:{job}:{role}", level,
                platform, node, "jvm", alert_type="forecast" if eta_hours is not None else "current",
                job=job, role=role or None, metric="after_gc_pct",
                observed_at=[item[0] for item in timed_values],
                after_gc_pct=round(latest, 3), samples=len(values),
                recent_median_pct=round(recent_median, 3),
                warning_pct=warning_pct, danger_pct=critical_pct,
                warning_eta_hours=warning_eta,
                **trend,
            ))
    return findings


def _jvm_heap_state(
    timed_values: list[tuple[datetime, float]],
    *,
    warning_pct: float,
    critical_pct: float,
) -> dict[str, Any]:
    """Evaluate current Heap risk using the configured confirmation rules.

    A warning requires three consecutive qualifying rolling windows.  A
    warning escalates after two critical samples; a critical finding falls
    back after three samples below the critical threshold.  Recovery requires
    three consecutive rolling windows below the warning threshold.  The
    rolling window is reset after a collection gap, while an already active
    state is retained until the recovery rule is satisfied.
    """
    status = "idle"
    activation_streak = 0
    recovery_streak = 0
    critical_up_streak = 0
    critical_down_streak = 0
    active_since: datetime | None = None
    active_observed_at: list[datetime] = []
    window: list[float] = []
    recent_median = None
    previous_at = None

    for timestamp, raw_value in timed_values:
        value = float(raw_value)
        if previous_at is not None and (timestamp - previous_at).total_seconds() > 15 * 60:
            window = []
            activation_streak = 0
            recovery_streak = 0
            critical_up_streak = 0
            critical_down_streak = 0
        previous_at = timestamp
        window.append(value)
        window = window[-3:]
        recent_median = statistics.median(window) if len(window) == 3 else None
        qualifies_warning = (
            recent_median is not None
            and value > warning_pct
            and recent_median > warning_pct
        )
        qualifies_recovery = (
            recent_median is not None
            and value < warning_pct
            and recent_median < warning_pct
        )

        if status == "idle":
            activation_streak = activation_streak + 1 if qualifies_warning else 0
            if activation_streak >= 3:
                status = "warning"
                active_since = timestamp
                active_observed_at = [timestamp]
                recovery_streak = 0
                critical_up_streak = 0
                critical_down_streak = 0
        elif status == "warning":
            if active_since is not None:
                active_observed_at.append(timestamp)
            recovery_streak = recovery_streak + 1 if qualifies_recovery else 0
            critical_up_streak = critical_up_streak + 1 if value > critical_pct else 0
            if recovery_streak >= 3:
                status = "idle"
                active_since = None
                active_observed_at = []
                activation_streak = 0
                recovery_streak = 0
                critical_up_streak = 0
                critical_down_streak = 0
            elif critical_up_streak >= 2:
                status = "critical"
                recovery_streak = 0
                critical_up_streak = 0
                critical_down_streak = 0
        else:  # critical
            if active_since is not None:
                active_observed_at.append(timestamp)
            recovery_streak = recovery_streak + 1 if qualifies_recovery else 0
            critical_down_streak = critical_down_streak + 1 if value < critical_pct else 0
            if recovery_streak >= 3:
                status = "idle"
                active_since = None
                active_observed_at = []
                activation_streak = 0
                recovery_streak = 0
                critical_up_streak = 0
                critical_down_streak = 0
            elif critical_down_streak >= 3:
                status = "warning"
                critical_up_streak = 0
                critical_down_streak = 0

    return {
        "status": status,
        "active_since": active_since,
        "active_observed_at": active_observed_at,
        "recent_median_pct": recent_median,
    }


def _business_risks(rows, config):
    series = defaultdict(list)
    metric_configs = {
        str(metric): metric_cfg
        for profile in (config.get("business_metrics") or {}).values()
        for metric, metric_cfg in (profile.get("metrics") or {}).items()
    }
    for snapshot in rows:
        collected_at = _safe_datetime(snapshot.get("collected_at"))
        if collected_at is None:
            continue
        for item in snapshot.get("business_metrics") or []:
            if item.get("status") != "ok" or item.get("value") is None:
                continue
            key = (
                item.get("platform"), item.get("node"), item.get("service"),
                item.get("job"), item.get("metric"),
            )
            metric_cfg = metric_configs.get(str(item.get("metric"))) or {}
            # Snapshot severity is calculated at collection time and may use
            # an older default. Recompute it here from the effective settings
            # so manual threshold overrides take effect without recollecting.
            severity = _business_severity(item.get("value"), metric_cfg)
            series[key].append((collected_at, item, severity))
    findings = []
    for key, values in series.items():
        values.sort(key=lambda row: row[0])
        latest_at, latest, severity = values[-1]
        if severity not in {"warning", "critical"}:
            continue
        active = []
        for timestamp, _item, row_severity in reversed(values):
            if row_severity not in {"warning", "critical"}:
                break
            active.append(timestamp)
        platform, node, service, job, metric = key
        metric_cfg = metric_configs.get(str(metric)) or {}
        threshold = metric_cfg.get(
            "critical_above" if severity == "critical" else "warning_above"
        )
        findings.append(_finding(
            f"resource_forecast:{platform}:business:{node}:{job}:{metric}",
            severity, platform, node, "business", observed_at=active,
            service=service, job=job, metric=metric,
            label=latest.get("label") or metric, value=latest.get("value"),
            unit=latest.get("unit") or "number", threshold=threshold,
            samples=len(active), sample_at=latest.get("sample_at") or latest_at.isoformat(),
        ))
    return findings


def _business_severity(value, config) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    critical = config.get("critical_above")
    warning = config.get("warning_above")
    if critical is not None and number >= float(critical):
        return "critical"
    if warning is not None and number >= float(warning):
        return "warning"
    return "healthy"


def _hourly_medians(values):
    buckets = defaultdict(list)
    sizes = {}
    for timestamp, available, size in values:
        hour = timestamp.replace(minute=0, second=0, microsecond=0)
        buckets[hour].append(available)
        sizes[hour] = size
    return [
        (hour, statistics.median(buckets[hour]), sizes[hour])
        for hour in sorted(buckets)
    ]


def _finding(key, severity, platform, node, resource, *, observed_at=None, **evidence):
    alert_type = evidence.pop("alert_type", "current")
    title, description = _risk_text(resource, evidence)
    timing = _risk_timing(observed_at or [])
    return {
        "key": key,
        "severity": severity,
        "platform": platform,
        "node": node,
        "resource": resource,
        "alert_type": alert_type,
        "title": title,
        "description": description,
        **timing,
        "evidence": evidence,
    }


def _risk_text(resource: str, evidence: dict) -> tuple[str, str]:
    if resource == "cpu":
        return (
            "CPU 持续高负载",
            f"最近窗口 CPU P95 为 {_fmt(evidence.get('p95'))}%，"
            f"基于 {evidence.get('samples', 0)} 个采集点判定。",
        )
    if resource == "memory":
        return (
            "节点可用内存偏低",
            f"最近窗口最低 MemAvailable 为 {_fmt(evidence.get('available_pct'))}%，"
            f"基于 {evidence.get('samples', 0)} 个采集点判定。",
        )
    if resource == "filesystem":
        return (
            "磁盘容量增长风险",
            f"挂载点 {evidence.get('mountpoint') or '未知'} 预计约 "
            f"{_fmt(evidence.get('eta_hours'))} 小时后达到 "
            f"{_fmt(evidence.get('target_pct'))}% 阈值。",
        )
    if resource == "jvm":
        job = str(evidence.get("job") or "JVM 服务").removesuffix("_exporter")
        if evidence.get("metric") == "heap_used_pct":
            alert_state = evidence.get("alert_state") or "warning"
            threshold = (
                evidence.get("danger_pct")
                if alert_state == "critical"
                else evidence.get("warning_pct")
            )
            threshold_label = "严重" if alert_state == "critical" else "告警"
            return (
                "JVM Heap 使用率偏高",
                f"{job} 最近一次 Heap 采样为 {_fmt(evidence.get('heap_used_pct'))}%，"
                f"最近 3 个采集点中位数为 {_fmt(evidence.get('recent_median_pct'))}%，"
                f"已达到 {_fmt(threshold)}% 现状{threshold_label}阈值。",
            )
        growth = evidence.get("increase_pct_points")
        eta = evidence.get("eta_hours")
        observed_hours = evidence.get("observed_hours", 0)
        span_hours = evidence.get("span_hours", 0)
        if eta is not None:
            return (
                "JVM 堆内存增长风险",
                f"{job} 的 GC 后 Old Gen 在 {observed_hours} 个有效小时桶内从 "
                f"{_fmt(evidence.get('start_pct'))}% 增至 "
                f"{_fmt(evidence.get('trend_current_pct'))}%，累计增长 "
                f"{_signed(growth)} 个百分点；稳健趋势为每小时 "
                f"{_signed(evidence.get('slope_pct_points_per_hour'))} 个百分点，"
                f"预计约 {_fmt(eta)} 小时后达到 "
                f"{_fmt(evidence.get('danger_pct'))}% 危险线。",
            )
        return (
            "JVM GC 后堆内存偏高",
            f"{job} 的 GC 后 Old Gen 当前为 "
            f"{_fmt(evidence.get('after_gc_pct'))}%，在跨度 "
            f"{_fmt(span_hours)} 小时的 {observed_hours} 个有效小时桶内，"
            f"小时中位数从 {_fmt(evidence.get('start_pct'))}% 变为 "
            f"{_fmt(evidence.get('trend_current_pct'))}%，变化 "
            f"{_signed(growth)} 个百分点；最近 3 个采集点中位数为 "
            f"{_fmt(evidence.get('recent_median_pct'))}%，"
            f"已达到 {_fmt(evidence.get('warning_pct'))}% 关注线，"
            f"仍需结合 Old/Full GC 次数和耗时观察。",
        )
    if resource == "business":
        service = str(evidence.get("service") or "业务服务")
        label = str(evidence.get("label") or evidence.get("metric") or "业务指标")
        return (
            f"{service} {label}达到风险阈值",
            f"{label}当前为 {_business_value(evidence.get('value'), evidence.get('unit'))}，"
            f"已达到本级阈值 {_business_value(evidence.get('threshold'), evidence.get('unit'))}；"
            f"连续 {evidence.get('samples', 0)} 个有效样本处于风险区间。",
        )
    return "资源风险", "该风险由本地资源快照趋势分析产生。"


def _fmt(value) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "未知"


def _signed(value) -> str:
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return "未知"


def _business_value(value, unit) -> str:
    formatted = _fmt(value)
    return {
        "seconds": f"{formatted} 秒",
        "percent": f"{formatted}%",
        "per_second": f"{formatted}/秒",
        "count": formatted,
    }.get(str(unit), formatted)


def _jvm_trend(timed_values, config) -> dict:
    hourly = _hourly_scalar_medians(timed_values)
    if not hourly:
        return {
            "trend": "insufficient", "start_pct": None,
            "trend_current_pct": None,
            "increase_pct_points": None, "slope_pct_points_per_hour": None,
            "eta_hours": None, "observed_hours": 0, "span_hours": 0.0,
            "trend_correlation": None,
        }
    started_at, start_pct = hourly[0]
    latest_at, latest_pct = hourly[-1]
    span_hours = (latest_at - started_at).total_seconds() / 3600
    elapsed = [
        (timestamp - started_at).total_seconds() / 3600
        for timestamp, _value in hourly
    ]
    medians = [value for _timestamp, value in hourly]
    slopes = [
        (medians[right] - medians[left]) / (elapsed[right] - elapsed[left])
        for left in range(len(hourly))
        for right in range(left + 1, len(hourly))
        if elapsed[right] > elapsed[left]
    ]
    slope = statistics.median(slopes) if slopes else 0.0
    correlation = _correlation(elapsed, medians)
    increase = latest_pct - start_pct
    minimum_hours = int(config.get("min_hours", 12))
    stable_rise = (
        len(hourly) >= minimum_hours
        and increase >= float(config.get("min_growth_pct_points", 1.0))
        and slope >= float(config.get("min_slope_pct_points_per_hour", 0.02))
        and correlation >= float(config.get("min_trend_correlation", 0.6))
    )
    stable_fall = (
        len(hourly) >= minimum_hours
        and increase <= -float(config.get("min_growth_pct_points", 1.0))
        and slope <= -float(config.get("min_slope_pct_points_per_hour", 0.02))
        and correlation <= -float(config.get("min_trend_correlation", 0.6))
    )
    danger_pct = float(config.get("critical_after_gc_pct", 85))
    eta_hours = None
    if stable_rise and slope > 0:
        eta_hours = max(0.0, (danger_pct - latest_pct) / slope)
    return {
        "trend": "rising" if stable_rise else "falling" if stable_fall else "stable",
        "start_pct": round(start_pct, 3),
        "trend_current_pct": round(latest_pct, 3),
        "increase_pct_points": round(increase, 3),
        "slope_pct_points_per_hour": round(slope, 5),
        "eta_hours": round(eta_hours, 2) if eta_hours is not None else None,
        "observed_hours": len(hourly),
        "span_hours": round(span_hours, 2),
        "trend_correlation": round(correlation, 4),
    }


def _hourly_scalar_medians(timed_values):
    buckets = defaultdict(list)
    for timestamp, value in timed_values:
        hour = timestamp.replace(minute=0, second=0, microsecond=0)
        buckets[hour].append(float(value))
    return [
        (hour, statistics.median(buckets[hour])) for hour in sorted(buckets)
    ]


def _correlation(left, right) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale == 0 or right_scale == 0:
        return 0.0
    return numerator / (left_scale * right_scale)


def _forecast_status(rows, config):
    """Expose forecast readiness separately from threshold-matching findings."""
    fs_cfg = ((config.get("risk_defaults") or {}).get("filesystem") or {})
    required_hours = int(fs_cfg.get("min_hours", 12))
    warning_eta_hours = float(fs_cfg.get("warning_eta_hours", 168))
    platforms = sorted({
        str(row.get("platform")) for row in rows if row.get("platform")
    })
    timestamps = defaultdict(lambda: defaultdict(list))
    for snapshot in rows:
        collected_at = _safe_datetime(snapshot.get("collected_at"))
        if collected_at is None:
            continue
        platform = str(snapshot.get("platform"))
        for node in snapshot.get("nodes") or []:
            for filesystem in node.get("filesystems") or []:
                if not (
                    filesystem.get("kind") == "local"
                    and filesystem.get("available_bytes") is not None
                    and filesystem.get("size_bytes")
                ):
                    continue
                key = (
                    str(node.get("node")), str(filesystem.get("device")),
                    str(filesystem.get("mountpoint")),
                )
                timestamps[platform][key].append(collected_at)

    result = []
    for platform in platforms:
        series = timestamps.get(platform) or {}
        values = sorted({
            value for series_values in series.values() for value in series_values
        })
        if not values:
            result.append({
                "platform": platform,
                "status": "no_data",
                "required_observed_hours": required_hours,
                "observed_hours": 0,
                "series_count": 0,
                "ready_series_count": 0,
                "span_hours": 0.0,
                "first_collected_at": None,
                "last_updated_at": None,
                "has_data_gaps": False,
                "largest_gap_seconds": 0,
                "warning_eta_hours": warning_eta_hours,
            })
            continue
        observed_by_series = [
            len({
                value.replace(minute=0, second=0, microsecond=0)
                for value in series_values
            })
            for series_values in series.values()
        ]
        gaps = [
            (right - left).total_seconds()
            for left, right in zip(values, values[1:])
        ]
        largest_gap = max(gaps, default=0)
        observed_hours = max(observed_by_series, default=0)
        ready_series_count = sum(
            count >= required_hours for count in observed_by_series
        )
        result.append({
            "platform": platform,
            "status": "ready" if ready_series_count else "collecting",
            "required_observed_hours": required_hours,
            "observed_hours": observed_hours,
            "series_count": len(observed_by_series),
            "ready_series_count": ready_series_count,
            "span_hours": round((values[-1] - values[0]).total_seconds() / 3600, 2),
            "first_collected_at": values[0].isoformat(),
            "last_updated_at": values[-1].isoformat(),
            "has_data_gaps": largest_gap > 15 * 60,
            "largest_gap_seconds": round(largest_gap),
            "warning_eta_hours": warning_eta_hours,
        })
    return result


def _risk_timing(observed_at) -> dict:
    """Return the duration of the latest continuous observation segment."""
    values = sorted({
        parsed for value in observed_at
        if (parsed := _safe_datetime(value)) is not None
    })
    if not values:
        return {
            "duration_seconds": 0,
            "first_observed_at": None,
            "last_updated_at": None,
        }
    started_at = values[-1]
    for left, right in reversed(list(zip(values, values[1:]))):
        if (right - left).total_seconds() > 15 * 60:
            break
        started_at = left
    return {
        "duration_seconds": max(0, round((values[-1] - started_at).total_seconds())),
        "first_observed_at": started_at.isoformat(),
        "last_updated_at": values[-1].isoformat(),
    }


def _recent_rows(rows, *, hours: int):
    timestamps = []
    for row in rows:
        try:
            timestamps.append(_as_datetime(row.get("collected_at")))
        except (TypeError, ValueError):
            continue
    if not timestamps:
        return []
    cutoff = max(timestamps) - timedelta(hours=hours)
    return [
        row for row in rows
        if _safe_datetime(row.get("collected_at")) is not None
        and _safe_datetime(row.get("collected_at")) >= cutoff
    ]


def _safe_datetime(value):
    try:
        return _as_datetime(value)
    except (TypeError, ValueError):
        return None


def _percentile(values, quantile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return math.nan
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def _as_datetime(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
