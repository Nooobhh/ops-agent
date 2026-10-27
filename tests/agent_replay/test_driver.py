import json

import httpx
import pytest

from lib.agent_replay.driver import ReplayDriver
from lib.agent_replay.recordings import Recording

MODEL_CONFIG = """
schema_version: 1
models:
  - id: model-replay
    title: Replay
    provider: openai_compatible
    base_url: http://127.0.0.1:9/v1
    api_key: any
    model: fake-model
    default_reasoning_effort: high
defaults:
  auto: {model_id: model-replay, reasoning_effort: high, timeout_seconds: 30}
  manual: {model_id: model-replay, reasoning_effort: high, timeout_seconds: 30}
  weekly: {model_id: model-replay, reasoning_effort: high, timeout_seconds: 30}
agent_policies:
  qa: {model_id: model-replay, reasoning_effort: high, max_tool_turns: 10, model_timeout_seconds: 30}
"""


def chat(*, tool=None, arguments=None, content=None, call_id="call_1"):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{
            "id": call_id,
            "type": "function",
            "function": {"name": tool, "arguments": json.dumps(arguments or {}, ensure_ascii=False)},
        }]
    return {
        "id": "cmpl",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def qa_case():
    snapshot = Recording(
        "c-snap", "read_resource_snapshot", {}, "alpha", "node1",
        {
            "schema_version": 1, "kind": "resource_polling_snapshot", "available": True,
            "platform": "alpha", "node": "node1", "collected_at": "2026-10-01T00:00:00+08:00",
            "status": "ok", "coverage": {}, "errors": [], "services": [],
            "nodes": [{"node": "node1", "memory_used_percent": 42}],
        },
        "tr-src",
    )
    return {
        "schema_version": 1,
        "case_id": "qa-synthetic",
        "smoke": True,
        "expect": {"task_type": "qa", "conclusion": ["answered"]},
        "create": {
            "kind": "manual", "message": "alpha node1 现在的内存使用率是多少",
            "platform": None, "node": None, "manual_artifacts": True,
            "risk_seed": None, "alert_event": None, "polling_snapshot": None,
        },
        "interaction": {
            "context_reply": None, "followups": [], "control_replies": [],
            "budget_grants": 0, "command_rejects": [],
        },
        "recordings": [snapshot.to_dict()],
        "labels": {
            "source_tasks": ["tr-src"],
            "source": {"platform": "alpha", "node": "node1", "scope": "node"},
            "key_evidence": ["c-snap"],
        },
    }


def agent_replies():
    return [
        chat(tool="read_resource_snapshot", call_id="call_snap"),
        chat(content="node1 内存使用率 42%"),
    ]


def fake_model(replies):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("tools"):
            # 分类器的无工具调用:回一段无效内容,分类按程序预判
            return httpx.Response(200, json=chat(content="无法判断"))
        return httpx.Response(200, json=replies.pop(0))
    return httpx.MockTransport(handler)


@pytest.fixture
def model_config(tmp_path):
    path = tmp_path / "model-replay.yaml"
    path.write_text(MODEL_CONFIG, encoding="utf-8")
    return path


def driver(repo, tmp_path, model_config, name, **kwargs):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    return ReplayDriver(
        qa_case(),
        repo_root=repo,
        out_dir=tmp_path / name,
        fixtures_dir=fixtures,
        model_config=model_config,
        **kwargs,
    )


def test_model_mode_replays_environment_and_records_responses(indexed_replay_repo, tmp_path, model_config):
    result = driver(
        indexed_replay_repo, tmp_path, model_config, "a",
        model_inner_transport=fake_model(agent_replies()),
    ).run()

    metrics = result["metrics"]
    assert metrics["hard_failures"] == []
    assert metrics["task_type"] == "qa"
    assert metrics["status"] == "completed"
    assert metrics["conclusion"] == "answered"
    assert metrics["conclusion_ok"] is True
    assert metrics["evidence"]["key_recall"] == 1.0
    assert metrics["tools"]["by_tool"] == {"read_resource_snapshot": 1}
    assert metrics["usage"]["requests"] == 3
    assert metrics["usage"]["prompt_tokens"] == 300
    assert metrics["usage"]["cached_tokens"] == 120
    assert metrics["usage"]["models"] == ["fake-model"]
    assert result["interactions"] == [{"finish": True}]
    responses = json.loads((tmp_path / "a" / "model-responses.json").read_text(encoding="utf-8"))
    assert (len(responses["worker"]), len(responses["classifier"])) == (2, 1)


def test_recorded_mode_reproduces_skeleton_without_network(indexed_replay_repo, tmp_path, model_config):
    first = driver(
        indexed_replay_repo, tmp_path, model_config, "a",
        model_inner_transport=fake_model(agent_replies()),
    ).run()
    script = json.loads((tmp_path / "a" / "model-responses.json").read_text(encoding="utf-8"))
    script["skeleton"] = first["skeleton"]

    replayed = driver(indexed_replay_repo, tmp_path, model_config, "a0", script=script).run()

    assert replayed["a0"] == {"divergence": None, "script_exhausted": False, "served": 3}
    assert replayed["metrics"]["hard_failures"] == []
    assert replayed["skeleton"] == first["skeleton"]

    script["skeleton"] = first["skeleton"][:-1]
    diverged = driver(indexed_replay_repo, tmp_path, model_config, "a0-bad", script=script).run()
    assert diverged["a0"]["divergence"]["index"] == len(first["skeleton"]) - 1
    assert "a0_divergence" in diverged["metrics"]["hard_failures"]


def test_interaction_policy(replay_repo, tmp_path, model_config):
    replay = driver(replay_repo, tmp_path, model_config, "policy")
    replay.case["interaction"].update({
        "budget_grants": 1, "command_rejects": ["rm -rf /tmp/x"],
        "followups": ["继续"], "control_replies": ["转 InvestigationAgent 深入调查"],
    })

    assert replay._decide({"kind": "command", "command": "rm  -rf /tmp/x"})[0] == "reject"
    assert replay._decide({"kind": "command", "command": "uptime"}) == ("approve", None, "once")
    # 实况对比时驱动器只替人批准只读命令
    assert replay._decide({"kind": "command", "command": "cat /opt/e/mysqld_exporter.cnf"})[0] == "reject"
    assert replay._decide({"kind": "command", "command": "grep x /var/log/m > /tmp/o"})[0] == "reject"
    assert replay._decide({"kind": "budget"})[0] == "approve"
    assert replay._decide({"kind": "budget"})[0] == "reject"
    assert replay._decide({"kind": "report_gate"})[0] == "revise"
    assert replay._decide({"kind": "report_gate"})[0] == "reject"
    assert replay._decide({"kind": "kb_draft"})[0] == "reject"

    handoff = [{"type": "agent_handoff_requested", "payload": {"options": ["补充任务信息"]}}]
    assert replay._reply(handoff) == "转 InvestigationAgent 深入调查"
    assert replay._reply(handoff) == "补充任务信息"
    context = [{"type": "task_context_clarification_requested", "payload": {"options": []}}]
    assert replay._reply(context) == "alpha node1"
    review = [{"type": "task_type_clarification_requested",
               "payload": {"options": ["采用程序判断：investigation", "采用模型判断：qa"]}}]
    assert replay._reply(review) == "采用模型判断：qa"
    answered = [*review, {"type": "user_message", "payload": {}}]
    assert replay._reply(answered) == "继续"
    assert replay._reply(answered) is None
