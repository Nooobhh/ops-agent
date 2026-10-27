from lib.alerts.model import AlertEvent, normalize_host


def test_normalize_host_handles_ip_fqdn_short_and_unknown():
    nodes = {
        "hd01": {"ip": "198.51.100.101"},
        "hd02": {"ip": "198.51.100.102"},
        "hd03": {"ip": "198.51.100.103"},
    }

    assert normalize_host("198.51.100.101:9100", nodes) == "hd01"
    assert normalize_host("hd02.example.internal", nodes) == "hd02"
    assert normalize_host("hd03", nodes) == "hd03"
    assert normalize_host("not-in-platform", nodes) is None


def test_alert_event_key_uses_normalized_host_or_raw_fallback():
    with_host = AlertEvent(
        source="prometheus",
        platform="alpha",
        alertname="disk_full",
        severity="warning",
        host="hd01",
        host_raw="198.51.100.101:9100",
        service="hdfs",
        state="firing",
        detail="disk",
    )
    without_host = AlertEvent(
        source="demo",
        platform="unknown",
        alertname="service_down",
        severity="critical",
        host=None,
        host_raw="raw-host",
        service="yarn",
        state="firing",
        detail="down",
    )

    assert with_host.key == "prometheus:alpha:disk_full:hd01"
    assert without_host.key == "demo:unknown:service_down:raw-host"
