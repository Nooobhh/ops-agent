from pathlib import Path

import pytest

from lib.ssh_pool import CmdResult
from lib.triage_agent.actions import (
    DIAGNOSTIC_MAX_OUTPUT_BYTES,
    ActionGateway,
)
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.paths import AppPaths


class FakePool:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(
        self,
        node,
        cmd,
        timeout=30,
        approved=False,
        max_output_bytes=None,
    ):
        self.calls.append(
            {
                "node": node,
                "cmd": cmd,
                "timeout": timeout,
                "approved": approved,
                "max_output_bytes": max_output_bytes,
            }
        )
        return self.result


def _gateway(tmp_path, result):
    config = tmp_path / "config"
    config.mkdir()
    diagnostics_path = config / "agent-diagnostics.yaml"
    diagnostics_path.write_text(
        """
version: 1
diagnostics:
  - id: output.test
    platforms: [alpha]
    params: {}
    command: uptime
    timeout_seconds: 10
approved_actions: []
""",
        encoding="utf-8",
    )
    pool = FakePool(result)
    gateway = ActionGateway(
        AppPaths.from_repo_root(tmp_path),
        DiagnosticRegistry.load(diagnostics_path),
        pool_factory=lambda _platform: pool,
    )
    session = gateway.start_session(
        platform="alpha", node="node1", trigger="test:limit", service="system"
    )
    return gateway, pool, session


def test_ssh_diagnostic_passes_one_megabyte_limit_and_reports_truncation(tmp_path):
    oversized = "x" * (DIAGNOSTIC_MAX_OUTPUT_BYTES + 1)
    result = CmdResult(
        "node1", "uptime", 0, oversized, "", 0.1, truncated=True
    )
    gateway, pool, session = _gateway(tmp_path, result)

    output = gateway.run_diagnostic(
        session.run_id,
        platform="alpha",
        node="node1",
        diagnostic_id="output.test",
        params={},
    )

    assert pool.calls[0]["max_output_bytes"] == 1024 * 1024
    assert output["truncated"] is True
    assert "output.test" in output["stderr"]


def test_small_ssh_diagnostic_is_not_truncated(tmp_path):
    result = CmdResult("node1", "uptime", 0, "small", "", 0.1)
    gateway, pool, session = _gateway(tmp_path, result)

    output = gateway.run_diagnostic(
        session.run_id,
        platform="alpha",
        node="node1",
        diagnostic_id="output.test",
        params={},
    )

    assert pool.calls[0]["max_output_bytes"] == DIAGNOSTIC_MAX_OUTPUT_BYTES
    assert output["stdout"] == "small"
    assert output["truncated"] is False


def test_http_reader_uses_bounded_read(monkeypatch, tmp_path):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            seen["size"] = size
            return b"{}"

    class Opener:
        def open(self, _request, timeout):
            return Response()

    monkeypatch.setattr(
        "lib.triage_agent.actions.urlrequest.build_opener",
        lambda *_handlers: Opener(),
    )
    gateway = ActionGateway(
        AppPaths.from_repo_root(tmp_path),
        DiagnosticRegistry({}, []),
    )

    status, body = gateway._http_get("http://example.test", 10)

    assert (status, body) == (200, b"{}")
    assert seen["size"] == DIAGNOSTIC_MAX_OUTPUT_BYTES + 1
