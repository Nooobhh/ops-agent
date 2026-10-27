"""Prometheus 告警源经插件(MCP)回放:时段 → firing/ok 事件对。"""
import pytest

from lib.alerts.sources import PrometheusSource, SourceError
from tests.prometheus_feed import (
    LABEL_CLUSTERS, FakeFeed, parse_cst, firing, iso, span, stamp, upstream_unavailable,
)

BASE = 1_774_603_200  # 2026-03-27T08:00:00+00:00,整分钟


def _source(feed, start=BASE, end=BASE + 300):
    return PrometheusSource(
        feed, LABEL_CLUSTERS, {"alpha": {"hd01": {"host": "hd01"}}},
        query_start_ms=start * 1000, query_end_ms=end * 1000,
    )


def test_closed_span_generates_firing_and_ok():
    firing_event, resolved = _source(FakeFeed(spans=[span(BASE + 60, BASE + 120)])).fetch()

    assert firing_event.state == "firing" and firing_event.platform == "alpha" and firing_event.host == "hd01"
    assert firing_event.source_active_at == iso(BASE + 60)
    assert firing_event.source_last_seen_at == iso(BASE + 120)
    assert resolved.state == "ok"
    assert resolved.source_active_at == iso(BASE + 60)
    assert resolved.source_resolved_at == iso(BASE + 180), "收口时间 = 最后一个点 + 1 个步长"


def test_open_span_uses_active_at_from_current_alerts():
    active_at = BASE - 1800
    feed = FakeFeed(alerts=[firing(active_at=active_at, summary="disk high")],
                    spans=[span(BASE, BASE + 240)])
    events = _source(feed).fetch()

    assert len(events) == 1 and events[0].state == "firing"
    assert events[0].source_active_at == iso(active_at)
    assert events[0].source_last_seen_at == iso(BASE + 240)
    assert events[0].detail == "disk high" and events[0].severity == "critical"


def test_span_still_open_by_mcp_rule_even_if_gone_from_current_alerts():
    """到窗口末尾 2 个步长内仍出现即未收口;下一 tick 重叠窗口里再收口。"""
    events = _source(FakeFeed(spans=[span(BASE + 60, BASE + 180)])).fetch()

    assert [event.state for event in events] == ["firing"]
    assert events[0].source_active_at == iso(BASE + 60)


def test_multiple_segments_preserved_and_parallel_series_merged():
    feed = FakeFeed(spans=[
        span(BASE + 60, BASE + 60),
        span(BASE + 240, BASE + 300, labels={"taskId": "1"}),
        span(BASE + 300, BASE + 360, labels={"taskId": "2"}),
        span(BASE + 600, BASE + 660),
    ])
    events = _source(feed, end=BASE + 1200).fetch()

    assert [event.state for event in events] == ["firing", "ok", "firing", "ok", "firing", "ok"]
    assert [event.source_active_at for event in events[::2]] == [iso(BASE + 60), iso(BASE + 240), iso(BASE + 600)]
    assert [event.source_resolved_at for event in events[1::2]] == [
        iso(BASE + 120), iso(BASE + 420), iso(BASE + 720),
    ], "同一主机的多条序列(任务号不同)并成一段"


def test_closed_span_clipped_by_window_start_only_reports_resolution():
    """起点被窗口截断的已收口段,上一 tick 见过它的开始:只出 ok,不再记一次新告警。"""
    events = _source(FakeFeed(spans=[span(BASE, BASE + 60)])).fetch()

    assert [event.state for event in events] == ["ok"]
    assert events[0].source_resolved_at == iso(BASE + 120)


def test_window_is_floored_to_whole_minutes_and_passed_as_cst():
    feed = FakeFeed()
    _source(feed, start=BASE + 37, end=BASE + 337).fetch()

    history = next(arguments for role, arguments in feed.calls if role == "history")
    assert history == {"start": stamp(BASE)["real"], "end": stamp(BASE + 337)["real"], "limit": 1000}
    assert [role for role, _ in feed.calls] == ["current", "rules", "history"]


def test_identical_span_in_overlapping_ticks_yields_identical_events():
    feed = FakeFeed(spans=[span(BASE + 120, BASE + 180)])
    first = _source(feed, start=BASE, end=BASE + 600).fetch()
    second = _source(feed, start=BASE + 60, end=BASE + 900).fetch()
    assert first == second


def test_platform_from_label_mapping_and_fqdn_fallback():
    feed = FakeFeed(alerts=[
        firing("a", labels={"platform": "beta", "instance": "198.51.100.102:9100"}),
        firing("b", labels={"platform": "edge", "instance": "node9.beta.local:2181"}),
        firing("c", labels={"platform": "", "instance": "198.51.100.101:9100"}),
        firing("d", labels={"platform": "", "instance": "198.51.100.199:9100"}),
    ])
    source = PrometheusSource(feed, LABEL_CLUSTERS, {
        "alpha": {"hd01": {"ip": "198.51.100.101"}, "node9": {}}, "beta": {"iot01": {"ip": "198.51.100.102"}, "node9": {}},
    })
    by_name = {event.alertname: event for event in source.fetch()}

    assert (by_name["a"].platform, by_name["a"].host) == ("beta", "iot01")
    assert (by_name["b"].platform, by_name["b"].host) == ("beta", "node9"), "映射外的平台标签按 FQDN 推断"
    assert (by_name["c"].platform, by_name["c"].host) == ("unknown", "hd01")
    assert (by_name["d"].platform, by_name["d"].host, by_name["d"].host_raw) == ("unknown", None, "198.51.100.199:9100")


def test_rules_become_definitions():
    feed = FakeFeed(rules=[
        {"name": "HostCpuUsageHigh", "group": "resource", "state": "inactive", "labels": {"severity": "critical"}},
        {"name": "Selfmon", "group": "selfmon", "state": "firing", "labels": {"platform": "beta"}},
    ])
    source = _source(feed)
    source.fetch()

    assert source.last_definitions == [
        {"alertname": "HostCpuUsageHigh", "group": "resource", "severity": "critical",
         "platform": "unknown", "state": "inactive"},
        {"alertname": "Selfmon", "group": "selfmon", "severity": "warning", "platform": "beta", "state": "firing"},
    ]


@pytest.mark.parametrize("role", ["current", "rules", "history"])
def test_any_feed_failure_is_a_source_error_not_empty(role):
    with pytest.raises(SourceError) as caught:
        _source(FakeFeed(errors={role: upstream_unavailable()})).fetch()
    assert caught.value.source == "prometheus"
    assert "UPSTREAM_UNAVAILABLE" in str(caught.value) and "victoriametrics" in str(caught.value)


def test_truncated_history_is_bisected_on_whole_minutes():
    every_minute = [span(BASE + 60 * index, BASE + 60 * index, f"a{index}") for index in range(1, 10)]

    def spans(start, end):
        return [item for item in every_minute if start <= parse_cst(item["start"]["real"]) < end]

    feed = FakeFeed(spans=spans, limit=4)
    events = _source(feed, end=BASE + 1200).fetch()

    assert sorted(event.alertname for event in events if event.state == "firing") == [f"a{i}" for i in range(1, 10)]
    windows = [(arguments["start"], arguments["end"]) for role, arguments in feed.calls if role == "history"]
    assert len(windows) > 1 and all(start.endswith(":00") for start, _ in windows)


def test_history_still_truncated_after_max_splits_fails():
    feed = FakeFeed(spans=lambda start, end: [span(start + 60, start + 60, f"x{i}") for i in range(3)], limit=1)
    with pytest.raises(SourceError, match="二分"):
        _source(feed, end=BASE + 3600).fetch()
