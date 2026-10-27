import json
from pathlib import Path

from lib.config import load_platform, load_local_dev
from lib.credentials import FileCredentials


def test_load_platform(tmp_path: Path):
    p = tmp_path / "platform-alpha.yaml"
    p.write_text(
        "platform: alpha\n"
        "nodes:\n"
        "  node1: {host: 198.51.100.101, components: [NAMENODE]}\n"
        "  node12: {host: 198.51.100.112, components: [DATANODE, NODEMANAGER]}\n",
        encoding="utf-8",
    )
    cfg = load_platform(p)
    assert cfg.platform == "alpha"
    assert cfg.nodes["node12"].host == "198.51.100.112"
    assert "DATANODE" in cfg.nodes["node12"].components


def test_load_local_dev_with_jump(tmp_path: Path):
    p = tmp_path / "local-dev.yaml"
    p.write_text(
        "jump: {host: 1.2.3.4, user: worker, key_file: ~/.ssh/id_ed25519}\n"
        "credentials_file: /x/creds.json\n",
        encoding="utf-8",
    )
    dev = load_local_dev(p)
    assert dev.jump.host == "1.2.3.4"
    assert dev.jump.user == "worker"
    assert dev.jump.key_file == str(Path("~/.ssh/id_ed25519").expanduser())
    assert dev.credentials_file == Path("/x/creds.json")


def test_load_local_dev_missing_returns_none(tmp_path: Path):
    assert load_local_dev(tmp_path / "nope.yaml") is None


def test_file_credentials(tmp_path: Path):
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"198.51.100.112": {"user": "root", "password": "pw"}}), encoding="utf-8")
    c = FileCredentials(f, merge_home=False).get("198.51.100.112")
    assert (c.user, c.password) == ("root", "pw")


def test_file_credentials_with_host_port(tmp_path: Path):
    """新格式:含 host+port 表示堡垒机隧道真实目标。"""
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({
        "192.0.2.22": {"user": "tpxxx", "password": "pw", "host": "203.0.113.250", "port": 2222}
    }), encoding="utf-8")
    c = FileCredentials(f, merge_home=False).get("192.0.2.22")
    assert (c.user, c.password, c.host, c.port) == ("tpxxx", "pw", "203.0.113.250", 2222)


def test_file_credentials_legacy_format_backward_compat(tmp_path: Path):
    """旧格式:只有 user/password,host=None,port=22——不 break Task 4 现有用例。"""
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"198.51.100.112": {"user": "root", "password": "pw"}}), encoding="utf-8")
    c = FileCredentials(f, merge_home=False).get("198.51.100.112")
    assert (c.user, c.password, c.host, c.port) == ("root", "pw", None, 22)
