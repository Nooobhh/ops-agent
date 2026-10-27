from pathlib import Path

from lib.alerts.model import AlertEvent
from lib.alerts.profiles import classify, load_profiles


def event(alertname: str, *, platform: str = "beta", service: str = "hdfs",
          severity: str = "warning", state: str = "firing") -> AlertEvent:
    return AlertEvent(
        source="demo",
        platform=platform,
        alertname=alertname,
        severity=severity,
        host="node1",
        host_raw="node1",
        service=service,
        state=state,
        detail="",
    )


def test_match_name_profile_has_priority_over_service_env_rule(tmp_path):
    profile_path = tmp_path / "profiles.yaml"
    profile_path.write_text(
        """
version: 1
default_class: watch
class_config: {watch: {}, chronic: {}, flapping: {}, ignore: {}}
profiles:
  - {match_name: exact_alert, class: ignore, reason: exact wins}
  - {match_service: hdfs, match_env: beta, class: chronic, reason: service rule}
""",
        encoding="utf-8",
    )

    cls, reason = classify(
        event("exact_alert"),
        active_host_count=1,
        prior_severity=None,
        profiles=load_profiles(profile_path),
    )

    assert (cls, reason) == ("ignore", "exact wins")


def test_match_env_limited_profile_does_not_apply_to_other_platform():
    profiles = load_profiles(Path("envs/example/config/alert-profiles.yaml"))

    cls, _ = classify(
        event("datanode_storage", platform="alpha"),
        active_host_count=8,
        prior_severity=None,
        profiles=profiles,
    )

    assert cls == "watch"


def test_chronic_escalates_on_host_delta_and_warning_to_critical():
    profiles = load_profiles(Path("envs/example/config/alert-profiles.yaml"))

    assert classify(
        event("datanode_storage"),
        active_host_count=4,
        prior_severity=None,
        profiles=profiles,
    )[0] == "watch"
    assert classify(
        event("datanode_storage"),
        active_host_count=3,
        prior_severity=None,
        profiles=profiles,
    )[0] == "chronic"
    cls, reason = classify(
        event("datanode_storage", severity="critical"),
        active_host_count=2,
        prior_severity="warning",
        profiles=profiles,
    )
    assert cls == "watch"
    assert reason.startswith("escalated:")


def test_default_class_is_used_when_no_profile_matches():
    profiles = load_profiles(Path("envs/example/config/alert-profiles.yaml"))

    cls, reason = classify(
        event("brand_new_alert", platform="alpha", service="spark"),
        active_host_count=1,
        prior_severity=None,
        profiles=profiles,
    )

    assert (cls, reason) == ("watch", "default")
