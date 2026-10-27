"""Read-only knowledge-base access for the web console.

正文与服务树直读 knowledge/ 源文件——中台是常驻进程,读 build/ 的构建快照必然过期。
只有全文检索走 build/kb-recall.sqlite,故索引可能比源文件旧;index_state 把这个
落差显形,不静默糊弄。本模块零写路径。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from lib.data_root import resolve_data_root
from lib.datastore import open_datastore
from lib.envpack import active_env_pack, env_timezone
from lib.kb.categories import doc_category, sop_kind_of
from lib.kb.bases import (
    discover_bases, doc_id_aliases, resolve_doc_id, resolve_knowledge_root, split_doc_path,
)
from lib.kb.browse import (
    BROWSE_LIMIT_DEFAULT,
    browse_search,
    build_tree,
    display_title,
    extract_h1,
    resolve_wikilinks,
    strip_frontmatter,
)
from lib.kb.frontmatter import FrontmatterError, load_doc
from lib.kb.index_sync import platform_label
from lib.kb.promotion import evaluate_auto_promotion
from lib.triage import load_routing

REBUILD_HINT = "请运行 .venv/bin/python scripts/build_wiki.py 重建知识库索引"


class DocumentNotFound(LookupError):
    """请求的文档不在 knowledge/ 下,或路径越界。"""


class TopologyNotFound(LookupError):
    """拓扑派生物不存在、损坏或版本不兼容。"""


def _kb_root(repo_root: Path) -> Path:
    return resolve_knowledge_root(Path(repo_root))


def _records_root(repo_root: Path) -> Path:
    return open_datastore(Path(repo_root)).troubleshoot.root


def _index_path(repo_root: Path) -> Path:
    return (resolve_data_root(Path(repo_root)) / "build" / "kb-recall.sqlite").resolve()


def _topology_path(repo_root: Path) -> Path:
    return (resolve_data_root(Path(repo_root)) / "build" / "kb-topology.json").resolve()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, env_timezone()).isoformat()


def _latest_source_mtime(kb_root: Path) -> Optional[float]:
    mtimes = [path.stat().st_mtime for path in kb_root.rglob("*.md")]
    return max(mtimes) if mtimes else None


def index_state(repo_root: Path) -> dict:
    """检索索引相对源文件是否过期。available=False 时前端禁用搜索框而非空转。"""
    index_file = _index_path(repo_root)
    kb_root = _kb_root(repo_root)
    if not index_file.is_file():
        return {"available": False, "built_at": None, "source_latest_at": None,
                "stale": True, "hint": REBUILD_HINT}
    built = index_file.stat().st_mtime
    latest = _latest_source_mtime(kb_root)
    stale = latest is not None and latest > built
    return {
        "available": True,
        "built_at": _iso(built),
        "source_latest_at": _iso(latest) if latest is not None else None,
        "stale": stale,
        "hint": REBUILD_HINT if stale else None,
    }


def _alerts_by_doc(repo_root: Path) -> dict[str, tuple[str, ...]]:
    """routing.json 的 alerts 段反查:doc → 命中它的 alertname(对齐 wiki.py:67-71)。"""
    routing_path = resolve_data_root(Path(repo_root)) / "build" / "routing.json"
    if not routing_path.is_file():
        return {}
    try:
        routing = load_routing(routing_path)
    except (OSError, ValueError):
        return {}
    mapping: dict[str, list[str]] = {}
    for platform_alerts in (routing.get("alerts") or {}).values():
        if not isinstance(platform_alerts, dict):
            continue
        for alertname, route in platform_alerts.items():
            doc = (route or {}).get("doc")
            if doc:
                mapping.setdefault(str(doc), []).append(str(alertname))
    return {doc: tuple(sorted(names)) for doc, names in mapping.items()}


def tree(repo_root: Path) -> dict:
    bases, warnings = build_tree(_kb_root(repo_root), _alerts_by_doc(repo_root))
    return {
        "index_state": index_state(repo_root),
        "bases": [asdict(base) for base in bases],
        "warnings": list(warnings),
        "platform_labels": {key: platform_label(key) for key in active_env_pack().knowledge_platforms()},
        "service_labels": dict(active_env_pack().service_labels),
    }


def topology(repo_root: Path) -> dict:
    """读取离线拓扑图派生物；拓扑构建仍由 build_topology/build_wiki 负责。"""
    path = _topology_path(repo_root)
    if not path.is_file():
        raise TopologyNotFound(
            f"拓扑图不存在: {path}; 请运行 .venv/bin/python scripts/build_topology.py"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TopologyNotFound(
            f"拓扑图不可读: {path}; 请运行 .venv/bin/python scripts/build_topology.py"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("nodes"), list)
        or not isinstance(payload.get("edges"), list)
    ):
        raise TopologyNotFound(
            f"拓扑图版本或结构不兼容: {path}; 请运行 .venv/bin/python scripts/build_topology.py"
        )
    return payload


def resolve_doc_path(repo_root: Path, rel_path: str) -> Path:
    """越界校验与 routes/triage_tasks.py:112-121 同法:resolve 后必须仍在 knowledge/ 内。

    迁移前的旧文档 ID(历史报告、任务里的链接)经 _redirects.yaml 映射到现行路径。
    """
    kb_root = _kb_root(repo_root)
    candidate = (kb_root / resolve_doc_id(kb_root, str(rel_path or ""))).resolve()
    try:
        candidate.relative_to(kb_root)
    except ValueError as exc:
        raise DocumentNotFound(f"文档路径越界: {rel_path}") from exc
    if candidate.suffix != ".md" or not candidate.is_file():
        raise DocumentNotFound(f"文档不存在: {rel_path}")
    return candidate


def read_doc(repo_root: Path, rel_path: str) -> dict:
    """→ 元信息 + frontmatter 原文 + 去掉 frontmatter 的正文 + wikilink 解析表。

    frontmatter 单独回传,前端按原样 <pre> 展示(对齐 MarkdownView 现有处理),
    正文里不再残留 YAML;wikilink 只给解析结果,替换在前端 mdast 上做。
    """
    kb_root = _kb_root(repo_root)
    path = resolve_doc_path(repo_root, rel_path)
    posix = path.relative_to(kb_root).as_posix()
    text = path.read_text(encoding="utf-8")
    frontmatter_raw, body = strip_frontmatter(text)

    meta: dict = {"path": posix, "title": path.stem, "doc_title": None,
                  "display_title": path.stem,
                  "platform": None, "service": None, "type": None, "status": None,
                  "problem": None, "symptoms": [], "alerts": [], "playbook": None,
                  "playbooks": [], "diagnostic_paths": [], "relations": [],
                  "knowledge_layer": None, "entrypoints": [], "profile": None,
                  "execution_scope": None, "confidence": None,
                  "observed_at": None, "valid_until": None,
                  "schema_version": None, "source": None, "source_ref": None,
                  "summary": None, "verified_by": None, "verified_at": None,
                  "verified_reason": None, "lint_error": None}
    base, inner = split_doc_path(path.relative_to(kb_root), discover_bases(kb_root))
    meta["knowledge_base"] = base.id if base else None
    if path.name == "_index.md":
        parts = inner.parts if base and base.layout == "platform_service" else ()
        meta["type"] = "index"
        meta["platform"] = parts[0] if len(parts) > 1 else None
        meta["service"] = parts[1] if len(parts) > 2 else None
    else:
        try:
            doc = load_doc(path)
            meta.update({
                "title": str(doc.title or path.stem),
                "platform": doc.platform, "service": doc.service,
                "type": doc.type, "status": doc.status, "problem": doc.problem,
                "symptoms": list(doc.symptoms), "playbook": doc.playbook,
                "playbooks": list(doc.playbooks),
                "diagnostic_paths": list(doc.diagnostic_paths),
                "relations": list(doc.relations),
                "knowledge_layer": doc.knowledge_layer,
                "entrypoints": list(doc.entrypoints),
                "profile": doc.profile,
                "execution_scope": doc.execution_scope,
                "confidence": doc.confidence,
                "observed_at": doc.observed_at,
                "valid_until": doc.valid_until,
                "schema_version": doc.raw.get("schema_version"),
                "source": doc.raw.get("source"),
                "source_ref": doc.raw.get("source_ref"),
                "summary": doc.raw.get("summary"),
                "verified_by": (str(doc.raw.get("verified_by"))
                                if doc.raw.get("verified_by") is not None else None),
                "verified_at": (str(doc.raw.get("verified_at"))
                                if doc.raw.get("verified_at") is not None else None),
                "verified_reason": (str(doc.raw.get("verified_reason"))
                                    if doc.raw.get("verified_reason") is not None else None),
                "category": doc_category(doc.type, doc.profile),
                "sop_kind": sop_kind_of(doc.type, doc.sop_kind),
            })
        except FrontmatterError as exc:
            # 坏 frontmatter 不挡阅读:正文照给,错误显形让人能去修
            meta["lint_error"] = str(exc)

    meta["doc_title"] = extract_h1(body)
    meta["display_title"] = display_title(
        title=meta["title"], doc_title=meta["doc_title"],
        platform=meta["platform"], service=meta["service"],
        problem=meta["problem"], path=Path(inner),
        doc_type=meta["type"],
    )
    meta["alerts"] = list(_alerts_by_doc(repo_root).get(posix, ()))
    wikilinks = []
    for link in resolve_wikilinks(body, kb_root, data_root=resolve_data_root(Path(repo_root))):
        value = asdict(link)
        if value.get("href") is None:
            value.pop("href", None)
        wikilinks.append(value)
    return {
        "path": posix,
        "meta": meta,
        "frontmatter_raw": frontmatter_raw,
        "markdown": body,
        "wikilinks": wikilinks,
    }


_RUN_ID_RE = re.compile(r"\bts-\d{8}-\d{4,6}-[0-9a-z]+\b")
_CASE_SECTION_RE = re.compile(r"^##\s+历史案例\s*$")


def _case_lines(markdown: str) -> list[dict]:
    """文档自述的历史案例行。只有 11 篇有,是 CLI 侧排查唯一的文档级回链。"""
    lines: list[dict] = []
    collecting = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            collecting = bool(_CASE_SECTION_RE.match(line))
            continue
        if not collecting:
            continue
        text = line.strip()
        if not text.startswith("-"):
            continue
        run_id = _RUN_ID_RE.search(text)
        lines.append({"text": text.lstrip("- ").strip(),
                      "run_id": run_id.group(0) if run_id else None})
    return lines


def _task_references(state_dir: Path, aliases: frozenset[str]) -> list[dict]:
    """扫 triage 任务找回链。24 份 JSON 全量扫,几十毫秒,不值得建缓存。"""
    root = Path(state_dir) / "triage-tasks"
    if not root.is_dir():
        return []
    found: list[dict] = []
    for path in sorted(root.glob("*/task.json")):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(task, dict):
            continue
        relations = []
        if task.get("kb_doc") in aliases:
            relations.append("produced")
        if task.get("matched_doc") in aliases:
            relations.append("matched")
        if aliases & set(task.get("consulted_docs") or []):
            relations.append("consulted")
        if not relations:
            continue
        found.append({
            "task_id": task.get("task_id"),
            "run_id": task.get("run_id"),
            "status": task.get("status"),
            "source": task.get("source"),
            "platform": task.get("platform"),
            "created_at": task.get("created_at"),
            "relations": relations,
        })
    found.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return found


def _conclusion_references(repo_root: Path, aliases: frozenset[str]) -> list[dict]:
    root = _records_root(repo_root)
    if not root.is_dir():
        return []
    found: list[dict] = []
    for path in sorted(root.glob("*/artifacts/conclusion.json")):
        try:
            conclusion = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(conclusion, dict):
            continue
        draft = conclusion.get("kb_draft")
        draft_doc = draft.get("doc") if isinstance(draft, dict) else None
        if conclusion.get("kb_doc") not in aliases and draft_doc not in aliases:
            continue
        found.append({
            "run_id": path.parent.parent.name,
            "verdict": conclusion.get("verdict"),
            "severity": conclusion.get("severity"),
            "kb_doc_effective": conclusion.get("kb_doc_effective"),
            "via": "kb_doc" if conclusion.get("kb_doc") in aliases else "kb_draft",
        })
    found.sort(key=lambda item: item["run_id"], reverse=True)
    return found


def references(repo_root: Path, state_dir: Path, rel_path: str) -> dict:
    """谁引用过这篇文档。

    覆盖面不对称,如实回传而不是假装完整:中台任务侧字段齐全,
    CLI 排查侧的 session.json 没有 kb_doc 字段,只能靠 artifacts/conclusion.json
    与文档自述的历史案例段兜底。
    """
    path = resolve_doc_path(repo_root, rel_path)
    posix = path.relative_to(_kb_root(repo_root)).as_posix()
    aliases = doc_id_aliases(_kb_root(repo_root), posix)
    _, body = strip_frontmatter(path.read_text(encoding="utf-8"))
    sessions = list(_records_root(repo_root).glob("*/session.json"))
    conclusions = _conclusion_references(repo_root, aliases)
    return {
        "path": posix,
        "tasks": _task_references(state_dir, aliases),
        "alerts": list(_alerts_by_doc(repo_root).get(posix, ())),
        "conclusions": conclusions,
        "case_lines": _case_lines(body),
        "coverage": {
            "sessions_total": len(sessions),
            "sessions_linkable": len(list(
                _records_root(repo_root)
                .glob("*/artifacts/conclusion.json"))),
            "note": "CLI 排查的 session.json 无 kb_doc 字段，"
                    "只有留下 conclusion.json 的 run 能回链到文档",
        },
    }


def _iter_docs(kb_root: Path):
    """全库 frontmatter 遍历,口径与 recall 索引一致(索引里没有的不该参与统计)。"""
    root = Path(kb_root).resolve()
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if (relative.name == "_index.md"
                or "_templates" in relative.parts
                or "_eval" in relative.parts):
            continue
        try:
            doc = load_doc(path)
        except FrontmatterError:
            continue
        if doc.type == "oplog":
            continue
        yield relative.as_posix(), doc


def _referenced_docs(repo_root: Path, state_dir: Path) -> dict[str, dict]:
    """一次扫出全库的 doc → 引用计数,避免对每篇文档各跑一次 references()。"""
    counts: dict[str, dict] = {}

    def bump(doc: object, kind: str) -> None:
        if not isinstance(doc, str) or not doc:
            return
        entry = counts.setdefault(doc, {"tasks": 0, "conclusions": 0})
        entry[kind] += 1

    tasks_root = Path(state_dir) / "triage-tasks"
    if tasks_root.is_dir():
        for path in tasks_root.glob("*/task.json"):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(task, dict):
                continue
            for doc in {task.get("kb_doc"), task.get("matched_doc"),
                        *(task.get("consulted_docs") or [])}:
                bump(doc, "tasks")

    records_root = _records_root(repo_root)
    if records_root.is_dir():
        for path in records_root.glob("*/artifacts/conclusion.json"):
            try:
                conclusion = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(conclusion, dict):
                continue
            draft = conclusion.get("kb_draft")
            for doc in {conclusion.get("kb_doc"),
                        draft.get("doc") if isinstance(draft, dict) else None}:
                bump(doc, "conclusions")
    return counts


def _read_json_object(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _promotion_run_refs(repo_root: Path, state_dir: Path, posix: str) -> list[dict]:
    aliases = doc_id_aliases(_kb_root(repo_root), posix)
    refs: dict[str, dict] = {}
    tasks_root = Path(state_dir) / "triage-tasks"
    if tasks_root.is_dir():
        for path in tasks_root.glob("*/task.json"):
            task = _read_json_object(path)
            if task is None or not aliases & set(task.get("consulted_docs") or []):
                continue
            run_id = task.get("run_id")
            if isinstance(run_id, str) and run_id:
                refs.setdefault(run_id, {"run_id": run_id, "via": []})["via"].append("consulted")

    records_root = _records_root(repo_root)
    if records_root.is_dir():
        for path in records_root.glob("*/artifacts/conclusion.json"):
            conclusion = _read_json_object(path)
            if conclusion is None or conclusion.get("kb_doc") not in aliases:
                continue
            run_id = path.parent.parent.name
            item = refs.setdefault(run_id, {"run_id": run_id, "via": []})
            item["via"].append("kb_doc")
            item["conclusion"] = conclusion
    return sorted(refs.values(), key=lambda item: item["run_id"], reverse=True)


def _promotion_review(repo_root: Path, state_dir: Path, posix: str, doc) -> Optional[dict]:
    path = _kb_root(repo_root) / posix
    _, body = strip_frontmatter(path.read_text(encoding="utf-8"))
    run_refs = _promotion_run_refs(repo_root, state_dir, posix)
    if not run_refs:
        return None

    attempts = []
    for ref in run_refs:
        run_id = ref["run_id"]
        conclusion = ref.get("conclusion") or {}
        session = _read_json_object(
            _records_root(repo_root) / run_id / "session.json"
        ) or {}
        verdict = conclusion.get("verdict") or session.get("verdict")
        decision = evaluate_auto_promotion(repo_root, posix, run_id)
        attempts.append({
            "run_id": run_id, "via": sorted(set(ref["via"])),
            "verdict": verdict,
            "aligned": decision.eligible or "报告与文档关键内容呼应不足" not in decision.reasons,
            "explicit_doc_reference": decision.explicit_doc_reference,
            "matched_terms": list(decision.matched_terms),
            "eligible": decision.eligible,
            "reasons": list(decision.reasons),
        })

    eligible = next((attempt for attempt in attempts if attempt["eligible"]), None)
    return {
        "path": posix,
        "title": display_title(
            title=doc.title, doc_title=extract_h1(body), platform=doc.platform,
            service=doc.service, problem=doc.problem,
            path=Path(split_doc_path(posix, discover_bases(_kb_root(repo_root)))[1]),
            doc_type=doc.type,
        ),
        "platform": str(doc.platform or ""), "service": str(doc.service or ""),
        "eligible": eligible is not None,
        "run_id": eligible["run_id"] if eligible else None,
        "attempts": attempts,
    }


def health(repo_root: Path, state_dir: Path) -> dict:
    """Read-only health view; mutations belong to the worker/importer."""
    kb_root = _kb_root(repo_root)
    documents = list(_iter_docs(kb_root))
    referenced = _referenced_docs(repo_root, state_dir)

    status_counts = {"draft": 0, "verified": 0, "other": 0}
    by_platform: dict[str, dict] = {}
    by_type: dict[str, int] = {}
    orphans: list[dict] = []
    promotion_reviews: list[dict] = []

    for posix, doc in documents:
        status = str(doc.status or "")
        status_counts[status if status in status_counts else "other"] += 1
        by_type[str(doc.type or "?")] = by_type.get(str(doc.type or "?"), 0) + 1

        platform = str(doc.platform or "?")
        stats = by_platform.setdefault(platform, {
            "platform": platform, "documents": 0, "troubleshoot": 0,
            "with_alerts": 0, "draft": 0, "verified": 0})
        stats["documents"] += 1
        if doc.type == "troubleshoot":
            stats["troubleshoot"] += 1
        if doc.alerts:
            stats["with_alerts"] += 1
        if status in ("draft", "verified"):
            stats[status] += 1

        # 召回孤儿:FTS 高权重列(title 10/problem 8/symptoms 6/alerts 6)里
        # symptoms 与 alerts 全空,只剩 body 权重 1.0,Top5 上限下基本召不回
        if doc.type == "troubleshoot" and not doc.symptoms and not doc.alerts:
            orphans.append({"path": posix, "title": str(doc.title or ""),
                            "platform": platform, "service": str(doc.service or "")})

        if status == "draft" and doc.type == "troubleshoot" and referenced.get(posix):
            review = _promotion_review(repo_root, state_dir, posix, doc)
            if review is not None:
                promotion_reviews.append(review)

    promotion_reviews.sort(key=lambda item: (not item["eligible"], item["path"]))
    candidates = [item for item in promotion_reviews if item["eligible"]]
    troubleshoot_total = by_type.get("troubleshoot", 0)
    return {
        "index": index_state(repo_root),
        "documents_total": len(documents),
        "status_counts": status_counts,
        "type_counts": by_type,
        "platform_coverage": [by_platform[key] for key in sorted(by_platform)],
        "recall_orphans": {
            "count": len(orphans),
            "troubleshoot_total": troubleshoot_total,
            "documents": orphans,
        },
        "promotion_candidates": candidates,
        "promotion_reviews": promotion_reviews,
    }


def search(repo_root: Path, query: str, *, platform: Optional[str] = None,
           service: Optional[str] = None, doc_type: Optional[str] = None,
           limit: int = BROWSE_LIMIT_DEFAULT) -> dict:
    """RecallIndexError 由调用方转 503——索引问题必须显形,不退化成空结果。"""
    hits = browse_search(_index_path(repo_root), query, platform=platform,
                         service=service, doc_type=doc_type, limit=limit)
    return {
        "index_state": index_state(repo_root),
        "hits": [asdict(hit) for hit in hits],
    }
