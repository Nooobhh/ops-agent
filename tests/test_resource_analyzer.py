from datetime import datetime, timedelta, timezone

from lib.acquisition.analyzer import analyze_resource_history
from lib.acquisition.store import append_snapshot


def test_jvm_risk_contains_operator_readable_description(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    for index in range(6):
        timestamp = start + timedelta(minutes=5 * index)
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"snap-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "beta",
            "nodes": [],
            "services": [{
                "node": "node14", "job": "hbase_regionserver_exporter",
                "role": None, "metrics": {"heap_max_bytes": 100},
                "old_gen_after_gc_bytes": {"G1 Old Gen": 80},
            }],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"jvm": {
            "warning_after_gc_pct": 70,
            "critical_after_gc_pct": 85,
        }},
    }, now=start + timedelta(minutes=30))

    finding = result["findings"][0]
    assert finding["title"] == "JVM GC 后堆内存偏高"
    assert "80.0%" in finding["description"]
    assert "Old/Full GC" in finding["description"]
    assert finding["duration_seconds"] == 25 * 60
    assert finding["last_updated_at"] == (start + timedelta(minutes=25)).isoformat()


def test_jvm_heap_usage_is_a_current_alert_separate_from_after_gc(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    for index in range(8):
        timestamp = start + timedelta(minutes=5 * index)
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"heap-current-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "beta",
            "nodes": [],
            "services": [{
                "node": "node14", "job": "hbase_regionserver_exporter",
                "role": None,
                "metrics": {"heap_max_bytes": 100, "heap_used_pct": 91},
                "old_gen_after_gc_bytes": {"G1 Old Gen": 0},
            }],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"jvm": {
            "warning_heap_used_pct": 80,
            "critical_heap_used_pct": 90,
            "warning_after_gc_pct": 70,
            "critical_after_gc_pct": 85,
        }},
    }, now=start + timedelta(minutes=45))

    finding = next(item for item in result["findings"] if item["evidence"].get("metric") == "heap_used_pct")
    assert finding["alert_type"] == "current"
    assert finding["severity"] == "critical"
    assert finding["evidence"]["heap_used_pct"] == 91.0
    assert "Heap 使用率" in finding["title"]


def test_jvm_heap_alert_and_recovery_follow_sampling_state_machine(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    values = [86, 86, 86, 86]

    def append_values(start_index, new_values):
        for offset, value in enumerate(new_values):
            index = start_index + offset
            timestamp = start + timedelta(minutes=5 * index)
            append_snapshot(root, {
                "schema_version": 1,
                "snapshot_id": f"heap-state-{index}",
                "collected_at": timestamp.isoformat(),
                "platform": "beta",
                "nodes": [],
                "services": [{
                    "node": "node14", "job": "hbase_regionserver_exporter",
                    "role": None, "metrics": {"heap_max_bytes": 100, "heap_used_pct": value},
                    "old_gen_after_gc_bytes": {"G1 Old Gen": 0},
                }],
            })

    append_values(0, values)
    config = {"risk_defaults": {"jvm": {
        "warning_heap_used_pct": 85,
        "critical_heap_used_pct": 90,
        "warning_after_gc_pct": 70,
        "critical_after_gc_pct": 85,
    }}}

    before_confirmation = analyze_resource_history(
        root, config, now=start + timedelta(minutes=20),
    )
    assert not [item for item in before_confirmation["findings"] if item["evidence"].get("metric") == "heap_used_pct"]

    append_values(4, [86])
    warning = analyze_resource_history(root, config, now=start + timedelta(minutes=25))
    warning_finding = next(item for item in warning["findings"] if item["evidence"].get("metric") == "heap_used_pct")
    assert warning_finding["severity"] == "warning"
    assert warning_finding["evidence"]["heap_used_pct"] == 86.0

    append_values(5, [91, 91])
    critical = analyze_resource_history(root, config, now=start + timedelta(minutes=35))
    critical_finding = next(item for item in critical["findings"] if item["evidence"].get("metric") == "heap_used_pct")
    assert critical_finding["severity"] == "critical"
    assert critical_finding["evidence"]["heap_used_pct"] == 91.0

    append_values(7, [89, 89, 89])
    degraded = analyze_resource_history(root, config, now=start + timedelta(minutes=50))
    degraded_finding = next(item for item in degraded["findings"] if item["evidence"].get("metric") == "heap_used_pct")
    assert degraded_finding["severity"] == "warning"

    append_values(10, [80, 80, 80, 80])
    recovered = analyze_resource_history(root, config, now=start + timedelta(minutes=70))
    assert not [item for item in recovered["findings"] if item["evidence"].get("metric") == "heap_used_pct"]


def test_forecast_readiness_counts_valid_hours_and_reports_gaps(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    timestamps = [start + timedelta(hours=index) for index in range(6)]
    timestamps += [start + timedelta(days=2, hours=index) for index in range(6)]
    for index, timestamp in enumerate(timestamps):
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"disk-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "alpha",
            "nodes": [{
                "node": "node1",
                "metrics": {},
                "filesystems": [{
                    "kind": "local", "readonly": 0,
                    "device": "/dev/sda1", "mountpoint": "/data",
                    "size_bytes": 1000, "available_bytes": 800,
                }],
            }],
            "services": [],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"filesystem": {
            "min_hours": 12, "warning_eta_hours": 168,
        }},
    }, now=start + timedelta(days=2, hours=6))

    status = result["forecast_status"][0]
    assert status["status"] == "ready"
    assert status["observed_hours"] == 12
    assert status["series_count"] == 1
    assert status["ready_series_count"] == 1
    assert status["span_hours"] == 53
    assert status["has_data_gaps"] is True
    assert status["largest_gap_seconds"] == 43 * 3600
    assert status["finding_count"] == 0
    assert status["health"] == "healthy"


