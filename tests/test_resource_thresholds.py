import json

import pytest

from lib.acquisition.thresholds import (
    ResourceThresholdError,
    load_overrides,
    merge_overrides,
    resource_alert_settings,
    save_overrides,
)


def _config(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("resource-metrics.yaml").write_text(
        """
risk_defaults:
  cpu: {warning_pct: 80, critical_pct: 90}
  memory: {warning_available_pct: 15, critical_available_pct: 8}
business_metrics:
  mysql:
    service: MySQL
    metrics:
      connection_usage_pct:
        label: 连接使用率
        unit: percent
        warning_above: 80
        critical_above: 95
""",
        encoding="utf-8",
    )


def test_manual_overrides_are_merged_without_mutating_defaults(tmp_path):
    config = {
        "risk_defaults": {"cpu": {"warning_pct": 80, "critical_pct": 90}},
    }

    result = merge_overrides(config, {
        "risk_defaults": {"cpu": {"warning_pct": 75}},
    })

    assert result["risk_defaults"]["cpu"] == {"warning_pct": 75, "critical_pct": 90}
    assert config["risk_defaults"]["cpu"]["warning_pct"] == 80


def test_settings_expose_default_override_and_effective_values(tmp_path):
    _config(tmp_path)
    state = tmp_path / "state"
    save_overrides(tmp_path, state, {
        "risk_defaults": {"cpu": {"warning_pct": 75}},
    })

    payload = resource_alert_settings(tmp_path, state)
    cpu = next(item for item in payload["groups"] if item["key"] == "cpu")
    warning = next(item for item in cpu["fields"] if item["key"] == "warning_pct")

    assert warning["default"] == 80
    assert warning["override"] == 75
    assert warning["effective"] == 75
    assert load_overrides(state)["risk_defaults"]["cpu"]["warning_pct"] == 75


def test_business_thresholds_can_be_cleared_and_invalid_values_are_rejected(tmp_path):
    _config(tmp_path)
    state = tmp_path / "state"
    save_overrides(tmp_path, state, {
        "business_metrics": {"mysql": {"metrics": {
            "connection_usage_pct": {"warning_above": 70},
        }}}
    })
    saved = json.loads(state.joinpath("resource-alert-settings.json").read_text())
    assert saved["overrides"]["business_metrics"]["mysql"]["metrics"]["connection_usage_pct"]["warning_above"] == 70

    save_overrides(tmp_path, state, {})
    assert load_overrides(state) == {}

    with pytest.raises(ResourceThresholdError):
        save_overrides(tmp_path, state, {
            "risk_defaults": {"cpu": {"warning_pct": 101}},
        })
