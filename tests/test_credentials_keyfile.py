import json
from pathlib import Path

import pytest

from lib.cmd_filter import CmdFilter, default_rules_path
from lib.config import NodeInfo, PlatformConfig
from lib.credentials import FileCredentials
from lib.ssh_pool import SSHPool


def test_file_credentials_parses_key_file_and_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "creds.json"
    f.write_text(
        json.dumps({
            "198.51.100.112": {
                "user": "root",
                "key_file": "~/.ssh/id_ed25519",
                "host": "127.0.0.1",
                "port": 2222,
            },
            "198.51.100.113": {"user": "root", "password": "pw"},
        }),
        encoding="utf-8",
    )

    key_cred = FileCredentials(f, merge_home=False).get("198.51.100.112")
    password_cred = FileCredentials(f, merge_home=False).get("198.51.100.113")

    assert key_cred.password is None
    assert key_cred.key_file == str(Path("~/.ssh/id_ed25519").expanduser())
    assert (key_cred.host, key_cred.port) == ("127.0.0.1", 2222)
    assert (password_cred.password, password_cred.key_file) == ("pw", None)


def test_transport_connects_with_pkey_when_key_file_present(monkeypatch, tmp_path):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake", encoding="utf-8")
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})

    class Creds:
        def get(self, host):
            from lib.credentials import Credential

            return Credential("root", key_file=str(key_file))

    seen = {}

    class FakeKey:
        pass

    class FakeEd25519:
        @classmethod
        def from_private_key_file(cls, path):
            seen["key_path"] = path
            return FakeKey()

    class FakeRSA:
        @classmethod
        def from_private_key_file(cls, path):
            raise AssertionError("RSA should not be tried after Ed25519 succeeds")

    class FakeTransport:
        def __init__(self, sock):
            seen["sock"] = sock

        def connect(self, **kwargs):
            seen["connect"] = kwargs

        def set_keepalive(self, seconds):
            seen["keepalive"] = seconds

        def is_active(self):
            return True

    monkeypatch.setattr("lib.ssh_pool.paramiko.Ed25519Key", FakeEd25519)
    monkeypatch.setattr("lib.ssh_pool.paramiko.RSAKey", FakeRSA)
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)

    pool = SSHPool(platform, Creds(), CmdFilter.from_yaml(default_rules_path()))
    pool._transport("node12")

    assert seen["key_path"] == str(key_file)
    assert seen["connect"]["username"] == "root"
    assert isinstance(seen["connect"]["pkey"], FakeKey)


def test_transport_requires_password_or_key_file(monkeypatch):
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})

    class Creds:
        def get(self, host):
            from lib.credentials import Credential

            return Credential("root")

    pool = SSHPool(platform, Creds(), CmdFilter.from_yaml(default_rules_path()))

    with pytest.raises(ValueError, match="198.51.100.112"):
        pool._transport("node12")
