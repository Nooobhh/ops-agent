"""Alert profile classification."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .model import AlertEvent

ALERT_CLASSES = {"watch", "chronic", "ignore", "flapping"}


@dataclass(frozen=True)
class ProfilesConfig:
    version: int
    default_class: str
    class_config: Dict[str, Dict[str, Any]]
    profiles: List[Dict[str, Any]]


def load_profiles(path) -> ProfilesConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ProfilesConfig(
        version=int(data.get("version", 1)),
        default_class=str(data.get("default_class", "watch")),
        class_config=dict(data.get("class_config") or {}),
        profiles=[dict(item) for item in (data.get("profiles") or [])],
    )


def classify(event: AlertEvent, *, active_host_count: int,
             prior_severity: Optional[str],
             profiles: ProfilesConfig) -> Tuple[str, str]:
    profile = match_profile(event, profiles)
    alert_class = _profile_class(profile, profiles)
    reason = str(profile.get("reason") if profile and profile.get("reason") else "default")

    if alert_class == "chronic" and _should_escalate(
        profile or {}, event, active_host_count, prior_severity, profiles
    ):
        return "watch", f"escalated:{reason}"
    return alert_class, reason


def baseline_class(event: AlertEvent, profiles: ProfilesConfig) -> str:
    """Return static profile classification without runtime escalation."""
    return _profile_class(match_profile(event, profiles), profiles)


def match_profile(event: AlertEvent, profiles: ProfilesConfig) -> Optional[Dict[str, Any]]:
    for profile in profiles.profiles:
        if profile.get("match_name") == event.alertname \
                and _env_matches(profile, event) \
                and _state_matches(profile, event):
            return profile
    for profile in profiles.profiles:
        if "match_name" in profile:
            continue
        if profile.get("match_service") != event.service:
            continue
        if not _env_matches(profile, event) or not _state_matches(profile, event):
            continue
        return profile
    return None


def _profile_class(profile: Optional[Dict[str, Any]], profiles: ProfilesConfig) -> str:
    alert_class = str(profile.get("class") if profile else profiles.default_class)
    return alert_class if alert_class in ALERT_CLASSES else profiles.default_class


def _env_matches(profile: Dict[str, Any], event: AlertEvent) -> bool:
    return "match_env" not in profile or profile.get("match_env") == event.platform


def _state_matches(profile: Dict[str, Any], event: AlertEvent) -> bool:
    return "match_state" not in profile or profile.get("match_state") == event.state


def _should_escalate(profile: Dict[str, Any], event: AlertEvent,
                     active_host_count: int, prior_severity: Optional[str],
                     profiles: ProfilesConfig) -> bool:
    chronic_config = profiles.class_config.get("chronic", {})
    baseline = int(profile.get("baseline_host_count", 0) or 0)
    delta = int(profile.get("escalate_host_delta",
                            chronic_config.get("escalate_host_delta", 0)) or 0)
    if baseline and delta and active_host_count - baseline >= delta:
        return True
    return prior_severity == "warning" and event.severity == "critical"
