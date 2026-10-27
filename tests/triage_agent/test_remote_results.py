import pytest

from lib.ssh_pool import SSHAuthFailed
from lib.ssh_pool import CmdResult
from lib.triage_agent.actions import ActionGateway
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.paths import AppPaths
from lib.triage_agent.remote_session import RemoteExecutionError


class FailingPool:
    def run(self, node, cmd, **kwargs):
        raise SSHAuthFailed("auth failed", RuntimeError("secret should not leak"))


class StatefulPool:
    def __init__(self):
        self.active = False
        self.calls = []
        self.closed = False

    def transport_is_active(self, node):
        return self.active

    def run(self, node, cmd, **kwargs):
        self.calls.append((node, cmd))
        self.active = True
        return CmdResult(node, cmd, 0, "ok", "", 0.01)

    def route_mode(self):
        return "direct"

    def last_channel_mode(self, node):
        return "exec"

    def last_connection_attempts(self, node):
        return 1

    def last_credential_status(self, node):
        return "configured"

    def close(self):
        self.closed = True


def test_remote_execution_error_has_redacted_stable_result(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "agent-diagnostics.yaml").write_text(
        "version: 1\ndiagnostics: []\napproved_actions: []\n",
        encoding="utf-8",
    )
    registry = DiagnosticRegistry.load(config / "agent-diagnostics.yaml")
    gateway = ActionGateway(
        AppPaths.from_repo_root(tmp_path),
        registry,
        pool_factory=lambda _platform: FailingPool(),
    )

    with pytest.raises(RemoteExecutionError) as exc_info:
        gateway.run_command(
            "run-1",
            platform="alpha",
            node="node1",
            pipeline=[["hostname"]],
        )

    result = exc_info.value.result
    assert result["ok"] is False
    assert result["operation"] == "run_command"
    assert result["target"] == {"platform": "alpha", "node": "node1"}
    assert result["error"] == {
        "phase": "authentication",
        "kind": "SSHAuthFailed",
        "retryable": True,
        "message": "节点认证失败，请检查账号页的节点凭据与认证配置",
    }
    assert "secret" not in str(result)


def test_action_gateway_reuses_connection_for_same_execution(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "agent-diagnostics.yaml").write_text(
        "version: 1\ndiagnostics: []\napproved_actions: []\n",
        encoding="utf-8",
    )
    registry = DiagnosticRegistry.load(config / "agent-diagnostics.yaml")
    pool = StatefulPool()
    gateway = ActionGateway(
        AppPaths.from_repo_root(tmp_path),
        registry,
        pool_factory=lambda _platform: pool,
    )
    record = gateway.start_session(
        platform="alpha", node="node1", trigger="test",
    )

    first = gateway.run_command(
        record.run_id,
        platform="alpha", node="node1", pipeline=[["hostname"]],
        execution_id="exec-1",
    )
    second = gateway.run_command(
        record.run_id,
        platform="alpha", node="node1", pipeline=[["uptime"]],
        execution_id="exec-1",
    )

    assert first["connection"]["status"] == "opened"
    assert second["connection"]["status"] == "reused"
    assert pool.calls == [("node1", "hostname"), ("node1", "uptime")]
    gateway.close_task_session("exec-1")
    assert pool.closed is True
