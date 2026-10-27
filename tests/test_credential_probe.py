from dataclasses import dataclass

from lib.credential_probe import probe_ssh_connection


@dataclass
class Result:
    exit_code: int = 0
    timed_out: bool = False


class FakePool:
    def __init__(self):
        self.calls = []
        self.closed = False

    def run(self, node, command, **kwargs):
        self.calls.append((node, command, kwargs))
        return Result()

    def close(self):
        self.closed = True


def test_probe_uses_fixed_readonly_command_and_closes_pool(tmp_path, monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(
        "scripts._common.build_pool_for_platform",
        lambda platform, repo_root: pool,
    )

    result = probe_ssh_connection(tmp_path, "alpha", "node1")

    assert result.exit_code == 0
    assert pool.calls == [("node1", "true", {"timeout": 15.0, "max_output_bytes": 1024})]
    assert pool.closed is True
