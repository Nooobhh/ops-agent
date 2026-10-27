import json

import pytest

from lib.alerts.sources import PrometheusSource, SourceError
from tests.prometheus_feed import LABEL_CLUSTERS, FakeFeed, firing, iso, upstream_unavailable


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_prometheus_fetch_normalizes_platform_severity_and_host():
    feed = FakeFeed(alerts=[firing("node_disk", labels={"instance": "198.51.100.101:9100", "severity": ""},
                                   active_at=1784682123, summary="disk high")])
    events = PrometheusSource(feed, LABEL_CLUSTERS, {"alpha": {"hd01": {"ip": "198.51.100.101"}}}).fetch()

    assert feed.calls == [("current", {"state": "firing"})], "无窗口时只取当前告警"
    assert len(events) == 1
    event = events[0]
    assert (event.platform, event.severity, event.host, event.detail) == ("alpha", "warning", "hd01", "disk high")
    assert event.source_active_at == iso(1784682123)


def test_prometheus_plugin_error_raises_source_error_with_source_name():
    with pytest.raises(SourceError) as excinfo:
        PrometheusSource(FakeFeed(errors={"current": upstream_unavailable("prometheus")}), {}, {}).fetch()

    assert excinfo.value.source == "prometheus"
    assert "prometheus" in str(excinfo.value)
