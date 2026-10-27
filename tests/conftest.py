"""脚本层测试共享 fixture:mock SSH pool + 示例环境包 + 真实 TCP/外部网络/采集 tick 硬闸。"""
import ipaddress
import socket
from pathlib import Path

import pytest

from lib.cmd_filter import CmdFilter, default_rules_path
from lib.ssh_pool import CmdResult

CONCLUSIVE_JSON = (
    '{"playbook":"alpha/system/check_memory_pressure","node":"node12",'
    '"verdict":"conclusive","severity":"P1",'
    '"conclusion_hint":"慢性 swap 残留",'
    '"steps":[{"name":"swap_usage","cmd":"free -m","expect":"swap <50%",'
    '"actual":"100%","pass":false,"raw":""}],"metrics":{"swap_used_pct":100}}'
)


class FixturePool:
    """真 CmdFilter + 假 SSH:红线行为与生产一致,输出可注入。"""

    def __init__(self, playbook_stdout=CONCLUSIVE_JSON):
        self.cmds = []          # [(cmd, approved)]
        self.pushes = []
        self.playbook_stdout = playbook_stdout
        self._filter = CmdFilter.from_yaml(default_rules_path())

    def run(self, node, cmd, timeout=180.0, approved=False):
        self._filter.check(cmd, approved=approved)
        self.cmds.append((cmd, approved))
        stdout = self.playbook_stdout if "bash /tmp/opsagent/" in cmd else "OK"
        return CmdResult(node, cmd, 0, stdout, "", 0.1, timed_out=False)

    def push(self, node, local, remote):
        self.pushes.append((str(local), remote))

    def close(self):
        pass


@pytest.fixture
def fixture_pool():
    return FixturePool()


@pytest.fixture(autouse=True)
def no_real_tcp(monkeypatch):
    """硬闸:Transport 换成假对象时,直连建 TCP 也不能真的连出去。

    逐文件加漏过一次(只 mock Transport 的测试真去连了 198.51.100.112),故收到这里。
    需要真实 _open_socket 的测试直接 import 原函数,不受此替换影响。
    """
    monkeypatch.setattr(
        "lib.ssh_pool._open_socket", lambda host, port, timeout: (host, port),
    )


EXAMPLE_PACK = Path(__file__).resolve().parents[1] / "envs" / "example"


def force_env_pack(monkeypatch, pack: Path) -> None:
    """代码仓本机 paths.local.yaml 配的 env_pack 不算,一律挂 pack;测试自建数据根里配的(as_env_pack)照用。"""
    import lib.envpack as envpack
    from lib.data_root import local_config_path

    real = envpack.resolve_env_pack_dir
    own = (envpack.CODE_ROOT / "config" / "paths.local.yaml").resolve()

    def resolve(repo_root):
        config = local_config_path(repo_root, "paths.local.yaml")
        if config.resolve() != own and config.is_file() and envpack._configured_pack(config):
            return real(repo_root)
        return Path(pack).resolve()

    resolve.__wrapped__ = real    # 需要真实定位规则的用例经 inspect.unwrap 取回
    monkeypatch.setattr(envpack, "resolve_env_pack_dir", resolve)


@pytest.fixture(scope="session", autouse=True)
def example_env_pack():
    """核心测试一律挂示例环境包(envs/example),不随开发机配置的私有环境包变化。

    会话级:模块级夹具也要读环境包。与环境包测试同跑时按目录先后,后建的覆盖先建的。
    """
    with pytest.MonkeyPatch.context() as patch:
        force_env_pack(patch, EXAMPLE_PACK)
        yield


@pytest.fixture
def use_env_timezone(tmp_path_factory, monkeypatch):
    """用例内换环境时区:挂一份只改了 timezone 的示例环境包副本(测按环境时区的日历边界)。"""
    import shutil

    import yaml

    def apply(zone: str) -> Path:
        root = tmp_path_factory.mktemp("tz-pack") / "pack"
        shutil.copytree(EXAMPLE_PACK, root, ignore=shutil.ignore_patterns("__pycache__"))
        env = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
        env["timezone"] = zone
        (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
        force_env_pack(monkeypatch, root)
        return root

    return apply


@pytest.fixture(autouse=True)
def no_ambient_data_root(monkeypatch):
    """开发机 shell 若设了 OPS_AGENT_DATA_ROOT,测试不得跟着写过去;需要的用例自行 setenv。"""
    monkeypatch.delenv("OPS_AGENT_DATA_ROOT", raising=False)


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _loopback_only(address) -> None:
    host = address[0] if isinstance(address, tuple) else None
    if host is None:  # AF_UNIX 等非 IP 地址
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        if host == "localhost":
            return
    raise ConnectionRefusedError(f"测试禁止连接外部地址 {address!r}:请 mock 网络层")


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """硬闸:测试进程只许连回环地址,生产集群、插件与公网一律连不出去。"""
    def connect(self, address):
        _loopback_only(address)
        return _REAL_CONNECT(self, address)

    def connect_ex(self, address):
        _loopback_only(address)
        return _REAL_CONNECT_EX(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def no_real_acquisition_tick(monkeypatch):
    """硬闸:中台调度器在测试里不跑真实告警/资源 tick(会连插件与集群、写记录根)。

    环境配置随环境包,临时仓起中台也看得到告警源;需要 tick 的用例自行替换
    ``lib.scheduler.alert_poll.run`` / ``resource_collect.run``。直接调用脚本 ``run`` 的用例不受影响。
    """
    import lib.scheduler as scheduler_module

    def refuse(_argv):
        raise RuntimeError("测试禁止调度器跑真实采集 tick:请替换 lib.scheduler 的 alert_poll/resource_collect.run")

    monkeypatch.setattr(scheduler_module.alert_poll, "run", refuse)
    monkeypatch.setattr(scheduler_module.resource_collect, "run", refuse)
