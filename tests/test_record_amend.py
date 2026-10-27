import json

import pytest

from lib.record import AmendmentRejected, start_session


def finalized(tmp_path):
    session = start_session(
        tmp_path, run_id="amend-1", node="node1", trigger="manual",
        platform="alpha", service="system",
    )
    session.record_playbook_result({"steps": [{"actual": "old evidence"}]})
    session.finalize(
        verdict="conclusive", severity="P1", root_cause="old cause",
        evidence=["old evidence"], remediation=["observe"],
    )
    return session


def test_amend_adds_evidence_without_losing_old_evidence(tmp_path):
    session = finalized(tmp_path)
    session.record_deep_dive_step("cat /tmp/new", 0, "new evidence")

    session.amend(add_evidence=["new evidence"])

    data = json.loads((session.dir / "session.json").read_text())
    assert data["evidence"] == ["old evidence", "new evidence"]
    assert data["amend_count"] == 2
    assert "第 2 次收口" in (session.dir / "report.md").read_text()


def test_amend_supersedes_old_evidence_and_renders_correction_section(tmp_path):
    session = finalized(tmp_path)

    session.amend(
        supersede=[{"evidence": "old   evidence", "reason": "采证后校正"}],
    )

    data = json.loads((session.dir / "session.json").read_text())
    assert data["evidence"] == []
    assert data["superseded_evidence"][0]["evidence"] == "old evidence"
    assert "已修正" in (session.dir / "report.md").read_text()


def test_amend_partial_root_cause_keeps_other_fields(tmp_path):
    session = finalized(tmp_path)

    session.amend(root_cause="new cause")

    data = json.loads((session.dir / "session.json").read_text())
    assert data["root_cause"] == "new cause"
    assert data["verdict"] == "conclusive"
    assert data["severity"] == "P1"
    assert data["evidence"] == ["old evidence"]


def test_amend_rejects_unknown_superseded_evidence_as_business_error(tmp_path):
    session = finalized(tmp_path)

    with pytest.raises(AmendmentRejected):
        session.amend(supersede=[{"evidence": "missing", "reason": "wrong"}])


def test_amend_rejects_evidence_not_present_in_collection(tmp_path):
    session = finalized(tmp_path)

    with pytest.raises(AmendmentRejected):
        session.amend(add_evidence=["not collected"])


def test_amend_rejects_session_metadata_as_evidence(tmp_path):
    session = finalized(tmp_path)

    with pytest.raises(AmendmentRejected):
        session.amend(add_evidence=["node1"])
