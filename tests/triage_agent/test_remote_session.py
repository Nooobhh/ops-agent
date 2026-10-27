from dataclasses import dataclass

from lib.ssh_pool import CmdResult
from lib.triage_agent.remote_session import TaskRemoteSession


@dataclass
class FakePool:
    active: bool = False
    runs: int = 0
    closes: int = 0

    def run(self, node, cmd, **kwargs):
        self.runs += 1
        self.active = True
        return CmdResult(node, cmd, 0, "ok", "", 0.01)

    def push(self, node, local, remote, **kwargs):
        self.active = True

    def transport_is_active(self, node):
        return self.active

    def route_mode(self):
        return "direct"

    def last_channel_mode(self, node):
        return "exec"

    def last_connection_attempts(self, node):
        return 1

    def last_credential_status(self, node):
        return "configured"

    def close(self):
        self.closes += 1
        self.active = False


def test_task_remote_session_reuses_node_transport_and_closes_once():
    pools = []

    def factory(platform):
        pool = FakePool()
        pools.append((platform, pool))
        return pool

    session = TaskRemoteSession("exec-1", factory)
    first, first_info = session.run("alpha", "node1", "hostname")
    second, second_info = session.run("alpha", "node1", "uptime")

    assert first.stdout == "ok"
    assert second.stdout == "ok"
    assert first_info.status == "opened"
    assert first_info.reused is False
    assert second_info.status == "reused"
    assert second_info.reused is True
    assert len(pools) == 1
    assert pools[0][1].runs == 2

    session.close()
    session.close()
    assert pools[0][1].closes == 1


def test_task_remote_sessions_are_isolated_by_execution_id():
    pools = []

    def factory(platform):
        pool = FakePool()
        pools.append(pool)
        return pool

    first = TaskRemoteSession("exec-1", factory)
    second = TaskRemoteSession("exec-2", factory)
    first.run("alpha", "node1", "hostname")
    second.run("alpha", "node1", "hostname")

    assert len(pools) == 2
    assert pools[0] is not pools[1]
    first.close()
    assert pools[0].closes == 1
    assert pools[1].closes == 0


def test_task_remote_session_marks_reconnect_after_a_dead_transport():
    pool = FakePool()
    session = TaskRemoteSession("exec-1", lambda _platform: pool)
    _, first_info = session.run("alpha", "node1", "hostname")
    pool.active = False
    _, next_info = session.run("alpha", "node1", "uptime")

    assert first_info.status == "opened"
    assert next_info.status == "reconnected"
    assert next_info.reused is False
