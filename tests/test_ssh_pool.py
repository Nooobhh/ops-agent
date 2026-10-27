import pytest

from lib.cmd_filter import CmdFilter, CommandDenied, default_rules_path
from lib.config import NodeInfo, PlatformConfig
from lib.credentials import Credential
from lib.ssh_pool import PushDenied, SSHPool, UnknownNode


class FakeChan:
    def __init__(self, stdout=b"", stderr=b"", exit_code=0, hang=False):
        self._out, self._err = bytearray(stdout), bytearray(stderr)
        self._exit, self._hang = exit_code, hang
        self.closed = False
        self.cmd = None

    def settimeout(self, t):
        pass

    def exec_command(self, cmd):
        self.cmd = cmd

    def invoke_subsystem(self, name):
        self.subsystem = name

    def recv_ready(self):
        return not self._hang and bool(self._out)

    def recv(self, n):
        b = bytes(self._out[:n]); del self._out[:n]; return b

    def recv_stderr_ready(self):
        return not self._hang and bool(self._err)

    def recv_stderr(self, n):
        b = bytes(self._err[:n]); del self._err[:n]; return b

    def exit_status_ready(self):
        return not self._hang and not self._out and not self._err

    def recv_exit_status(self):
        return self._exit

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, chans):
        self._chans = list(chans)
        self.opened = 0

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        self.opened += 1
        return self._chans.pop(0)


@pytest.fixture
def pool():
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})

    class Creds:
        def get(self, host):
            return Credential("root", "pw")

    return SSHPool(platform, Creds(), CmdFilter.from_yaml(default_rules_path()))


def test_run_basic(pool):
    ft = FakeTransport([FakeChan(stdout=b"ok\n", exit_code=0)])
    pool._transports["node12"] = ft
    r = pool.run("node12", "echo ok")
    assert (r.stdout, r.exit_code, r.timed_out) == ("ok\n", 0, False)


def test_unknown_node_raises_named_error(pool):
    """裸 KeyError 只回显节点名,上层既读不懂也判不了责,必须换成具名异常。"""
    with pytest.raises(UnknownNode) as excinfo:
        pool.run("node12.hd.com", "echo ok")
    assert "node12.hd.com" in str(excinfo.value)
    assert "alpha" in str(excinfo.value)


def test_denied_write_opens_no_channel(pool):
    ft = FakeTransport([])
    pool._transports["node12"] = ft
    with pytest.raises(CommandDenied):
        pool.run("node12", "rm /tmp/x")
    assert ft.opened == 0


def test_approved_write_runs(pool):
    ft = FakeTransport([FakeChan(stdout=b"", exit_code=0)])
    pool._transports["node12"] = ft
    assert pool.run("node12", "kill -9 123", approved=True).exit_code == 0


def test_timeout_keeps_transport(pool):
    ft = FakeTransport([FakeChan(hang=True), FakeChan(stdout=b"alive\n", exit_code=0)])
    pool._transports["node12"] = ft
    r1 = pool.run("node12", "sleep 999", timeout=0.3)
    assert r1.timed_out and r1.exit_code is None
    r2 = pool.run("node12", "echo alive")          # 同一 transport 直接复用
    assert r2.stdout == "alive\n"
    assert ft.opened == 2 and pool._transports["node12"] is ft


def test_push_outside_workdir_denied(pool):
    with pytest.raises(PushDenied):
        pool.push("node12", __file__, "/etc/evil.sh")


def test_push_path_traversal_denied(pool):
    ft = FakeTransport([])
    pool._transports["node12"] = ft
    with pytest.raises(PushDenied):
        pool.push("node12", __file__, "/tmp/opsagent/../../../etc/cron.d/evil")


def test_push_normalized_path_used(pool, monkeypatch):
    """守卫通过后,SFTP put 的目标必须是 normalize 后的路径,不能是带 .. 的原串。"""
    ft = FakeTransport([FakeChan()])
    pool._transports["node12"] = ft
    seen = {}
    class FakeSFTP:
        def __init__(self, chan): pass
        def put(self, local, remote): seen["remote"] = remote
        def close(self): pass
    monkeypatch.setattr("lib.ssh_pool.paramiko.SFTPClient", FakeSFTP)
    # 合法带内部 .. 的路径(normalize 后仍在 /tmp/opsagent/ 下)
    pool.push("node12", __file__, "/tmp/opsagent/run-1/sub/../foo.sh")
    assert seen["remote"] == "/tmp/opsagent/run-1/foo.sh"


def test_transport_uses_credential_host_port_when_provided(pool, monkeypatch):
    """凭证含 host+port 时,SSH 打真实目标,不是 platform.host。"""
    from lib.credentials import Credential
    class Creds:
        def get(self, host): return Credential("tp", "pw", host="203.0.113.250", port=2222)
    p = SSHPool(pool._platform, Creds(), pool._filter)
    seen = {}
    class FakeT:
        def __init__(self, sock, *a, **kw): seen["sock"] = sock
        def connect(self, **kw): seen["kw"] = kw
        def set_keepalive(self, s): pass
        def is_active(self): return True
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeT)
    p._transport("node12")
    assert seen["sock"] == ("203.0.113.250", 2222)   # 用了凭证里的目标
    assert seen["kw"] == {"username": "tp", "password": "pw"}


def test_transport_falls_back_to_platform_host_when_no_cred_host(pool, monkeypatch):
    """凭证无 host+port(旧格式)时,回落到 platform.host + 22。"""
    from lib.credentials import Credential
    class Creds:
        def get(self, host): return Credential("root", "pw")     # host=None, port=22
    p = SSHPool(pool._platform, Creds(), pool._filter)
    seen = {}
    class FakeT:
        def __init__(self, sock, *a, **kw): seen["sock"] = sock
        def connect(self, **kw): pass
        def set_keepalive(self, s): pass
        def is_active(self): return True
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeT)
    p._transport("node12")
    assert seen["sock"] == ("198.51.100.112", 22)         # platform.host + 默认 22
