from fastapi.testclient import TestClient

from lib.alerts.channels import load_channels, save_channel
from web.server.app import create_app


WECOM_KEY = "wecom-secret-5aaa"
FEISHU_TOKEN = "feishu-secret-9bbb"
FEISHU_URL = f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_TOKEN}"


def _client(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir, TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))


def test_get_reports_plain_and_masked_credentials_and_signature_state(tmp_path):
    state_dir, client = _client(tmp_path)
    save_channel(
        state_dir,
        "wecom",
        enabled=True,
        credentials_update={"webhook_key": WECOM_KEY},
    )
    save_channel(
        state_dir,
        "feishu",
        enabled=False,
        credentials_update={"webhook": FEISHU_URL, "secret": "sign-secret"},
    )

    response = client.get("/api/notify/channels")

    assert response.status_code == 200
    assert response.json()["wecom"] == {
        "enabled": True,
        "configured": True,
        "hint": "尾号 5aaa",
        "credential_masked": "weco****5aaa",
        "credential_plain": WECOM_KEY,
        "updated_at": load_channels(state_dir)["wecom"]["updated_at"],
    }
    assert response.json()["feishu"]["hint"] == "尾号 9bbb"
    assert response.json()["feishu"]["credential_masked"] == "…/hook/feis****9bbb"
    assert response.json()["feishu"]["credential_plain"] == FEISHU_URL
    assert response.json()["feishu"]["secret_plain"] == "sign-secret"
    assert response.json()["feishu"]["signed"] is True


def test_get_unconfigured_channels_reports_null_plain_credentials(tmp_path):
    _, client = _client(tmp_path)

    body = client.get("/api/notify/channels").json()

    assert body["wecom"]["credential_plain"] is None
    assert body["feishu"]["credential_plain"] is None
    assert body["feishu"]["secret_plain"] is None


def test_notification_markdown_template_settings_roundtrip(tmp_path):
    _, client = _client(tmp_path)

    initial = client.get("/api/notify/settings")
    assert initial.status_code == 200
    assert initial.json()["format"] == "markdown"
    assert "{title}" in initial.json()["default_template"]
    assert initial.json()["template"] == ""

    saved = client.put("/api/notify/settings", json={
        "format": "markdown",
        "template": "## {severity_upper} {title}\n> {description}",
    })
    assert saved.status_code == 200
    assert saved.json()["template"].startswith("## {severity_upper}")
    assert saved.json()["effective_template"] == saved.json()["template"]

    invalid = client.put("/api/notify/settings", json={
        "format": "markdown", "template": "{unknown}",
    })
    assert invalid.status_code == 422


def test_post_save_partial_update_unknown_and_missing_credentials(tmp_path):
    state_dir, client = _client(tmp_path)

    assert client.post("/api/notify/channels/unknown", json={"enabled": False}).status_code == 404
    missing = client.post("/api/notify/channels/wecom", json={"enabled": True})
    assert missing.status_code == 422
    assert "先填写" in missing.json()["detail"]

    saved = client.post("/api/notify/channels/wecom", json={
        "enabled": True,
        "webhook_key": WECOM_KEY,
    })
    assert saved.status_code == 200
    assert saved.json()["wecom"]["configured"] is True
    updated = client.post("/api/notify/channels/wecom", json={
        "enabled": False,
        "webhook_key": "",
    })
    assert updated.status_code == 200
    assert load_channels(state_dir)["wecom"]["webhook_key"] == WECOM_KEY
    assert load_channels(state_dir)["wecom"]["enabled"] is False
    assert client.post("/api/notify/channels/wecom", json={
        "enabled": True,
        "webhook_key": "bad key!",
    }).status_code == 422


def test_feishu_webhook_is_limited_to_official_secure_endpoint(tmp_path):
    _, client = _client(tmp_path)
    invalid = [
        "https://example.com/open-apis/bot/v2/hook/token",
        "http://open.feishu.cn/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn/open-apis/bot/v2/hook/token?redirect=x",
        "https://open.feishu.cn/another/path/token",
        "https://user@example.com/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn:444/open-apis/bot/v2/hook/token",
    ]
    for webhook in invalid:
        response = client.post("/api/notify/channels/feishu", json={
            "enabled": False,
            "webhook": webhook,
        })
        assert response.status_code == 422, webhook

    response = client.post("/api/notify/channels/feishu", json={
        "enabled": True,
        "webhook": FEISHU_URL,
    })
    assert response.status_code == 200


