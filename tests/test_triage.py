import json
from pathlib import Path

import pytest

from lib.triage import (
    TriageHit,
    TriageCandidate,
    load_routing,
    match_alert,
    filter_candidates,
)


ROUTING = {
    "alerts": {
        "alpha": {
            "集群监控-内存告警": {
                "doc": "alpha/system/alpha-system-memory-pressure.md",
                "playbook": "playbooks/alpha/system/check_memory_pressure.sh",
                "platform": "alpha",
                "service": "system",
            },
            "YARN-vcore-critical": {
                "doc": "alpha/yarn/alpha-yarn-vcore-exhaustion.md",
                "playbook": None,
                "platform": "alpha",
                "service": "yarn",
            },
        },
        "shared": {
            "SharedAlert": {
                "doc": "shared/system/shared-system-alert.md",
                "playbook": None,
                "platform": "shared",
                "service": "system",
            },
        },
    },
    "docs": [
        {"path": "alpha/system/alpha-system-memory-pressure.md",
         "title": "alpha 节点内存压力", "type": "troubleshoot",
         "platform": "alpha", "service": "system", "problem": "memory-pressure",
         "symptoms": ["内存告警", "swap 高"],
         "playbook": "playbooks/alpha/system/check_memory_pressure.sh",
         "status": "draft", "is_index": False},
        {"path": "alpha/yarn/alpha-yarn-vcore-exhaustion.md",
         "title": "alpha YARN vcore 拥塞", "type": "troubleshoot",
         "platform": "alpha", "service": "yarn", "problem": "vcore-exhaustion",
         "symptoms": ["vcore 打满", "PendingContainers 高"],
         "playbook": None,
         "status": "draft", "is_index": False},
        {"path": "alpha/system/_index.md", "title": "alpha/system 索引",
         "platform": "alpha", "service": "system",
         "type": None, "problem": None, "symptoms": [], "playbook": None,
         "status": None, "is_index": True},
    ],
}


def test_load_routing(tmp_path):
    p = tmp_path / "routing.json"
    p.write_text(json.dumps(ROUTING), encoding="utf-8")
    r = load_routing(p)
    assert "集群监控-内存告警" in r["alerts"]["alpha"]
    assert len(r["docs"]) == 3


def test_match_alert_exact_hit():
    h = match_alert(ROUTING, "集群监控-内存告警", "alpha")
    assert h is not None
    assert h.doc == "alpha/system/alpha-system-memory-pressure.md"
    assert h.playbook == "playbooks/alpha/system/check_memory_pressure.sh"
    assert h.platform == "alpha" and h.service == "system"


def test_match_alert_normalizes_whitespace():
    """告警名首尾空白应被规范化(与 routing 编译时一致)。"""
    h = match_alert(ROUTING, "  集群监控-内存告警  ", "alpha")
    assert h is not None and h.doc.endswith("alpha-system-memory-pressure.md")


def test_match_alert_case_sensitive():
    """告警名大小写敏感(下游 alertmanager 就是精确字符串)。"""
    assert match_alert(ROUTING, "YARN-VCORE-CRITICAL", "alpha") is None
    assert match_alert(ROUTING, "YARN-vcore-critical", "alpha") is not None


def test_match_alert_miss():
    assert match_alert(ROUTING, "不存在的告警", "alpha") is None


def test_match_alert_does_not_cross_platform():
    assert match_alert(ROUTING, "集群监控-内存告警", "beta") is None


def test_match_alert_shared_fallback():
    h = match_alert(ROUTING, "SharedAlert", "beta")
    assert h is not None and h.platform == "shared"


def test_match_alert_platform_route_precedes_shared():
    routing = json.loads(json.dumps(ROUTING))
    routing["alerts"]["shared"]["集群监控-内存告警"] = {
        "doc": "shared/system/shared-memory.md",
        "playbook": None,
        "platform": "shared",
        "service": "system",
    }
    h = match_alert(routing, "集群监控-内存告警", "alpha")
    assert h is not None and h.platform == "alpha"


def test_filter_candidates_platform_service():
    """按 (platform, service) 过滤,index 与他类跳过。"""
    cs = filter_candidates(ROUTING, platform="alpha", service="system")
    assert len(cs) == 1
    assert isinstance(cs[0], TriageCandidate)
    assert cs[0].problem == "memory-pressure"
    assert cs[0].playbook == "playbooks/alpha/system/check_memory_pressure.sh"


def test_filter_candidates_platform_only():
    """无 service 参数时按 platform 全量返回。"""
    cs = filter_candidates(ROUTING, platform="alpha")
    assert len(cs) == 2      # system + yarn(index 跳过)


def test_filter_candidates_no_service_match():
    """service 无匹配返回空。"""
    assert filter_candidates(ROUTING, platform="alpha", service="nonexistent") == []


def test_filter_candidates_skips_index_and_no_type():
    """is_index=True 与 type 为空的条目一律跳过。"""
    cs = filter_candidates(ROUTING, platform="alpha", service="system")
    assert all(not c.path.endswith("_index.md") for c in cs)
