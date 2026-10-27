from datetime import timedelta

from lib.alerts.state import AlertsState

from tests.test_web_routes_alerts import _fixture


def prepare(tmp_path):
    client, now, winner, loser, historical_key, _ = _fixture(tmp_path)
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    state.active[winner.key]["class"] = "watch"
    state.active[winner.key]["first_seen"] = (now - timedelta(hours=1)).isoformat()
    state.active[loser.key]["class"] = "chronic"
    state.active[loser.key]["first_seen"] = (now - timedelta(hours=48)).isoformat()
    state.save()
    return client, winner, loser, historical_key


def test_class_query_filters_active(tmp_path):
    client, _winner, loser, historical = prepare(tmp_path)
    items = client.get("/api/alerts", params={"class": "chronic"}).json()["items"]
    assert {item["key"] for item in items} == {loser.key, historical}
    assert [item["key"] for item in items if item["status"] != "resolved"] == [loser.key]
    assert all(item["effective_class"] == "chronic" for item in items)


def test_class_multi_select(tmp_path):
    client, winner, loser, historical = prepare(tmp_path)
    items = client.get(
        "/api/alerts", params={"class": "watch,chronic"},
    ).json()["items"]
    assert {item["key"] for item in items} == {winner.key, loser.key, historical}


def test_class_ignore_matches_muted(tmp_path):
    client, winner, _loser, _ = prepare(tmp_path)
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    state.active[winner.key]["muted"] = True
    state.save()
    items = client.get("/api/alerts", params={"class": "ignore"}).json()["items"]
    assert [item["key"] for item in items] == [winner.key]


def test_chronic_legacy_still_works(tmp_path):
    client, _winner, loser, _ = prepare(tmp_path)
    items = client.get("/api/alerts", params={"chronic": "only"}).json()["items"]
    assert {item["key"] for item in items} == {loser.key}


def test_class_and_chronic_combined(tmp_path):
    client, winner, loser, _ = prepare(tmp_path)
    assert client.get("/api/alerts", params={
        "class": "chronic", "chronic": "only",
    }).json()["items"][0]["key"] == loser.key
    assert client.get("/api/alerts", params={
        "class": "watch", "chronic": "only",
    }).json()["items"] == []
    assert winner.key != loser.key


def test_class_invalid_value_422(tmp_path):
    client, *_ = prepare(tmp_path)
    response = client.get("/api/alerts", params={"class": "foo"})
    assert response.status_code == 422
    assert response.json()["detail"] == "class 值不正确"
