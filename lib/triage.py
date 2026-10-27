"""三元组匹配 + routing 索引消费。

skills 层不直接读 routing.json,通过 match_alert / filter_candidates 消费。
- match_alert(routing, alertname, platform) → 平台精确匹配,shared 兜底
- filter_candidates(routing, platform[, service]) → 候选 List,供 LLM 选择题
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class TriageHit:
    """精确告警名命中的路由结果。"""
    doc: str
    playbook: Optional[str]
    platform: str
    service: str
    playbooks: List[dict] = field(default_factory=list)
    diagnostic_paths: List[dict] = field(default_factory=list)


@dataclass
class TriageCandidate:
    """LLM 选择题的一个候选文档。"""
    path: str
    title: str
    platform: str
    service: str
    problem: Optional[str]
    symptoms: List[str]
    playbook: Optional[str]
    status: Optional[str]
    playbooks: List[dict] = field(default_factory=list)
    diagnostic_paths: List[dict] = field(default_factory=list)
    knowledge_layer: Optional[str] = None
    profile: Optional[str] = None


def load_routing(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def match_alert(routing: Dict[str, Any], alertname: str,
                platform: str) -> Optional[TriageHit]:
    """按 (platform, alertname) 匹配,本平台优先,shared 兜底。"""
    key = str(alertname).strip()
    platform_key = str(platform).strip()
    alerts = routing.get("alerts") or {}
    entry = (alerts.get(platform_key) or {}).get(key)
    if entry is None and platform_key != "shared":
        entry = (alerts.get("shared") or {}).get(key)
    if not entry:
        return None
    return TriageHit(
        doc=entry["doc"], playbook=entry.get("playbook"),
        platform=entry["platform"], service=entry["service"],
        playbooks=list(entry.get("playbooks") or []),
        diagnostic_paths=list(entry.get("diagnostic_paths") or []),
    )


def iter_alert_entries(routing: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """遍历按平台分区的 alerts 索引中的全部路由条目。"""
    for platform_alerts in (routing.get("alerts") or {}).values():
        yield from platform_alerts.values()


def filter_candidates(routing: Dict[str, Any], platform: str,
                      service: Optional[str] = None) -> List[TriageCandidate]:
    """按 (platform[, service]) 从 routing.docs 过滤出候选文档,跳 index 与无 type 条目。"""
    out: List[TriageCandidate] = []
    for d in routing.get("docs", []):
        if d.get("is_index") or not d.get("type"):
            continue
        if d.get("platform") != platform:
            continue
        if service is not None and d.get("service") != service:
            continue
        out.append(TriageCandidate(
            path=d["path"], title=d.get("title") or d["path"],
            platform=d["platform"], service=d.get("service") or "",
            problem=d.get("problem"), symptoms=list(d.get("symptoms") or []),
            playbook=d.get("playbook"), status=d.get("status"),
            playbooks=list(d.get("playbooks") or []),
            diagnostic_paths=list(d.get("diagnostic_paths") or []),
            knowledge_layer=d.get("knowledge_layer"), profile=d.get("profile"),
        ))
    return out
