import json

import yaml

from scripts import eval_agent_replay

from .test_cases import source_repo  # noqa: F401 — 复用合成历史任务
from .test_metrics import BASELINE, run


def test_build_cases_cli_emits_json_and_writes_cases(source_repo, tmp_path, capsys):  # noqa: F811
    out = tmp_path / "out"
    out.mkdir()
    (out / "manifest.yaml").write_text(yaml.safe_dump({
        "knowledge_commit": "abc123",
        "cases": [{"id": "qa-node1-mem", "source": "tr-main", "smoke": True}],
    }), encoding="utf-8")

    rc = eval_agent_replay.main(["--out", str(out), "build-cases", "--source-repo", str(source_repo)])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert payload["cases"] == [{"case_id": "qa-node1-mem", "recordings": 2, "key_evidence": 2, "smoke": True}]
    assert (out / "cases" / "qa-node1-mem.json").is_file()
    assert json.loads((out / "fixtures" / "meta.json").read_text())["knowledge_commit"] == "abc123"


def test_compare_cli_and_error_output(tmp_path, capsys):
    def report(runs):
        return {"cases": [{"case_id": case_id, "runs": items} for case_id, items in runs.items()]}

    baseline = tmp_path / "base.json"
    candidate = tmp_path / "cand.json"
    baseline.write_text(json.dumps(report(BASELINE)), encoding="utf-8")
    candidate.write_text(json.dumps(report({case_id: [run(case_id)] for case_id in BASELINE})), encoding="utf-8")

    assert eval_agent_replay.main(["compare", str(baseline), str(candidate)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"

    assert eval_agent_replay.main(["compare", str(tmp_path / "missing.json"), str(candidate)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
