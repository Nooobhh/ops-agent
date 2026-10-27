import json
from pathlib import Path

import pytest

from lib.acquisition.settings import (
    AcquisitionSettingsError,
    load_settings,
    resolve_storage_path,
    save_settings,
)


def test_defaults_are_24x7_five_minutes_and_gzip(tmp_path):
    settings = load_settings(tmp_path / "state")

    assert settings["alerts"]["enabled"] is True
    assert settings["resources"]["enabled"] is True
    assert settings["alerts"]["interval_seconds"] == 300
    assert settings["resources"]["interval_seconds"] == 300
    assert settings["resources"]["compression"] == "gzip"
    assert settings["resources"]["retention_days"] == 90


def test_legacy_poll_marker_is_migrated_until_new_settings_are_saved(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    state.joinpath("poll-enabled.json").write_text(
        json.dumps({"enabled": False}), encoding="utf-8",
    )

    settings = load_settings(state)
    assert settings["alerts"]["enabled"] is False

    settings["resources"]["retention_days"] = 120
    save_settings(state, settings)
    state.joinpath("poll-enabled.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8",
    )

    loaded = load_settings(state)
    assert loaded["alerts"]["enabled"] is False
    assert loaded["resources"]["retention_days"] == 120


def test_settings_validate_ranges_and_reject_filesystem_root(tmp_path):
    settings = load_settings(tmp_path / "state")
    settings["resources"]["interval_seconds"] = 10
    with pytest.raises(AcquisitionSettingsError):
        save_settings(tmp_path / "state", settings)

    with pytest.raises(AcquisitionSettingsError):
        resolve_storage_path(tmp_path, Path(tmp_path.anchor))


def test_resource_risk_notifications_can_be_enabled_after_baseline_is_ready(tmp_path):
    settings = load_settings(tmp_path / "state")
    settings["resources"]["risk_notifications_enabled"] = True

    saved = save_settings(tmp_path / "state", settings)

    assert saved["resources"]["risk_notifications_enabled"] is True
