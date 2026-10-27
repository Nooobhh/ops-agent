from datetime import datetime, timezone

from lib.acquisition.resource_notifications import sync_resource_risk_notifications


NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self):
        self.notifiers = [object()]
        self.blocks = []

    def dispatch(self, blocks):
        self.blocks.extend(blocks)


def finding(key="resource_forecast:alpha:cpu:node1"):
    return {
        "key": key,
        "severity": "critical",
        "platform": "alpha",
        "node": "node1",
        "resource": "cpu",
        "title": "CPU 持续高负载",
        "description": "CPU P95 为 95.0%。",
        "first_observed_at": NOW.isoformat(),
        "last_updated_at": NOW.isoformat(),
        "duration_seconds": 0,
        "evidence": {"p95": 95, "samples": 6},
    }


def test_continuous_risk_is_notified_once_and_reappearance_notifies_again(tmp_path):
    notifier = FakeNotifier()
    risks = {"findings": [finding()]}

    first = sync_resource_risk_notifications(
        risks, tmp_path, enabled=True, notifier=notifier, now=NOW,
    )
    second = sync_resource_risk_notifications(
        risks, tmp_path, enabled=True, notifier=notifier,
        now=NOW.replace(minute=5),
    )
    recovered = sync_resource_risk_notifications(
        {"findings": []}, tmp_path, enabled=True, notifier=notifier,
        now=NOW.replace(minute=10),
    )
    returned = sync_resource_risk_notifications(
        risks, tmp_path, enabled=True, notifier=notifier,
        now=NOW.replace(minute=15),
    )

    assert first["notified"] == 1
    assert second["notified"] == 0
    assert recovered["resolved"] == 1
    assert returned["notified"] == 1
    assert len(notifier.blocks) == 2


def test_disabled_notification_tracks_recovery_without_sending(tmp_path):
    notifier = FakeNotifier()
    risks = {"findings": [finding()]}

    first = sync_resource_risk_notifications(
        risks, tmp_path, enabled=False, notifier=notifier, now=NOW,
    )
    sync_resource_risk_notifications(
        {"findings": []}, tmp_path, enabled=False, notifier=notifier,
        now=NOW.replace(minute=5),
    )
    returned = sync_resource_risk_notifications(
        risks, tmp_path, enabled=True, notifier=notifier,
        now=NOW.replace(minute=10),
    )

    assert first["notified"] == 0
    assert returned["notified"] == 1


def test_severity_change_does_not_re_notify_without_recovery(tmp_path):
    notifier = FakeNotifier()
    warning = {**finding(), "severity": "warning"}
    critical = {**finding(), "severity": "critical"}

    first = sync_resource_risk_notifications(
        {"findings": [warning]}, tmp_path, enabled=True, notifier=notifier, now=NOW,
    )
    escalated = sync_resource_risk_notifications(
        {"findings": [critical]}, tmp_path, enabled=True, notifier=notifier,
        now=NOW.replace(minute=5),
    )
    repeated = sync_resource_risk_notifications(
        {"findings": [critical]}, tmp_path, enabled=True, notifier=notifier,
        now=NOW.replace(minute=10),
    )

    assert first["notified"] == 1
    assert escalated["notified"] == 0
    assert repeated["notified"] == 0
    assert len(notifier.blocks) == 1
