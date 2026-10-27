"""人工浏览侧的知识库查询:服务树、wikilink 解析、放宽约束的全文检索。

与 recall.py 的 agent 召回契约物理隔离——search_recall_index 的 Top5 硬上限与
「平台必填 + 隐式并 shared」是被验收过的 agent 行为,人工浏览不该受它约束,
也不该为了浏览去改它。两者共享同一张 kb_docs 表与同一个只读打开器;浏览走
trigram FTS 排序,agent 召回另有字段加权排序(recall._rank_documents)。
"""
from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lib.envpack import KNOWLEDGE_ENV_SCOPE, active_env_pack
from lib.kb.bases import (
    discover_bases, iter_documents, resolve_knowledge_root, split_doc_path,
)
from lib.kb.categories import doc_category, sop_kind_of
from lib.kb.frontmatter import FrontmatterError, load_doc
from lib.kb.recall import (
    _SEARCH_COLUMNS,
    RecallIndexError,
    _literal_match_clause,
    _literal_match_count,
    _literal_match_parameters,
    _match_query,
    _open_readonly,
    _short_query_terms,
)

import sqlite3

BROWSE_LIMIT_MAX = 50
BROWSE_LIMIT_DEFAULT = 20
# 与 wiki.js 的 PLAT_ORDER 一致,保证两个阅读器的左栏顺序不打架
# bm25 列权重:前 6 列 UNINDEXED,后 5 列
# title 10 / problem 8 / symptoms 6 / alerts 6 / body 1
_BM25 = "-bm25(kb_docs, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 8.0, 6.0, 6.0, 1.0)"
_SNIPPET = "snippet(kb_docs, -1, '', '', ' ... ', 32)"
# 与 wiki.py:36 同源:group(1)=target(anchor 丢弃),group(2)=label
_WIKILINK_RE = re.compile(
    r"\[\[((?:\\(?!\|)|[^\[\]|#\\])+?)(?:#[^\[\]|]*)?"
    r"(?:\\?\|([^\[\]]*))?\]\]"
)
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")

@dataclass(frozen=True)
class BrowseHit:
    path: str
    platform: str
    service: str
    doc_type: str
    status: str
    title: str
    display_title: str
    score: float
    snippet: str
    category: str = "reference"
    sop_kind: Optional[str] = None


@dataclass(frozen=True)
class TreeDoc:
    path: str
    title: str
    doc_title: Optional[str]
    display_title: str
    component: Optional[str]
    doc_type: str
    status: str
    problem: Optional[str]
    alerts: tuple[str, ...]
    category: str = "reference"
    sop_kind: Optional[str] = None


@dataclass(frozen=True)
class ServiceNode:
    service: str
    index_doc: Optional[str]
    index_title: Optional[str]
    component_indexes: tuple["ComponentIndex", ...]
    docs: tuple[TreeDoc, ...]


@dataclass(frozen=True)
class ComponentIndex:
    component: str
    index_doc: str
    index_title: Optional[str]


@dataclass(frozen=True)
class PlatformNode:
    platform: str
    services: tuple[ServiceNode, ...]


@dataclass(frozen=True)
class BaseNode:
    id: str
    name: str
    description: str
    layout: str
    platforms: tuple[PlatformNode, ...]


@dataclass(frozen=True)
class WikiLink:
    target: str
    label: str
    resolved_path: Optional[str]
    ambiguous: bool
    href: Optional[str] = None


