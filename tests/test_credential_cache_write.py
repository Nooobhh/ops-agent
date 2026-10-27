import json
import multiprocessing

import pytest

from lib.credential_cache import CredentialCache


def _cache_set(path, host, password):
    CredentialCache(path).set(host, {
        "host": "203.0.113.250",
        "port": 2222,
        "user": "tp",
        "password": password,
        "target": host,
        "valid_to": 2_000_000_000,
    })


def test_set_get_and_missing(tmp_path):
    path = tmp_path / "config" / "credentials.json"
    cache = CredentialCache(path)
    assert cache.get("198.51.100.101") is None
    cache.set("198.51.100.101", {
        "host": "bastion", "port": 22, "user": "u", "password": "p",
        "valid_to": 1_900_000_000,
    })
    record = cache.get("198.51.100.101")
    assert record["password"] == "p"
    assert record["valid_to"] == 1_900_000_000
    assert record["cached_at"]


def test_concurrent_set_preserves_both_hosts(tmp_path):
    path = tmp_path / "credentials.json"
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_cache_set, args=(path, f"198.51.100.{100 + n}", f"p{n}"))
        for n in (1, 2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"198.51.100.101", "198.51.100.102"}


def test_replace_failure_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"old": {"user": "u", "password": "p"}}), encoding="utf-8")
    original = path.read_bytes()

    def fail_replace(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr("lib.credential_cache.os.replace", fail_replace)
    with pytest.raises(RuntimeError, match="interrupted"):
        CredentialCache(path).set("new", {"user": "u", "password": "p"})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".credentials.json.*.tmp"))


def test_delete_preserves_other_hosts_and_status_hides_password(tmp_path):
    path = tmp_path / "credentials.json"
    cache = CredentialCache(path)
    cache.set("one", {"user": "u", "password": "secret"})
    cache.set("two", {"user": "u2", "password": "secret2"})
    cache.delete("one")
    assert cache.get("one") is None
    assert cache.get("two")["password"] == "secret2"
    status = cache.get_status()
    assert all("password" not in item for item in status)
    assert all("user" not in item for item in status)
    assert status[0]["bastion"] == ":22"


def test_legacy_record_defaults_valid_to_to_zero(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"198.51.100.104": {"user": "root", "password": "pw"}}),
                    encoding="utf-8")
    cache = CredentialCache(path)
    assert cache.get("198.51.100.104")["valid_to"] == 0
    assert cache.list_hosts() == ["198.51.100.104"]


def test_corrupt_json_is_read_as_empty_but_not_overwritten(tmp_path, capsys):
    path = tmp_path / "credentials.json"
    path.write_text("{broken", encoding="utf-8")
    cache = CredentialCache(path)
    assert cache.get("x") is None
    assert "WARN credentials cache JSON 损坏" in capsys.readouterr().err
    with pytest.raises(ValueError, match="拒绝覆盖"):
        cache.set("x", {"user": "u", "password": "p"})
    assert path.read_text(encoding="utf-8") == "{broken"
