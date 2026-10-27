import json

import yaml

from lib.agent_replay.recordings import (
    Recording,
    RecordingPool,
    SignatureMap,
    call_key,
    extract_recordings,
)


def recording(call_id, tool, args, *, node="node1", platform="alpha", result=None):
    return Recording(
        call_id=call_id,
        tool=tool,
        args=args,
        platform=platform,
        node=node,
        result=result or {"stdout": call_id, "exit_code": 0},
        source_task="tr-src",
    )


def test_command_key_is_same_for_pipeline_and_rendered_string():
    from_pipeline = call_key(
        "run_command", {"pipeline": [["uptime"], ["head", "-n", "5"]]},
        platform="alpha", node="node1",
    )
    from_string = call_key(
        "run_command", {"command": "uptime  |  head -n 5"},
        platform="alpha", node="node1",
    )
    assert from_pipeline.exact == from_string.exact


def test_pool_matches_exact_then_fuzzy_numeric_then_miss():
    pool = RecordingPool([
        recording("c-200", "run_diagnostic", {"diagnostic_id": "logs.tail", "params": {"lines": 200, "log": "a"}}),
        recording("c-50", "run_diagnostic", {"diagnostic_id": "logs.tail", "params": {"lines": 50, "log": "a"}}),
    ])

    hit, kind = pool.match("run_diagnostic", {"diagnostic_id": "logs.tail", "params": {"log": "a", "lines": 200}},
                           platform="alpha", node="node1")
    assert (hit.call_id, kind) == ("c-200", "exact")

    near, kind = pool.match("run_diagnostic", {"diagnostic_id": "logs.tail", "params": {"log": "a", "lines": 60}},
                            platform="alpha", node="node1")
    assert (near.call_id, kind) == ("c-50", "fuzzy")

    other_log, kind = pool.match("run_diagnostic", {"diagnostic_id": "logs.tail", "params": {"log": "b", "lines": 50}},
                                 platform="alpha", node="node1")
    assert other_log is None and kind == "miss"
    assert [item["match"] for item in pool.lookups] == ["exact", "fuzzy", "miss"]


def test_pool_never_serves_another_nodes_data():
    pool = RecordingPool([recording("c1", "run_command", {"command": "uptime"}, node="node1")])

    hit, kind = pool.match("run_command", {"command": "uptime"}, platform="alpha", node="node2")

    assert hit is None and kind == "miss"


def test_first_recording_wins_for_duplicate_signatures():
    pool = RecordingPool([
        recording("primary", "run_command", {"command": "uptime"}),
        recording("extra", "run_command", {"command": "uptime"}),
    ])

    hit, _ = pool.match("run_command", {"command": "uptime"}, platform="alpha", node="node1")

    assert hit.call_id == "primary"


def test_signature_map_translates_renamed_plugin_tool(tmp_path):
    path = tmp_path / "map.yaml"
    path.write_text(yaml.safe_dump({
        "tool_aliases": {"prometheus.query_named": "run_diagnostic"},
        "diagnostic_aliases": {"node_load": "prom.node_load"},
        "arg_renames": {"prometheus.query_named": {"query": "diagnostic_id", "args": "params"}},
    }), encoding="utf-8")
    pool = RecordingPool(
        [recording("c1", "run_diagnostic", {"diagnostic_id": "prom.node_load", "params": {}})],
        signature_map=SignatureMap.load(path),
    )

    hit, kind = pool.match(
        "prometheus.query_named", {"query": "node_load", "args": {}}, platform="alpha", node="node1",
    )

    assert (hit.call_id, kind) == ("c1", "exact")


def write_task(task_dir, task, events, invocations):
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (task_dir / "messages.jsonl").write_text(
        "".join(json.dumps({"type": kind, "payload": payload}) + "\n" for kind, payload in events),
        encoding="utf-8",
    )
    (task_dir / "invocations.json").write_text(json.dumps(invocations), encoding="utf-8")


def test_extract_keeps_only_completed_environment_observations(tmp_path):
    task_dir = tmp_path / "tr-1"
    write_task(
        task_dir,
        {"task_id": "tr-1", "platform": "alpha", "node": "node1"},
        [
            ("tool_call", {"tool_call_id": "c-diag", "name": "run_diagnostic",
                           "arguments": {"diagnostic_id": "system.memory_summary", "params": {}}}),
            ("tool_call", {"tool_call_id": "c-failed", "name": "run_diagnostic",
                           "arguments": {"diagnostic_id": "nope", "params": {}}}),
            ("tool_call", {"tool_call_id": "c-approval", "name": "run_command",
                           "arguments": {"node": "node2", "pipeline": [["uptime"]]}}),
            ("tool_call", {"tool_call_id": "c-kb", "name": "read_kb_document", "arguments": {"doc": "x.md"}}),
            ("agent_playbook_result", {"invocation_id": "alert-playbook-1",
                                       "playbook": "playbooks/alpha/x.sh", "result": {}}),
        ],
        {
            "c-diag": {"status": "done", "result": {"stdout": "Mem: 1", "exit_code": 0}},
            "c-failed": {"status": "failed", "result": {"error": "未登记", "failed": True}},
            # 旧版命令审批放行便条不是观测结果
            "c-approval": {"status": "done", "result": {"approved": True, "command": "uptime", "note": "重发"}},
            "c-kb": {"status": "done", "result": {"content": "doc"}},
            "alert-playbook-1": {"status": "done", "result": {
                "verdict": "conclusive", "steps": [], "target": {"platform": "alpha", "node": "node3"},
            }},
        },
    )

    recordings = extract_recordings(task_dir)

    assert [(item.call_id, item.tool, item.node) for item in recordings] == [
        ("c-diag", "run_diagnostic", "node1"),
        ("alert-playbook-1", "run_registered_playbook", "node3"),
    ]
    assert recordings[1].args == {"playbook": "playbooks/alpha/x.sh"}
