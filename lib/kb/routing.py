"""routing 索引编译器:frontmatter → routing.json。

两类入口:
- alerts:按 (platform, alertname) 精确匹配(仅 troubleshoot 类文档参与,
  告警名去首尾空白后 key)
- docs:按 (platform, service) 分组的候选清单,供 LLM 症状选择题消费
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .bases import iter_documents
from .frontmatter import FrontmatterError, load_doc


def build_routing(kb_root: Path) -> Dict[str, Any]:
    kb_root = Path(kb_root).resolve()
    alerts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    docs: list[Dict[str, Any]] = []

    for item in iter_documents(kb_root):
        p = item.path
        try:
            doc = load_doc(p)
        except FrontmatterError:
            continue          # lint 阶段已报,routing 编译容错

        if doc.is_index:
            continue          # index 页无 problem/symptoms,不进候选表

        rel = str(p.relative_to(kb_root)).replace("\\", "/")
        entry = {
            "path": rel,
            "title": doc.title,
            "platform": doc.platform,
            "service": doc.service,
            "problem": doc.problem,
            "symptoms": doc.symptoms,
            "playbook": doc.playbook,
            "playbooks": doc.playbooks,
            "diagnostic_paths": doc.diagnostic_paths,
            "knowledge_layer": doc.knowledge_layer,
            "entrypoints": doc.entrypoints,
            "profile": doc.profile,
            "execution_scope": doc.execution_scope,
            "confidence": doc.confidence,
            "observed_at": doc.observed_at,
            "valid_until": doc.valid_until,
            "status": doc.status,
            "type": doc.type,
            "is_index": doc.is_index,
        }
        docs.append(entry)

        if doc.type == "troubleshoot":
            for a in doc.alerts:
                key = str(a).strip()
                if not key:
                    continue
                platform_alerts = alerts.setdefault(doc.platform, {})
                if key in platform_alerts:
                    # 冲突时保留最先注册;kb-lint 会报错,这里静默
                    continue
                platform_alerts[key] = {
                    "doc": rel,
                    "playbook": doc.playbook,
                    "playbooks": doc.playbooks,
                    "diagnostic_paths": doc.diagnostic_paths,
                    "platform": doc.platform,
                    "service": doc.service,
                }

    return {"alerts": alerts, "docs": docs}
