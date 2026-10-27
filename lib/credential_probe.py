"""User-triggered, fixed-command SSH connectivity probe."""
from __future__ import annotations

from pathlib import Path


def probe_ssh_connection(repo_root: Path, platform: str, node: str):
    """Run the non-mutating SSH probe through the canonical SSHPool path."""
    from scripts._common import build_pool_for_platform

    pool = build_pool_for_platform(platform, Path(repo_root))
    try:
        return pool.run(node, "true", timeout=15.0, max_output_bytes=1024)
    finally:
        pool.close()
