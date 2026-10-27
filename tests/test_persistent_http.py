from lib import persistent_http


class FakeResponse:
    status_code = 200

    def json(self):
        return {"ok": True}


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def test_client_disables_proxy_and_retries_only_connections():
    client = persistent_http.PersistentJsonClient()

    assert client.session.trust_env is False
    retry = client.session.get_adapter("http://").max_retries
    assert retry.connect == persistent_http.CONNECT_RETRIES
    assert retry.read == 0
    assert retry.status == 0


def test_client_reuses_session_and_passes_bounded_timeouts():
    client = persistent_http.PersistentJsonClient()
    fake = FakeSession()
    client.session = fake

    assert client.get_json(
        "http://manager:8080/api/v1/clusters",
        auth=("user", "password"),
        timeout_seconds=30,
        headers={"X-Requested-By": "ops-agent"},
    ) == {"ok": True}
    assert client.get_json(
        "http://manager:8080/api/v1/alerts",
        auth=("user", "password"),
        timeout_seconds=30,
    ) == {"ok": True}

    assert len(fake.calls) == 2
    assert fake.calls[0][1]["timeout"] == (3.0, 30.0)
    assert fake.calls[0][1]["auth"] == ("user", "password")


def test_process_pool_reuses_client_for_same_origin(monkeypatch):
    created = []

    class SpyClient:
        def __init__(self):
            created.append(self)

        def get_json(self, url, **kwargs):
            return {"url": url}

    monkeypatch.setattr(persistent_http, "PersistentJsonClient", SpyClient)
    monkeypatch.setattr(persistent_http, "_CLIENTS", {})

    persistent_http.persistent_json_get("http://manager:8080/a")
    persistent_http.persistent_json_get("http://manager:8080/b")
    persistent_http.persistent_json_get("http://other:8080/a")

    assert len(created) == 2
