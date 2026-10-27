from types import SimpleNamespace

from lib.network_env import configure_direct_network, configure_no_proxy
from lib.alerts.state import AlertsState
from scripts import alert_poll


def test_configure_no_proxy_merges_both_cases_and_url_hosts():
    environ = {
        "NO_PROXY": "localhost,198.51.100.101",
        "no_proxy": "127.0.0.1",
    }

    configure_no_proxy(
        [
            "http://192.0.2.81:9092",
            "http://192.0.2.11:8080/api/v1/clusters",
            "192.0.2.81",
        ],
        environ=environ,
    )

    expected = (
        "localhost,198.51.100.101,127.0.0.1,192.0.2.81,192.0.2.11"
    )
    assert environ["NO_PROXY"] == expected
    assert environ["no_proxy"] == expected


def test_configure_direct_network_removes_all_proxy_variables():
    environ = {
        "HTTP_PROXY": "http://proxy:7890",
        "HTTPS_PROXY": "http://proxy:7890",
        "ALL_PROXY": "socks5://proxy:7891",
        "http_proxy": "http://proxy:7890",
        "https_proxy": "http://proxy:7890",
        "all_proxy": "socks5://proxy:7891",
        "NO_PROXY": "localhost",
        "no_proxy": "127.0.0.1",
        "KEEP_ME": "unchanged",
    }

    configure_direct_network(environ=environ)

    assert environ == {
        "NO_PROXY": "*",
        "no_proxy": "*",
        "KEEP_ME": "unchanged",
    }


def test_collect_events_adds_source_and_platform_endpoints_to_no_proxy(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(alert_poll, "_load_platforms", lambda: {
        "alpha": SimpleNamespace(
            endpoints={
                "prometheus": {"url": "http://192.0.2.81:9092"},
                "manager": {"url": "http://192.0.2.11:8080"},
            },
            nodes={},
        ),
        "beta": SimpleNamespace(
            endpoints={
                "prometheus": {"url": "http://192.0.2.53:9091"},
                "manager": {"url": "http://192.0.2.200:8080"},
            },
            nodes={},
        ),
    })
    config = {
        "sources": {
            "prometheus-central": {
                "type": "prometheus",
                "url": "http://192.0.2.81:9092",
                "enabled": False,
            },
        },
    }

    alert_poll.collect_events(
        config,
        AlertsState(tmp_path / "alerts.json"),
    )

    expected = (
        "192.0.2.81,192.0.2.11,192.0.2.53,192.0.2.200"
    )
    assert alert_poll.os.environ["NO_PROXY"] == expected
    assert alert_poll.os.environ["no_proxy"] == expected
