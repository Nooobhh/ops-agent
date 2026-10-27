import json
from pathlib import Path

from lib.agent_replay.lean_replay import replay_case

REPO = Path(__file__).resolve().parents[2]


def _transcript(tmp_path: Path, records) -> Path:
    path = tmp_path / "case-x" / "lean" / "transcript.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")
    return path


def test_replay_runs_recorded_session_through_new_runtime(tmp_path):
    shell = {"name": "shell", "arguments": {"platform": "alpha", "nodes": ["node1"], "command": "free -h"}}
    denied = {"name": "shell", "arguments": {"platform": "alpha", "nodes": ["node1"], "command": "sudo ls"}}
    finish = {"name": "finish", "arguments": {
        "answer": "内存够用", "evidence": ["available 60G", "转述的证据不在输出里"], "confidence": "high"}}
    path = _transcript(tmp_path, [
        {"type": "question", "question": "node1 内存够用吗?"},
        {"type": "assistant", "content": None, "tool_calls": [shell]},
        {"type": "tool_result", "name": "shell",
         "result": {"results": [{"node": "node1", "exit_code": 0, "stdout": "Mem: available 60G\n", "stderr": ""}]}},
        {"type": "assistant", "content": None, "tool_calls": [denied]},
        {"type": "tool_result", "name": "shell", "result": {"denied": True}},
        {"type": "assistant", "content": "结论", "tool_calls": [finish]},
        {"type": "tool_result", "name": "finish", "result": {"ok": True}},
    ])
    result = replay_case(path, tmp_path / "work", repo_root=REPO,
                         recall_index=tmp_path / "missing.sqlite", knowledge_root=tmp_path)

    assert result["status"] == "waiting_input"
    assert result["script_left"] == 0
    assert result["shell_errors"] == 1  # sudo 被命令闸拒
    assert result["facts"] == {"extracted": 1, "not_found": 1}
    assert result["verdict"] == "conclusive"
    assert result["approvals"] == {}
