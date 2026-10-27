import json

import pytest

from lib.agent_replay.gateway import (
    MISS_NOTE,
    ReplayActionGateway,
    ReplayKnowledgeSink,
    ReplayLiveAccessError,
)
from lib.agent_replay.recordings import Recording, RecordingPool
from lib.triage_agent.actions import ActionDenied
from lib.triage_agent.diagnostics import DiagnosticDenied, DiagnosticRegistry
from lib.triage_agent.paths import AppPaths

from .conftest import PLAYBOOK


def make_gateway(repo, recordings, snapshot=None):
    paths = AppPaths.from_repo_root(repo)
    pool = RecordingPool(recordings, snapshot_fallback=snapshot)
    gateway = ReplayActionGateway(
        paths, DiagnosticRegistry.load(paths.diagnostics_config), recordings=pool,
    )
    session = gateway.start_session(platform="alpha", node="node1", trigger="replay")
    return gateway, pool, session


def saved(session):
    return json.loads((session.dir / "session.json").read_text(encoding="utf-8"))


def rec(call_id, tool, args, result):
    return Recording(call_id, tool, args, "alpha", "node1", result, "tr-src")


def test_diagnostic_hit_is_recorded_as_session_step(replay_repo):
    gateway, pool, session = make_gateway(replay_repo, [
        rec("c1", "run_diagnostic", {"diagnostic_id": "system.memory_summary", "params": {}},
            {"stdout": "Mem: 64G", "stderr": "", "exit_code": 0}),
    ])

    result = gateway.run_diagnostic(
        session.run_id, platform="alpha", node="node1",
        diagnostic_id="system.memory_summary", params={},
    )

    assert result["stdout"] == "Mem: 64G"
    step = saved(session)["steps"][0]
    assert step["cmd"] == "[node1] free -m"
    assert step["stdout"] == "Mem: 64G"
    assert pool.lookups[-1]["match"] == "exact"


def test_unregistered_diagnostic_is_still_rejected_by_registry(replay_repo):
    gateway, _pool, session = make_gateway(replay_repo, [])

    with pytest.raises(DiagnosticDenied):
        gateway.run_diagnostic(
            session.run_id, platform="alpha", node="node1",
            diagnostic_id="system.not_there", params={},
        )


def test_miss_returns_explicit_failure_without_counting_as_evidence(replay_repo):
    gateway, pool, session = make_gateway(replay_repo, [])

    result = gateway.run_diagnostic(
        session.run_id, platform="alpha", node="node1",
        diagnostic_id="system.memory_summary", params={},
    )

    assert result["exit_code"] == -1
    assert result["stderr"] == MISS_NOTE
    assert saved(session)["steps"] == []
    assert pool.lookups[-1]["match"] == "miss"


def test_free_command_keeps_redline_and_replays_output(replay_repo):
    gateway, _pool, session = make_gateway(replay_repo, [
        rec("c1", "run_command", {"node": "node1", "pipeline": [["uptime"]]},
            {"command": "uptime", "stdout": "load average: 1.0", "stderr": "", "exit_code": 0}),
    ])

    with pytest.raises(ActionDenied, match="红线"):
        gateway.run_command(session.run_id, platform="alpha", node="node1", pipeline=[["rm", "-rf", "/data"]])
    result = gateway.run_command(session.run_id, platform="alpha", node="node1", pipeline=[["uptime"]])

    assert result["stdout"] == "load average: 1.0"
    assert saved(session)["steps"][-1]["cmd"] == "[node1] uptime"


def test_playbook_hit_and_miss(replay_repo):
    gateway, _pool, session = make_gateway(replay_repo, [])

    with pytest.raises(ActionDenied, match="未登记"):
        gateway.run_registered_playbook(
            run_id=session.run_id, platform="alpha", node="node1", playbook="playbooks/other.sh",
        )
    missed = gateway.run_registered_playbook(
        run_id=session.run_id, platform="alpha", node="node1", playbook=PLAYBOOK,
    )
    assert missed["verdict"] == "insufficient"
    assert saved(session)["playbook_results"] == []

    gateway.recordings = RecordingPool([rec(
        "alert-playbook-1", "run_registered_playbook", {"playbook": PLAYBOOK},
        {"verdict": "conclusive", "steps": [{"name": "m", "actual": "ok", "pass": True}]},
    )])
    hit = gateway.run_registered_playbook(
        run_id=session.run_id, platform="alpha", node="node1", playbook=PLAYBOOK,
    )
    assert hit["verdict"] == "conclusive"
    assert saved(session)["playbook_results"][0]["verdict"] == "conclusive"


def test_snapshot_prefers_recording_then_creation_snapshot(replay_repo):
    creation = {"available": True, "platform": "alpha", "node": "node1", "collected_at": "t0"}
    gateway, pool, _session = make_gateway(replay_repo, [], snapshot=creation)

    assert gateway.latest_resource_snapshot("alpha", "node1")["collected_at"] == "t0"
    assert pool.lookups[-1]["match"] == "fallback"

    gateway.recordings = RecordingPool([rec(
        "c1", "read_resource_snapshot", {},
        {"schema_version": 1, "kind": "resource_polling_snapshot", "available": True, "collected_at": "t1"},
    )])
    recorded = gateway.latest_resource_snapshot("alpha", "node1")
    assert recorded["collected_at"] == "t1"
    assert "kind" not in recorded


def test_resource_risk_trims_samples_and_validates_ref(replay_repo):
    ref = "resource_forecast:alpha:node1:metric"
    gateway, _pool, _session = make_gateway(replay_repo, [
        Recording("c1", "read_resource_risk", {"risk_ref": ref}, "alpha", "node1",
                  {"finding": {"key": ref}, "samples": [1, 2, 3]}, "tr-src"),
    ])

    with pytest.raises(ActionDenied):
        gateway.read_resource_risk(risk_ref="../etc", platform="alpha", node="node1")
    result = gateway.read_resource_risk(risk_ref=ref, platform="alpha", node="node1", max_samples=2)

    assert result["samples"] == [2, 3]


def test_any_live_access_is_refused(replay_repo):
    gateway, _pool, _session = make_gateway(replay_repo, [])

    with pytest.raises(ReplayLiveAccessError):
        gateway._remote_session("exec-1").pool("alpha")


def test_knowledge_sink_records_without_writing(replay_repo, monkeypatch):
    paths = AppPaths.from_repo_root(replay_repo)
    sink = ReplayKnowledgeSink(paths)
    target = paths.knowledge_root / "alpha" / "system" / "new.md"

    class Parsed:
        mode = "new"

    monkeypatch.setattr(sink, "_prepare", lambda *_args, **_kwargs: (Parsed(), {}, target, ["证据不全"]))
    result = sink.persist("ts-1", {"mode": "new"})

    assert result.path == target
    assert not target.exists()
    assert sink.attempts == [{
        "run_id": "ts-1", "mode": "new", "target": str(target), "evidence_issues": ["证据不全"],
    }]
