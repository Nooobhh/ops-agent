import yaml

from web.server.data.dedup_live import compute_live


def _entry(source, platform, alertname, host="node1", host_raw=None):
    return {"event": {
        "source": source,
        "platform": platform,
        "alertname": alertname,
        "severity": "critical",
        "host": host,
        "host_raw": host_raw or host,
        "service": "ZooKeeper",
        "state": "firing",
        "detail": {},
    }}


def _rules(path):
    path.write_text(yaml.safe_dump({
        "version": 1,
        "rules": [{
            "match": {
                "demo": {"platform": "alpha", "alertname": "zookeeper_server_process"},
                "prometheus": {"port": 2181},
            },
            "winner": "demo",
            "reason": "service context",
        }],
    }), encoding="utf-8")
    return path


def _active():
    return {
        "demo:alpha:zookeeper_server_process:node1": _entry(
            "demo", "alpha", "zookeeper_server_process",
        ),
        "prometheus:alpha:zookeeper_port:node1": _entry(
            "prometheus", "alpha", "zookeeper_port", host_raw="node1:2181",
        ),
    }


def test_compute_live_returns_loser_to_winner_mapping(tmp_path):
    result = compute_live(_rules(tmp_path / "rules.yaml"), _active(), {})

    assert result == {
        "mapping": {
            "prometheus:alpha:zookeeper_port:node1":
                "demo:alpha:zookeeper_server_process:node1",
        },
        "excluded_sources": [],
    }


def test_compute_live_excludes_entries_from_failed_source(tmp_path):
    result = compute_live(
        _rules(tmp_path / "rules.yaml"), _active(), {"demo:alpha": 1},
    )

    assert result["mapping"] == {}
    assert result["excluded_sources"] == ["demo:alpha"]


def test_compute_live_missing_rules_is_safe(tmp_path):
    assert compute_live(tmp_path / "missing.yaml", _active(), {}) == {
        "mapping": {}, "excluded_sources": [],
    }
