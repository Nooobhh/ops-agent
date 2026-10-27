"""Read-only knowledge-base routing lookup for alert details."""
from __future__ import annotations

from pathlib import Path

from lib.kb.bases import resolve_knowledge_root
from lib.triage import load_routing, match_alert


def lookup(routing_path: Path, alertname: str, platform: str) -> dict | None:
    path = Path(routing_path)
    if not path.exists():
        return None
    routing = load_routing(path)
    hit = match_alert(routing, alertname, platform)
    if hit is None:
        return None

    title = hit.doc
    for document in routing.get("docs") or []:
        if document.get("path") == hit.doc:
            title = document.get("title") or hit.doc
            break
    # 跳中台内的知识页而不是离线 wiki 快照:开关也从「wiki.html 是否构建过」
    # 改成「文档本身在不在」——知识页直读源文件,不依赖 build/ 产物。
    doc_path = resolve_knowledge_root(path.parent.parent) / hit.doc
    return {
        "doc": hit.doc,
        "doc_title": title,
        "doc_url": f"/knowledge/{hit.doc}" if doc_path.is_file() else None,
        "playbook": hit.playbook,
        "matched_via": "shared" if hit.platform == "shared" else "platform",
    }
