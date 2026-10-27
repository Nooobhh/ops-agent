"""账号页 API 的通用行为:凭据项与节点凭据提供方来自环境包扩展(这里装一个假扩展)。"""
import json
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

from web.server.app import create_app
from tests.envpack_helpers import as_env_pack

EXT = '''
from lib.envpack.ext import CredentialField, CredentialProfileSpec, CredentialStoreError, CredentialTestFailed

CALLS = []


def _validate(value):
    user, password = str(value.get("user") or "").strip(), str(value.get("password") or "")
    if not user or not password:
        raise CredentialStoreError("需要 user 与 password")
    out = {"user": user, "password": password}
    if value.get("endpoint"):
        out["endpoint"] = str(value["endpoint"])
    return out


def _test(value, repo_root):
    CALLS.append(dict(value))
    if value["password"] == "bad":
        raise CredentialTestFailed("登录失败,请检查", "login_failed")
    return "登录成功"


def profiles(pack):
    return [CredentialProfileSpec(
        id=f"acct:{key}", title=f"{key} 账号", username_field="user", extra_fields=("endpoint",),
        fields=(CredentialField("user", "账号"), CredentialField("password", "密码", kind="secret")),
        validate=_validate, test=_test,
    ) for key in pack.cluster_keys("managed")]


class Provider:
    title = "演示节点凭据"
    description = "演示"
    generate_label = "生成"
    generate_confirm = "确认?"

    def __init__(self):
        self.calls = []

    def initialize(self, credentials, ip_prefix=None):
        return {}

    def generate_all(self, credentials, ip_prefix=None, *, replace_existing=True):
        self.calls.append((ip_prefix, replace_existing))
        return {"192.0.2.1": {"ok": True, "status": "generated"}}


PROVIDER = Provider()
'''


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = as_env_pack(tmp_path)
    env = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
    env["python"] = "python"
    env["extensions"] = {"credential_profiles": "fake_cred_ext:profiles",
                         "node_credentials": "fake_cred_ext:PROVIDER"}
    (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (root / "python").mkdir()
    (root / "python" / "fake_cred_ext.py").write_text(EXT, encoding="utf-8")
    yield root
    sys.modules.pop("fake_cred_ext", None)


def client_at(root):
    return TestClient(create_app(state_dir=root / "state", repo_root=root))


def test_profiles_come_from_the_extension_and_secrets_never_come_back(repo):
    client = client_at(repo)
    body = client.get("/api/credentials").json()
    assert body["schema_version"] == 2
    assert [item["id"] for item in body["profiles"]] == ["acct:alpha", "acct:beta"]
    assert body["profiles"][0]["fields"][1] == {
        "name": "password", "label": "密码", "kind": "secret", "required": True, "help": "",
    }
    assert body["node_credentials"]["provider"]["generate_label"] == "生成"
    assert body["ssh_test_platforms"] == [{"key": "alpha", "label": "Alpha"}, {"key": "beta", "label": "Beta"}]

    response = client.put("/api/credentials/profiles/acct:alpha", json={"user": "admin-user", "password": "s3cret"})
    assert response.status_code == 200
    saved = next(item for item in response.json()["profiles"] if item["id"] == "acct:alpha")
    assert saved["configured"] is True and saved["username_hint"] == "ad***r"
    assert "s3cret" not in json.dumps(response.json())
    stored = json.loads((repo / "config" / "auth-profiles.json").read_text(encoding="utf-8"))
    assert stored["profiles"]["acct:alpha"] == {"user": "admin-user", "password": "s3cret"}


def test_unknown_profile_field_and_incomplete_values_are_rejected(repo):
    client = client_at(repo)
    assert client.put("/api/credentials/profiles/acct:prod", json={}).status_code == 404
    assert client.put("/api/credentials/profiles/acct:alpha", json={"token": "x"}).status_code == 422
    assert client.put("/api/credentials/profiles/acct:alpha", json={"user": "only"}).status_code == 422
    assert client.put("/api/credentials/profiles/acct:alpha", json={"user": "u", "password": "p",
                                                                "endpoint": "http://x"}).status_code == 200
    assert client.delete("/api/credentials/profiles/unknown").status_code == 404
    assert client.delete("/api/credentials/profiles/acct:alpha").status_code == 200


def test_login_test_uses_input_without_persisting_and_records_result(repo):
    client = client_at(repo)
    client.put("/api/credentials/profiles/acct:alpha", json={"user": "admin", "password": "saved"})
    ok = client.post("/api/credentials/profiles/acct:alpha/test", json={"password": "temporary"}).json()
    assert ok == {**ok, "ok": True, "kind": "profile", "profile_id": "acct:alpha", "message": "登录成功"}
    ext = sys.modules["fake_cred_ext"]
    assert ext.CALLS[-1] == {"user": "admin", "password": "temporary"}
    stored = json.loads((repo / "config" / "auth-profiles.json").read_text(encoding="utf-8"))
    assert stored["profiles"]["acct:alpha"]["password"] == "saved"

    failed = client.post("/api/credentials/profiles/acct:alpha/test", json={"password": "bad"}).json()
    assert (failed["ok"], failed["error_code"], failed["message"]) == (False, "login_failed", "登录失败,请检查")
    missing = client.post("/api/credentials/profiles/acct:beta/test", json={}).json()
    assert (missing["ok"], missing["error_code"]) == (False, "invalid_config")
    profile = next(item for item in client.get("/api/credentials").json()["profiles"] if item["id"] == "acct:alpha")
    assert profile["last_test"]["status"] == "failure"


def test_node_credentials_are_redacted_and_generated_only_on_explicit_request(repo):
    (repo / "config" / "credentials.json").write_text(json.dumps({
        "192.0.2.1": {
            "user": "token-secret", "password": "password-secret", "target": "node1",
            "target_ip": "192.0.2.1", "mode": 0, "valid_from": 100, "valid_to": 200,
            "cached_at": "2026-09-08 00:00:00", "pty": True,
        },
    }), encoding="utf-8")
    client = client_at(repo)
    body = client.get("/api/credentials").json()
    node = next(item for item in body["node_credentials"]["nodes"] if item["host"] == "192.0.2.1")
    assert (node["mode"], node["valid_to"]) == (0, 200)
    assert "token-secret" not in json.dumps(body) and "password-secret" not in json.dumps(body)

    provider = sys.modules["fake_cred_ext"].PROVIDER
    assert provider.calls == []
    response = client.post("/api/credentials/node-credentials/generate")
    assert response.status_code == 200
    assert (response.json()["ok"], response.json()["generated"]) == (True, ["192.0.2.1"])
    assert provider.calls == [(None, True)]


def test_pack_without_credential_extensions_has_an_empty_account_page(tmp_path):
    client = client_at(as_env_pack(tmp_path))
    body = client.get("/api/credentials").json()
    assert body["profiles"] == [] and body["node_credentials"]["provider"] is None
    assert client.post("/api/credentials/node-credentials/generate").status_code == 404


def test_broken_extension_only_fails_the_account_page(repo):
    (repo / "python" / "fake_cred_ext.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    client = client_at(repo)
    response = client.get("/api/credentials")
    assert response.status_code == 500 and "环境包凭据扩展不可用" in response.json()["detail"]
    assert client.get("/api/health").status_code == 200


def test_account_page_no_longer_holds_mysql_accounts(repo):
    """MySQL 连接改在 MySQL 插件页按连接配置,账号页不再有 MySQL 账号与连接测试。"""
    client = client_at(repo)
    body = client.get("/api/credentials").json()
    assert not any(item["id"].startswith("mysql") for item in body["profiles"]) and "mysql_nodes" not in body
    assert client.put("/api/credentials/mysql/mysql", json={"user": "u", "password": "p"}).status_code in {404, 405}