def test_risk_duration_restarts_after_collection_gap(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    timestamps = [start + timedelta(minutes=5 * index) for index in range(6)]
    timestamps += [
        start + timedelta(hours=6, minutes=5 * index) for index in range(6)
    ]
    for index, timestamp in enumerate(timestamps):
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"jvm-gap-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "beta",
            "nodes": [],
            "services": [{
                "node": "node14", "job": "hbase_regionserver_exporter",
                "role": None, "metrics": {"heap_max_bytes": 100},
                "old_gen_after_gc_bytes": {"G1 Old Gen": 80},
            }],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"jvm": {"warning_after_gc_pct": 70}},
    }, now=start + timedelta(hours=6, minutes=30))

    finding = result["findings"][0]
    assert finding["duration_seconds"] == 25 * 60
    assert finding["last_updated_at"] == timestamps[-1].isoformat()


def test_jvm_stable_growth_forecasts_danger_line_before_current_warning(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    for index in range(12):
        timestamp = start + timedelta(hours=index)
        after_gc_pct = 60 + index * 0.2
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"jvm-growth-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "beta",
            "nodes": [],
            "services": [{
                "node": "node14", "job": "hbase_regionserver_exporter",
                "role": None, "metrics": {"heap_max_bytes": 100},
                "old_gen_after_gc_bytes": {"G1 Old Gen": after_gc_pct},
            }],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"jvm": {
            "warning_after_gc_pct": 70, "critical_after_gc_pct": 85,
            "min_hours": 12, "min_growth_pct_points": 1,
            "min_slope_pct_points_per_hour": 0.02,
            "min_trend_correlation": 0.6,
            "warning_eta_hours": 168, "critical_eta_hours": 24,
        }},
    }, now=start + timedelta(hours=12))

    finding = result["findings"][0]
    assert finding["title"] == "JVM 堆内存增长风险"
    assert finding["severity"] == "warning"
    assert finding["evidence"]["trend"] == "rising"
    assert finding["evidence"]["increase_pct_points"] == 2.2
    assert finding["evidence"]["slope_pct_points_per_hour"] == 0.2
    assert finding["evidence"]["eta_hours"] == 114
    assert "60.0% 增至 62.2%" in finding["description"]
    assert "85.0% 危险线" in finding["description"]


def test_jvm_warning_clears_when_recent_samples_recover_to_zero(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    values = [80] * 6 + [0] * 6
    for index, after_gc_pct in enumerate(values):
        timestamp = start + timedelta(hours=index)
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"jvm-recovered-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "beta",
            "nodes": [],
            "services": [{
                "node": "node14", "job": "hbase_regionserver_exporter",
                "role": None, "metrics": {"heap_max_bytes": 100},
                "old_gen_after_gc_bytes": {"G1 Old Gen": after_gc_pct},
            }],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"jvm": {
            "warning_after_gc_pct": 70,
            "critical_after_gc_pct": 85,
        }},
    }, now=start + timedelta(hours=12))

    assert result["findings"] == []


