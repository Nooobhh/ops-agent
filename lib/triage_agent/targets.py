"""Registered remote targets used by the triage task flow.

Targets are the clusters declared by the active environment pack (keyed by
their legacy platform key).  ``managed`` clusters additionally provide
platform diagnostics; every target is a valid read-only SSH target.  Task
validation, node canonicalisation and SSHPool construction all read the same
resource inventory.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from lib.config import PlatformConfig
from lib.envpack import load_env_pack
from lib.inventory import load_inventory
from lib.inventory.sources.legacy import to_platform_config


def target_keys(repo_root: Path) -> tuple[str, ...]:
    return load_env_pack(repo_root).cluster_keys()


def is_registered_target(repo_root: Path, target: str) -> bool:
    return target in target_keys(repo_root)


def target_config_path(repo_root: Path, target: str) -> Path:
    path = load_env_pack(repo_root).source_path(target, "hosts")
    if path is None:
        raise ValueError(f"unsupported triage target: {target}")
    return path


def load_target(repo_root: Path, target: str) -> PlatformConfig:
    if not is_registered_target(repo_root, target):
        raise ValueError(f"unsupported triage target: {target}")
    return to_platform_config(load_inventory(repo_root), target)


def target_nodes(repo_root: Path, target: str) -> list[str]:
    return sorted(load_target(repo_root, target).nodes)


def resolve_node(repo_root: Path, target: str, value: str) -> Optional[str]:
    """Return the registered node key for a key, host IP, alias or FQDN (port ignored)."""
    if not str(value or "").strip() or not is_registered_target(repo_root, target):
        return None
    host = load_inventory(repo_root).resolve_host(value, target)
    return None if host is None else str(host.attrs["key"])


def infer_target_for_node(repo_root: Path, value: str) -> Optional[str]:
    """Infer a target only when the host identity maps unambiguously."""
    if not str(value or "").strip():
        return None
    inventory = load_inventory(repo_root)
    host = inventory.resolve_host(value)
    return None if host is None else inventory.legacy(host.ref)[0]