def _report_task_href(data_root: Path, run_id: str) -> Optional[str]:
    """Resolve a report run to the task report route when the task is known."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
        return None
    state_root = data_root / "state" / "triage-tasks"
    if state_root.is_dir():
        for task_path in state_root.glob("*/task.json"):
            try:
                import json
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(task, dict) and task.get("run_id") == run_id and task.get("task_id"):
                return f"/triage/{task['task_id']}/report"
    from lib.datastore.local import LocalDataStore  # 延迟导入:lib.record 经 lib.kb 回引

    report = LocalDataStore(data_root).troubleshoot.run_dir(run_id) / "report.md"
    return f"/reports/troubleshoot/{run_id}" if report.is_file() else None


def service_label(service: object) -> str:
    """服务显示名取环境包 knowledge.service_labels;未登记的原样显示。"""
    value = str(service or "").strip()
    return active_env_pack().service_labels.get(value.lower(), value)


def _is_machine_title(title: str, stem: str) -> bool:
    value = title.strip()
    return not value or value == stem or (
        " " not in value and "-" in value and value.lower() == value
    )


def _platform_title(value: object) -> str:
    key = str(value or "").strip().lower()
    if key == KNOWLEDGE_ENV_SCOPE:
        return "Shared"
    decl = active_env_pack().knowledge_cluster(key)
    if decl is not None:
        return decl.short_name
    return str(value or "").strip()


def _title_scope(*, service: object, path: Path) -> str:
    service_value = service_label(service) or path.parent.name
    # 四级路径表示 platform/service/component/document；组件放进标题上下文，
    # 让同一服务下的 NameNode/DataNode 等文档在列表和检索结果中一眼可分。
    component = path.parts[2] if len(path.parts) >= 4 else ""
    if component and component not in {"_index", str(service or "")}:
        component_value = service_label(component)
        if component_value and component_value.lower() != service_value.lower():
            return f"{service_value}/{component_value}"
    return service_value


def _title_problem(*, doc_title: Optional[str], problem: object,
                   platform: object, service: object, path: Path) -> str:
    """Extract the human problem part without repeating platform/service names."""
    candidate = str(doc_title or "").strip()
    platform_value = _platform_title(platform)
    scope_parts = [service_label(service) or path.parent.name]
    if len(path.parts) >= 4 and path.parts[2] != "_index":
        scope_parts.append(service_label(path.parts[2]))
    # Titles from legacy documents often start with platform + service + component;
    # remove each prefix repeatedly while keeping the original problem wording.
    prefixes = [platform_value, str(platform or "").strip(), *scope_parts]
    for prefix in prefixes:
        if not prefix:
            continue
        variants = [prefix]
        if prefix.lower() == "spark":
            variants.extend(["Spark23", "Spark2", "Spark 2.3"])
        pattern = "|".join(re.escape(item) for item in variants)
        candidate = re.sub(
            rf"^(?:{pattern})(?=$|[\s/·—–-])[\s/·—–-]*",
            "", candidate, flags=re.IGNORECASE,
        ).strip()
    candidate = re.sub(r"^服务\s*", "", candidate)
    suffixes = "|".join(re.escape(item.short_name) for item in active_env_pack().clusters
                        if "knowledge" in item.roles)
    if suffixes:
        candidate = re.sub(rf"\s*[（(](?:{suffixes})[)）]\s*$", "", candidate,
                           flags=re.IGNORECASE).strip()
    if candidate:
        return candidate
    fallback = str(problem or "").strip()
    if fallback in {"baseline", "environment-baseline"}:
        return "服务基线"
    if fallback:
        return fallback.replace("-", " ")
    return "知识条目"


def display_title(*, title: object, doc_title: Optional[str], platform: object,
                  service: object, problem: object, path: Path,
                  doc_type: object) -> str:
    """Return one structured human title for tree, search and detail views.

    The raw frontmatter/H1 remain unchanged for traceability.  This presentation
    contract makes titles deterministic for humans and vector-search metadata:
    ``Platform - Service[/Component] - Problem``.
    """
    raw_title = str(title or "").strip()
    # Index pages are navigation documents; preserve their authored heading.
    if path.stem == "_index" and doc_title and doc_title.strip():
        return doc_title.strip()
    if not raw_title and not doc_title and not problem:
        return path.stem
    platform_value = _platform_title(platform)
    scope_value = _title_scope(service=service, path=path)
    problem_value = str(problem or "").strip()
    if problem_value in {"baseline", "environment-baseline"} or path.stem.endswith("-baseline"):
        problem_text = "服务基线"
    else:
        problem_text = _title_problem(
            doc_title=doc_title if doc_title and doc_title.strip() else raw_title,
            problem=problem, platform=platform, service=service, path=path,
        )
    parts = [part for part in (platform_value, scope_value, problem_text) if part]
    return " - ".join(parts) or raw_title or path.stem


def _is_browsable(relative: Path) -> bool:
    """与 recall._iter_documents 同口径:索引里没有的东西,树里也不该有。"""
    return not (
        relative.name == "_index.md"
        or "_templates" in relative.parts
        or "_eval" in relative.parts
    )


def extract_h1(markdown: str) -> Optional[str]:
    """正文首个 H1(人类可读标题)。跳过 fenced code,避免把 shell 注释当标题。"""
    in_fence = False
    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = _H1_RE.match(line)
        if matched:
            return matched.group(1).strip() or None
    return None


def strip_frontmatter(text: str) -> tuple[str, str]:
    """→ (frontmatter 原文, 正文)。无 frontmatter 时前者为空串。

    不复用 recall 的 _FM_RE:那个 \\s* 会贪婪吃掉闭合线后的空行,
    这里正文要原样交给前端渲染,不能悄悄改变首行间距。
    """
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return "", stripped
    matched = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", stripped, re.S)
    if matched is None:
        return "", stripped
    return matched.group(1), stripped[matched.end():]


def _wikilink_tables(kb_root: Path) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """→ (BY_REL, BY_SLUG, 歧义 slug 集合)。规则与 wiki.py:250-256 一致。

    BY_SLUG 同名冲突时既有 wiki 静默取最后一篇;这里保持同样的跳转目标,
    额外把冲突 slug 记下来供 UI 提示,不在本层消歧。
    """
    by_rel: dict[str, str] = {}
    by_slug: dict[str, str] = {}
    ambiguous: set[str] = set()
    for item in iter_documents(kb_root):
        posix = item.doc_id
        by_rel[posix[:-3]] = posix
        slug = item.path.stem
        if slug in by_slug:
            ambiguous.add(slug)
        by_slug[slug] = posix
    return by_rel, by_slug, ambiguous


def resolve_wikilinks(markdown: str, kb_root: Path, *,
                      data_root: Optional[Path] = None) -> tuple[WikiLink, ...]:
    """解析正文里的 [[...]],不做替换——替换在前端 mdast 上做。

    ``[[report:<run_id>]]`` 按 data_root 下的 state/records 定位任务与报告;
    缺省取 kb_root 的父目录(知识库位于数据根内的布局)。

    在 markdown 源上做替换需要手写 code 段识别(fenced/indented/inline 三种),
    而 remark 解析后 code 与 inlineCode 是独立节点类型,天然跳过。
    bash 的 `[[ -f x ]]` 只出现在代码里,由那层豁免兜住(对齐 wiki.py:38-46)。
    """
    by_rel, by_slug, ambiguous = _wikilink_tables(kb_root)
    seen: set[tuple[str, str]] = set()
    links: list[WikiLink] = []
    for matched in _WIKILINK_RE.finditer(markdown):
        target = matched.group(1).strip()
        label = (matched.group(2) or "").strip() or target
        if not target or (target, label) in seen:
            continue
        seen.add((target, label))
        if target.startswith("report:"):
            run_id = target.removeprefix("report:")
            links.append(WikiLink(
                target=target,
                label=label,
                resolved_path=None,
                ambiguous=False,
                href=_report_task_href(Path(data_root) if data_root else kb_root.parent, run_id),
            ))
            continue
        resolved = by_rel.get(target) or by_slug.get(target)
        links.append(WikiLink(
            target=target,
            label=label,
            resolved_path=resolved,
            ambiguous=resolved is not None and target not in by_rel and target in ambiguous,
            href=None,
        ))
    return tuple(links)


def build_tree(kb_root: Path,
               alerts_by_doc: Optional[dict[str, tuple[str, ...]]] = None,
               ) -> tuple[tuple[BaseNode, ...], tuple[str, ...]]:
    """扫知识库根,按库组装 base → platform → service 三级树。

    单篇 frontmatter 坏掉降级成 warning,不炸整棵树(对齐 wiki.py 的单篇降级)。
    _index.md 不进文档列表:platform_service 库的 platform/service/_index.md 是服务概览;
    flat 库(如按类型分组的库)以库根 _index.md 作为其唯一服务的概览。
    """
    root = Path(kb_root).resolve()
    bases = discover_bases(root)
    alerts_by_doc = alerts_by_doc or {}
    grouped: dict[str, dict[str, dict[str, list[TreeDoc]]]] = {}
    indexes: dict[tuple[str, str, str], tuple[str, Optional[str]]] = {}
    component_indexes: dict[tuple[str, str, str, str], tuple[str, Optional[str]]] = {}
    warnings: list[str] = []

    for item in iter_documents(root, bases):
        base, inner, posix = item.base, item.inner, item.doc_id
        try:
            text = item.path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"{posix}: 不可读 — {exc}")
            continue

        if inner.name == "_index.md":
            # 更深的如 hd/hdfs/namenode/_index.md 属于组件概览,不能按 (platform, service)
            # 覆盖服务概览;库根 _index 是库总索引,flat 库拿它当服务概览。
            _, body = strip_frontmatter(text)
            if base.layout == "flat" and len(inner.parts) == 1 and base.platform and base.service:
                indexes[(base.id, base.platform, base.service)] = (posix, extract_h1(body))
            elif base.layout == "platform_service" and len(inner.parts) == 3:
                indexes[(base.id, inner.parts[0], inner.parts[1])] = (posix, extract_h1(body))
            elif base.layout == "platform_service" and len(inner.parts) == 4:
                component_indexes[(base.id, *inner.parts[:3])] = (posix, extract_h1(body))
            continue

        try:
            doc = load_doc(item.path)
        except FrontmatterError as exc:
            warnings.append(f"{posix}: {exc}")
            continue
        platform = str(doc.platform or "?")
        service = str(doc.service or "?")
        _, body = strip_frontmatter(text)
        doc_title = extract_h1(body)
        if base.layout == "flat":
            component = inner.parts[0] if len(inner.parts) >= 2 else None
        else:
            component = inner.parts[2] if len(inner.parts) >= 4 else None
        grouped.setdefault(base.id, {}).setdefault(platform, {}).setdefault(service, []).append(TreeDoc(
            path=posix,
            title=str(doc.title or item.path.stem),
            doc_title=doc_title,
            display_title=display_title(
                title=doc.title, doc_title=doc_title, platform=doc.platform,
                service=doc.service, problem=doc.problem, path=Path(inner),
                doc_type=doc.type,
            ),
            component=component,
            doc_type=str(doc.type or ""),
            status=str(doc.status or ""),
            problem=doc.problem,
            alerts=tuple(alerts_by_doc.get(posix, ())),
            category=doc_category(doc.type, doc.profile),
            sop_kind=sop_kind_of(doc.type, doc.sop_kind),
        ))

    nodes: list[BaseNode] = []
    for base in bases:
        by_platform = grouped.get(base.id, {})
        order = active_env_pack().knowledge_platforms()
        ordered_platforms = [p for p in order if p in by_platform]
        ordered_platforms += sorted(p for p in by_platform if p not in order)
        platforms: list[PlatformNode] = []
        for platform in ordered_platforms:
            services = []
            for service in sorted(by_platform[platform]):
                index_doc, index_title = indexes.get((base.id, platform, service), (None, None))
                components = tuple(
                    ComponentIndex(component=component, index_doc=doc, index_title=title)
                    for (item_base, item_platform, item_service, component), (doc, title)
                    in sorted(component_indexes.items())
                    if (item_base, item_platform, item_service) == (base.id, platform, service)
                )
                services.append(ServiceNode(
                    service=service,
                    index_doc=index_doc,
                    index_title=index_title,
                    component_indexes=components,
                    docs=tuple(sorted(by_platform[platform][service], key=lambda d: d.path)),
                ))
            platforms.append(PlatformNode(platform=platform, services=tuple(services)))
        nodes.append(BaseNode(
            id=base.id, name=base.name, description=base.description,
            layout=base.layout, platforms=tuple(platforms),
        ))
    return tuple(nodes), tuple(warnings)


def _browse_filters(platform: Optional[str], service: Optional[str],
                    doc_type: Optional[str]) -> tuple[str, list[object]]:
    """人工浏览:platform 缺省即全库,不像 agent 侧隐式并上 shared。"""
    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (("platform", platform), ("service", service),
                          ("doc_type", doc_type)):
        if value:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    return (" AND ".join(clauses) if clauses else "1"), parameters


def browse_search(index_path: Path, query: str, *,
                  platform: Optional[str] = None,
                  service: Optional[str] = None,
                  doc_type: Optional[str] = None,
                  limit: int = BROWSE_LIMIT_DEFAULT) -> tuple[BrowseHit, ...]:
    """人工检索:平台/服务/类型均可选,条数上限 50。

    索引缺失、版本不匹配或损坏一律抛 RecallIndexError(禁懒构建、禁静默空结果),
    与 agent 侧同一套失败语义。
    """
    match_query = _match_query(query)
    short_terms = _short_query_terms(query)
    actual_limit = min(BROWSE_LIMIT_MAX, max(0, int(limit)))
    if (not match_query and not short_terms) or actual_limit == 0:
        return ()

    filters, filter_parameters = _browse_filters(platform, service, doc_type)
    columns = "path, platform, service, doc_type, status, title"
    fts_sql = (
        f"SELECT {columns}, {_BM25} AS score, {_SNIPPET} AS snippet_text "
        f"FROM kb_docs WHERE kb_docs MATCH ? AND {filters} "
        "ORDER BY score DESC, path ASC LIMIT ?"
    )
    literal_sql = (
        f"SELECT {columns}, 0.0 AS score, {_SNIPPET} AS snippet_text, "
        f"{', '.join(_SEARCH_COLUMNS)} "
        f"FROM kb_docs WHERE ({_literal_match_clause(short_terms)}) AND {filters} "
        "ORDER BY path ASC"
    ) if short_terms else ""

    try:
        with closing(_open_readonly(index_path)) as connection:
            rows: list[tuple[object, ...]] = []
            if match_query:
                rows = connection.execute(
                    fts_sql, [match_query, *filter_parameters, actual_limit],
                ).fetchall()
            if short_terms and not rows:
                # 短词(<3 字)进不了 trigram 索引,退回 LIKE 扫描并按命中词数排序
                parameters = [
                    *_literal_match_parameters(short_terms),
                    *filter_parameters,
                ]
                scored = (
                    (_literal_match_count(short_terms, row[8:]), row[:8])
                    for row in connection.execute(literal_sql, parameters)
                )
                rows = [
                    row for _, row in sorted(
                        (candidate for candidate in scored if candidate[0]),
                        key=lambda candidate: (-candidate[0], str(candidate[1][0])),
                    )[:actual_limit]
                ]
    except RecallIndexError:
        raise
    except sqlite3.Error as exc:
        raise RecallIndexError(f"知识库召回索引不可读；{exc}") from exc

    kb_root = resolve_knowledge_root(Path(index_path).resolve().parent.parent)
    bases = discover_bases(kb_root)
    hits = []
    for (path, hit_platform, hit_service, hit_type, status, title,
         score, snippet_text) in rows:
        relative = Path(str(path))
        body_h1 = None
        problem = ""
        profile = sop_kind = None
        try:
            source_path = kb_root / relative
            parsed = load_doc(source_path)
            _, source_body = strip_frontmatter(source_path.read_text(encoding="utf-8"))
            body_h1 = extract_h1(source_body)
            problem = parsed.problem
            profile, sop_kind = parsed.profile, parsed.sop_kind
        except (OSError, FrontmatterError):
            pass
        hits.append(BrowseHit(
            path=str(path),
            platform=str(hit_platform),
            service=str(hit_service),
            doc_type=str(hit_type),
            status=str(status),
            title=str(title),
            display_title=display_title(
                title=title, doc_title=body_h1, platform=hit_platform,
                service=hit_service, problem=problem,
                path=Path(split_doc_path(relative, bases)[1]),
                doc_type=hit_type,
            ),
            score=round(float(score), 6),
            snippet=str(snippet_text or "").strip(),
            category=doc_category(str(hit_type), profile),
            sop_kind=sop_kind_of(str(hit_type), sop_kind),
        ))
    return tuple(hits)
