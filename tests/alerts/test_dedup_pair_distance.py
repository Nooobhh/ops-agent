from lib.alerts.dedup import DedupRules, apply
from lib.alerts.model import AlertEvent


RULES = DedupRules(version=1, rules=({
    "match": {
        "demo": {"platform": "alpha", "alertname": "zookeeper_server_process"},
        "prometheus": {"port": 2181},
    },
    "winner": "demo",
},))


def _event(source, alertname, timestamp=None):
    return AlertEvent(
        source=source,
        platform="alpha",
        alertname=alertname,
        severity="critical",
        host="hd01",
        host_raw="hd01" if source == "demo" else "hd01:2181",
        service="ZOOKEEPER",
        state="firing",
        detail="",
        source_active_at=timestamp,
    )


def _pair(left_ts=None, right_ts=None):
    winner = _event("demo", "zookeeper_server_process", left_ts)
    loser = _event("prometheus", "zookeeper_port", right_ts)
    return winner, loser


def test_dedup_default_300_backward_compat():
    winner, loser = _pair()

    assert apply(RULES, [winner, loser]) == {loser.key: winner.key}


def test_dedup_pair_distance_within_60s():
    winner, loser = _pair(
        "2026-07-24T10:32:08+00:00",
        "2026-07-24T10:32:00+00:00",
    )

    assert apply(RULES, [winner, loser], window_seconds=60) == {
        loser.key: winner.key,
    }


def test_dedup_pair_distance_beyond_60s():
    winner, loser = _pair(
        "2026-07-24T10:34:00+00:00",
        "2026-07-24T10:32:00+00:00",
    )

    assert apply(RULES, [winner, loser], window_seconds=60) == {}


def test_dedup_pair_distance_not_age():
    winner, loser = _pair(
        "2026-07-24T10:32:52+00:00",
        "2026-07-24T10:32:44+00:00",
    )

    assert apply(RULES, [winner, loser], window_seconds=60) == {
        loser.key: winner.key,
    }


def test_dedup_no_source_ts_participates():
    winner, loser = _pair(None, "2026-07-24T10:32:00+00:00")

    assert apply(RULES, [winner, loser], window_seconds=60) == {
        loser.key: winner.key,
    }


def test_dedup_source_ts_parse_error_participates():
    winner, loser = _pair("not-a-timestamp", "2026-07-24T10:32:00+00:00")

    assert apply(RULES, [winner, loser], window_seconds=60) == {
        loser.key: winner.key,
    }
