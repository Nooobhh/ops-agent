import json
from unittest.mock import Mock

from lib.credentials import CredentialProviderUnavailable
from scripts import node_credentials


def _provider(monkeypatch, provider):
    monkeypatch.setattr(node_credentials, "node_credential_provider", lambda _pack: provider)


def test_cli_initializes_through_the_provider_and_reports(monkeypatch, capsys, tmp_path):
    provider = Mock()
    provider.initialize.return_value = {
        "192.0.2.1": {"ok": True, "status": "generated"},
        "192.0.2.2": {"ok": True, "status": "cached"},
    }
    _provider(monkeypatch, provider)
    constructor = Mock(return_value="creds")
    monkeypatch.setattr(node_credentials, "FileCredentials", constructor)
    path = tmp_path / "credentials.json"
    rc = node_credentials.run(["--ip-prefix", "192.0.2.", "--credentials", str(path)])
    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    constructor.assert_called_once_with(path, merge_home=True)
    provider.initialize.assert_called_once_with("creds", "192.0.2.")
    provider.generate_all.assert_not_called()
    assert output == {"ok": True, "generated": ["192.0.2.1"], "cached": ["192.0.2.2"],
                      "failed": [], "total": 2, "success": 2}


def test_replace_existing_is_the_only_path_to_generate_all(monkeypatch, capsys):
    provider = Mock()
    provider.generate_all.return_value = {"192.0.2.1": {"ok": True, "status": "generated"}}
    _provider(monkeypatch, provider)
    monkeypatch.setattr(node_credentials, "FileCredentials", Mock(return_value="creds"))
    assert node_credentials.run(["--replace-existing"]) == 0
    provider.generate_all.assert_called_once_with("creds", None, replace_existing=True)
    provider.initialize.assert_not_called()


def test_partial_failure_still_exits_zero(monkeypatch, capsys):
    provider = Mock()
    provider.initialize.return_value = {
        "192.0.2.1": {"ok": True, "status": "generated"},
        "192.0.2.2": {"ok": False, "error": "boom"},
    }
    _provider(monkeypatch, provider)
    monkeypatch.setattr(node_credentials, "FileCredentials", Mock())
    assert node_credentials.run([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["failed"] == [{"ip": "192.0.2.2", "error": "boom"}]


def test_provider_unavailable_is_strict_json_and_does_not_leak_secrets(monkeypatch, capsys):
    provider = Mock()
    provider.initialize.side_effect = CredentialProviderUnavailable("网关认证", FileNotFoundError("x"))
    _provider(monkeypatch, provider)
    monkeypatch.setattr(node_credentials, "FileCredentials", Mock())
    assert node_credentials.run([]) == 1
    raw = capsys.readouterr().out
    output = json.loads(raw)
    assert (output["ok"], output["code"]) == (False, "provider_unavailable")
    assert "password" not in raw and "totp_secret" not in raw


def test_all_failed_returns_generate_failed(monkeypatch, capsys):
    provider = Mock()
    provider.initialize.return_value = {"192.0.2.1": {"ok": False, "error": "no token"}}
    _provider(monkeypatch, provider)
    monkeypatch.setattr(node_credentials, "FileCredentials", Mock())
    assert node_credentials.run([]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "generate_failed"


def test_pack_without_provider_is_a_noop_success(monkeypatch, capsys):
    _provider(monkeypatch, None)
    assert node_credentials.run([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True and output["total"] == 0 and "node_credentials" in output["skipped"]
