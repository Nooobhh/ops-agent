from datetime import datetime
from pathlib import Path
import json
import re

from lib.runner import _RUN_ID_RE
from tests.test_patrol_runner import FakePool, VALID_JSON
from scripts.patrol_runner import resolve_run_id, run_patrol_once


def test_run_id_auto_includes_timestamp_and_sanitized_playbook_name():
    run_id = resolve_run_id(
        "auto",
        Path("playbooks/alpha/patrol/patrol_node_health.sh"),
        platform_path=Path("config/platform-alpha.yaml"),
        now=datetime(2026, 7, 16, 8, 30),
    )

    assert run_id == "patrol-20260716-0830-alpha-node_health"
    assert re.fullmatch(r"^patrol-\d{8}-\d{4}-[a-z]+-[a-z_]+$", run_id)
    assert _RUN_ID_RE.fullmatch(run_id)


def test_explicit_invalid_run_id_remains_rejected():
    run_id = "../../etc/foo"

    assert _RUN_ID_RE.fullmatch(run_id) is None


def test_run_patrol_once_writes_platform_to_snapshot(tmp_path):
    result = run_patrol_once(
        pool=FakePool(VALID_JSON),
        nodes=["node12"],
        playbook=Path("tests/data/toy_playbook.sh"),
        records_root=tmp_path,
        run_id="patrol-explicit",
        platform="beta",
    )

    snap = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
    assert snap["platform"] == "beta"