def test_disk_forecast_health_becomes_warning_when_eta_enters_window(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    gib = 1024 ** 3
    for index in range(12):
        timestamp = start + timedelta(hours=index)
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"disk-warning-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "alpha",
            "nodes": [{
                "node": "node1", "metrics": {},
                "filesystems": [{
                    "kind": "local", "readonly": 0,
                    "device": "/dev/sda1", "mountpoint": "/data",
                    "size_bytes": 100 * gib,
                    "available_bytes": (40 - index * 0.2) * gib,
                }],
            }],
            "services": [],
        })

    result = analyze_resource_history(root, {
        "risk_defaults": {"filesystem": {
            "min_hours": 12, "default_target_pct": 80,
            "warning_eta_hours": 168, "critical_eta_hours": 24,
        }},
    }, now=start + timedelta(hours=12))

    status = result["forecast_status"][0]
    assert status["health"] == "warning"
    assert status["finding_count"] == 1
    finding = next(row for row in result["findings"] if row["resource"] == "filesystem")
    assert finding["severity"] == "warning"
    assert finding["alert_type"] == "forecast"
    assert 80 < finding["evidence"]["eta_hours"] < 100


def test_critical_business_metric_is_promoted_to_resource_finding(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    for index, waits in enumerate((0, 12, 53)):
        timestamp = start + timedelta(minutes=5 * index)
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"business-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "beta", "nodes": [], "services": [],
            "business_metrics": [{
                "platform": "beta", "node": "node1", "service": "MySQL",
                "job": "iot_mysql_exporter", "metric": "row_lock_current_waits",
                "label": "当前行锁等待", "unit": "count", "value": waits,
                "status": "ok",
                "severity": "healthy" if waits == 0 else "critical",
                "sample_at": timestamp.isoformat(), "details": {},
            }],
        })

    result = analyze_resource_history(root, {
        "business_metrics": {"mysql": {"metrics": {
            "row_lock_current_waits": {
                "warning_above": 1, "critical_above": 10,
            },
        }}},
    }, now=start + timedelta(minutes=15))

    finding = result["findings"][0]
    assert finding["resource"] == "business"
    assert finding["severity"] == "critical"
    assert finding["title"] == "MySQL 当前行锁等待达到风险阈值"
    assert "当前为 53.0" in finding["description"]
    assert "阈值 10.0" in finding["description"]
    assert finding["evidence"]["samples"] == 2


def test_business_risk_recomputes_severity_from_effective_thresholds(tmp_path):
    root = tmp_path / "resources"
    start = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    for index, value in enumerate((0.0, 0.03)):
        timestamp = start + timedelta(minutes=5 * index)
        append_snapshot(root, {
            "schema_version": 1,
            "snapshot_id": f"business-override-{index}",
            "collected_at": timestamp.isoformat(),
            "platform": "mysql",
            "nodes": [], "services": [],
            "business_metrics": [{
                "platform": "mysql", "node": "192.0.2.111", "service": "MySQL",
                "job": "mysql_exporter1", "metric": "slow_queries_per_second",
                "label": "慢查询速率", "unit": "per_second", "value": value,
                # Simulate a stale collection-time severity from the old 0.01 default.
                "status": "ok", "severity": "warning",
                "sample_at": timestamp.isoformat(), "details": {},
            }],
        })

    result = analyze_resource_history(root, {
        "business_metrics": {"mysql": {"metrics": {
            "slow_queries_per_second": {
                "warning_above": 0.05, "critical_above": 0.1,
            },
        }}},
    }, now=start + timedelta(minutes=10))

    assert result["findings"] == []


def test_excluded_platform_is_removed_from_forecast_history(tmp_path):
    root = tmp_path / "resources"
    append_snapshot(root, {
        "schema_version": 1, "snapshot_id": "ai-old",
        "collected_at": "2026-08-31T00:00:00+00:00", "platform": "ai",
        "nodes": [{"node": "127.0.0.1", "metrics": {}, "filesystems": [{
            "kind": "local", "readonly": 0, "device": "/dev/disk1",
            "mountpoint": "/", "size_bytes": 100, "available_bytes": 50,
        }]}], "services": [],
    })

    result = analyze_resource_history(root, {
        "node": {"excluded_platforms": ["ai"]},
        "risk_defaults": {"filesystem": {"min_hours": 12}},
    }, now=datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc))

    assert result["snapshot_count"] == 0
    assert result["forecast_status"] == []
