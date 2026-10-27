import json
from datetime import datetime, timezone

from lib.alerts.poll_control import load_status, set_enabled


def test_enabled_marker_remains_effective_across_reboot(tmp_path):
    marker = tmp_path / "poll-enabled.json"
    marker.write_text(json.dumps({
        "enabled": True,
        "updated_at": "2026-07-20T01:00:00+00:00",
        "updated_by": "web",
    }), encoding="utf-8")
    boot = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)

    stale = load_status(marker, boot_time=boot)
    fresh = set_enabled(
        marker, True,
        now=datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
        boot_time=boot,
    )

    assert stale["enabled"] is True
    assert stale["effective"] is True
    assert stale["since_boot"] is True
    assert stale["reason"] is None
    assert fresh["enabled"] is True
    assert fresh["effective"] is True
    assert fresh["since_boot"] is True


def test_unknown_boot_time_no_longer_stops_24x7_acquisition(tmp_path):
    marker = tmp_path / "poll-enabled.json"
    marker.write_text(json.dumps({
        "enabled": True,
        "updated_at": "2026-07-20T03:00:00+00:00",
        "updated_by": "web",
    }), encoding="utf-8")

    status = load_status(marker, boot_time=None)

    assert status["effective"] is True
    assert status["since_boot"] is True
    assert status["reason"] is None
