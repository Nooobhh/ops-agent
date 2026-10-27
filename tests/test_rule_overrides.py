import json

import pytest

from lib.alerts.rule_overrides import load_rule_overrides, set_rule_override


def test_partial_updates_roundtrip_without_losing_fields(tmp_path):
    first = set_rule_override(tmp_path, "prometheus:DiskErrors", notify=False)
    assert first["notify"] is False
    assert first["severity"] is None

    second = set_rule_override(tmp_path, "prometheus:DiskErrors", severity="warning")
    assert second["notify"] is False
    assert second["severity"] == "warning"
    assert load_rule_overrides(tmp_path)["prometheus:DiskErrors"] == second

    payload = json.loads((tmp_path / "alert-rule-overrides.json").read_text())
    assert payload == {"version": 1, "overrides": {"prometheus:DiskErrors": second}}


def test_invalid_severity_does_not_modify_file(tmp_path):
    set_rule_override(tmp_path, "prometheus:CpuHigh", notify=False)
    path = tmp_path / "alert-rule-overrides.json"
    before = path.read_bytes()

    with pytest.raises(ValueError):
        set_rule_override(tmp_path, "prometheus:CpuHigh", severity="urgent")

    assert path.read_bytes() == before


def test_clear_existing_and_missing_keys(tmp_path):
    set_rule_override(tmp_path, "demo:alpha:NameNode", severity="critical")
    assert set_rule_override(tmp_path, "demo:alpha:NameNode", clear=True) is None
    assert load_rule_overrides(tmp_path) == {}
    assert set_rule_override(tmp_path, "missing", clear=True) is None


def test_clear_severity_is_atomic_and_preserves_notify(tmp_path):
    set_rule_override(
        tmp_path, "prometheus:DiskFull", notify=False, severity="warning",
    )

    entry = set_rule_override(
        tmp_path, "prometheus:DiskFull", clear_severity=True,
    )

    assert entry["notify"] is False
    assert entry["severity"] is None
    assert load_rule_overrides(tmp_path)["prometheus:DiskFull"] == entry


def test_load_is_fail_soft_for_missing_broken_and_wrong_shapes(tmp_path):
    assert load_rule_overrides(tmp_path) == {}
    path = tmp_path / "alert-rule-overrides.json"
    for content in ("{", "[]", '{"overrides": []}'):
        path.write_text(content, encoding="utf-8")
        assert load_rule_overrides(tmp_path) == {}