def test_feishu_secret_has_preserve_clear_and_replace_states(tmp_path):
    state_dir, client = _client(tmp_path)
    save_channel(
        state_dir,
        "feishu",
        enabled=True,
        credentials_update={"webhook": FEISHU_URL, "secret": "old-secret"},
    )

    preserved = client.post("/api/notify/channels/feishu", json={"enabled": True})
    assert preserved.status_code == 200
    assert load_channels(state_dir)["feishu"]["secret"] == "old-secret"

    cleared = client.post("/api/notify/channels/feishu", json={
        "enabled": True,
        "secret": "",
    })
    assert cleared.status_code == 200
    assert cleared.json()["feishu"]["signed"] is False
    assert cleared.json()["feishu"]["secret_plain"] is None
    assert load_channels(state_dir)["feishu"]["secret"] == ""

    replaced = client.post("/api/notify/channels/feishu", json={
        "enabled": True,
        "secret": "new-secret",
    })
    assert replaced.status_code == 200
    assert replaced.json()["feishu"]["signed"] is True
    assert load_channels(state_dir)["feishu"]["secret"] == "new-secret"


def test_saved_channel_probe_result_is_forwarded_without_queue_file(tmp_path, monkeypatch):
    state_dir, client = _client(tmp_path)
    missing = client.post("/api/notify/channels/wecom/test")
    assert missing.status_code == 422

    save_channel(
        state_dir,
        "wecom",
        enabled=False,
        credentials_update={"webhook_key": WECOM_KEY},
    )
    seen = []

    def fake_probe(self, content):
        seen.append((self.webhook_key, content))
        return {"ok": False, "code": 40001, "msg": "invalid key"}

    monkeypatch.setattr("web.server.routes.notify.WecomNotifier.probe", fake_probe)
    response = client.post("/api/notify/channels/wecom/test")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "code": 40001, "msg": "invalid key"}
    assert seen[0][0] == WECOM_KEY
    assert "OpsAgent 通知渠道测试消息" in seen[0][1]
    assert not state_dir.joinpath(".notify-probe-unused.jsonl").exists()


def test_channel_probe_uses_body_credentials_without_saving(tmp_path, monkeypatch):
    state_dir, client = _client(tmp_path)
    save_channel(
        state_dir,
        "wecom",
        enabled=True,
        credentials_update={"webhook_key": WECOM_KEY},
    )
    override_key = "override-key-1234"
    seen = []

    def fake_wecom_probe(self, content):
        seen.append((self.webhook_key, content))
        return {"ok": True, "code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "web.server.routes.notify.WecomNotifier.probe", fake_wecom_probe,
    )
    response = client.post(
        "/api/notify/channels/wecom/test",
        json={"webhook_key": override_key},
    )

    assert response.status_code == 200
    assert seen[0][0] == override_key
    assert load_channels(state_dir)["wecom"]["webhook_key"] == WECOM_KEY


def test_channel_probe_validates_body_overrides(tmp_path):
    _, client = _client(tmp_path)

    invalid_wecom = client.post(
        "/api/notify/channels/wecom/test",
        json={"webhook_key": "bad<>key"},
    )
    invalid_feishu = client.post(
        "/api/notify/channels/feishu/test",
        json={"webhook": "https://example.com/not-a-feishu-hook"},
    )
    missing = client.post("/api/notify/channels/feishu/test")

    assert invalid_wecom.status_code == 422
    assert invalid_wecom.json()["detail"] == "企微 webhook key 格式不正确"
    assert invalid_feishu.status_code == 422
    assert missing.status_code == 422
    assert missing.json()["detail"] == "该渠道尚未配置凭证"


def test_feishu_probe_uses_body_webhook_and_secret(tmp_path, monkeypatch):
    state_dir, client = _client(tmp_path)
    override_url = (
        "https://open.feishu.cn/open-apis/bot/v2/hook/override-token-1234"
    )
    seen = []

    def fake_feishu_probe(self, content):
        seen.append((self.webhook_url, self.secret, content))
        return {"ok": True, "code": 0, "msg": "ok"}

    monkeypatch.setattr(
        "web.server.routes.notify.FeishuNotifier.probe", fake_feishu_probe,
    )
    response = client.post(
        "/api/notify/channels/feishu/test",
        json={"webhook": override_url, "secret": "override-sign-secret"},
    )

    assert response.status_code == 200
    assert seen[0][:2] == (override_url, "override-sign-secret")
    assert load_channels(state_dir).get("feishu") is None
