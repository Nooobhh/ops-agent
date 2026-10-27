"""Deterministic retrieval profiles.

Task classification is intentionally not implemented here.  A caller chooses a
profile and this module describes what the profile is allowed to retrieve.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    include_baseline: bool
    include_diagnosis: bool
    include_knowledge: bool
    include_procedures: bool
    include_alert_route: bool
    topology_depth: int
    use_vector: bool
    use_rerank: bool
    max_documents_per_bucket: int = 5
    # 必读里可收的 knowledge 桶(拓扑/基线类)文档数;按相关度与故障、流程同排
    required_knowledge: int = 0
    # 沿服务 depends_on 向上游扩展的跳数:上游故障常表现为本服务异常;0 为不扩展
    dependency_hops: int = 0


POLICIES = {
    "qa": RetrievalPolicy(
        "qa", True, False, True, False, False, 1, False, False, 5,
    ),
    "investigation": RetrievalPolicy(
        "investigation", True, True, True, True, False, 1, True, True, 5,
        required_knowledge=2, dependency_hops=2,
    ),
    "alert": RetrievalPolicy(
        "alert", True, False, True, True, True, 1, False, False, 5,
    ),
    "artifact": RetrievalPolicy(
        "artifact", True, True, True, True, False, 1, True, True, 5, dependency_hops=1,
    ),
    "default": RetrievalPolicy(
        "default", True, False, True, False, False, 1, False, False, 5,
    ),
}


def policy_for(name: str) -> RetrievalPolicy:
    """Return a safe default for unknown or empty profile names."""
    return POLICIES.get(str(name or "").strip().lower(), POLICIES["default"])
