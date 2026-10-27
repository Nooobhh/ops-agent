import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib.credentials import CredentialMissing, FileCredentials


def write_cache(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


def test_get_missing_expired_valid_and_legacy_without_auto_refresh(tmp_path):
    path = tmp_path / "credentials.json"
    write_cache(path, {
        "expiring": {"user": "u", "password": "p", "valid_to": int(time.time()) + 299},
        "valid": {"user": "u", "password": "p", "valid_to": int(time.time()) + 900},
        "legacy": {"user": "root", "password": "pw"},
    })
    creds = FileCredentials(path, merge_home=False)
    with pytest.raises(CredentialMissing):
        creds.get("missing")
    expiring = creds.get("expiring")
    assert 0 < expiring.valid_to - time.time() < 300
    assert creds.get("valid").valid_to > time.time()
    assert creds.get("legacy").valid_to == 0


def test_get_reuses_project_cache_even_when_near_expiry(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    write_cache(path, {"node": {
        "user": "u", "password": "p", "valid_to": int(time.time()) + 900,
    }})
    creds = FileCredentials(path, merge_home=False)
    assert creds.get("node").user == "u"


def test_delete_delegates_to_project_cache(tmp_path):
    creds = FileCredentials(tmp_path / "credentials.json", merge_home=False)
    creds._cache = Mock()
    creds.delete("node")
    creds._cache.delete.assert_called_once_with("node")


def test_merge_home_true_works_for_custom_project_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ops-agent").mkdir(parents=True)
    project_path = tmp_path / "custom" / "path.json"
    write_cache(project_path, {
        "project-only": {"user": "project", "password": "p"},
        "shared": {"user": "stale", "password": "old"},
    })
    write_cache(home / ".ops-agent" / "credentials.json", {
        "shared": {"user": "home", "password": "new"},
        "manager:alpha": {"user": "admin", "password": "manager"},
        "wecom:ops-alerts": {"webhook_key": "K"},
    })
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    creds = FileCredentials(project_path, merge_home=True)
    assert creds.get("project-only").user == "project"
    assert creds.get("shared").user == "home"
    assert creds.get("manager:alpha").user == "admin"
    assert creds._load_merged()["wecom:ops-alerts"]["webhook_key"] == "K"


def test_merge_home_false_is_project_local_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ops-agent").mkdir(parents=True)
    write_cache(home / ".ops-agent" / "credentials.json", {
        "home-only": {"user": "home", "password": "secret"},
    })
    project_path = tmp_path / "custom" / "path.json"
    write_cache(project_path, {"project-only": {"user": "project", "password": "p"}})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    creds = FileCredentials(project_path, merge_home=False)
    assert creds.get("project-only").user == "project"
    with pytest.raises(CredentialMissing):
        creds.get("home-only")


def test_merge_home_defaults_true(tmp_path):
    assert FileCredentials(tmp_path / "credentials.json").merge_home is True


def test_store_writes_cache_under_host_lock_and_returns_credential(tmp_path):
    """提供方生成后经 host_lock + store 写缓存;读回即通用 Credential。"""
    creds = FileCredentials(tmp_path / "credentials.json", merge_home=False)
    with creds.host_lock("198.51.100.105"):
        stored = creds.store("198.51.100.105", {"user": "token", "password": "pw", "pty": True, "mode": 0})
    assert (stored.user, stored.pty, stored.mode) == ("token", True, 0)
    assert creds.get("198.51.100.105").password == "pw"
    assert [item["state"] for item in creds.cache_status()] == ["long_lived"]
