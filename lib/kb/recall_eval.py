from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml

from lib.data_root import resolve_data_root
from lib.envpack import active_env_pack
from lib.kb.bases import iter_documents
from lib.kb.frontmatter import _FM_RE, load_doc


_CASE_FIELDS = {
    "id",
    "query",
    "platform",
    "expected_primary",
    "acceptable_support",
    "must_not_match",
    "source",
    "split",
    "notes",
}
_SOURCES = {"real_case", "reviewed_paraphrase", "negative"}
_SPLITS = {"dev", "holdout"}
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_QUERY_PART_RE = re.compile(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+")
_QUERY_STOPWORDS = (
    "调查",
    "问题",
    "相关",
    "已经",
    "以及",
    "关于",
    "进行",
    "的",
    "与",
    "和",
)
_SERVICE_ALIASES = {
    "yarn": ("yarn",),
    "hdfs": ("hdfs",),
    "spark": ("spark",),
}
_COMPONENT_ALIASES = {
    "datanode": ("datanode", "dn"),
    "namenode": ("namenode", "nn"),
    "nodemanager": ("nodemanager", "nm"),
    "resourcemanager": ("resourcemanager", "resource manager", "rm"),
    "thriftserver": ("thriftserver", "thrift server"),
    "executor": ("executor",),
}
_COMPONENT_SERVICES = {
    "datanode": "hdfs",
    "namenode": "hdfs",
    "nodemanager": "yarn",
    "resourcemanager": "yarn",
    "thriftserver": "spark",
    "executor": "spark",
}


class CorpusError(ValueError):
    pass


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    query: str
    platform: str | None
    expected_primary: tuple[str, ...]
    acceptable_support: tuple[str, ...]
    must_not_match: tuple[str, ...]
    source: str
    split: str
    notes: str


@dataclass(frozen=True)
class Corpus:
    version: int
    cases: tuple[EvalCase, ...]
    preliminary: bool


@dataclass(frozen=True)
class Section:
    heading: str
    ordinal: int
    text: str


@dataclass(frozen=True)
class SearchDocument:
    path: str
    title: str
    platform: str
    service: str
    problem: str
    symptoms: tuple[str, ...]
    alerts: tuple[str, ...]
    status: str
    doc_type: str
    related: tuple[str, ...]
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class SearchHit:
    path: str
    platform: str
    service: str
    doc_type: str
    score: float
    matched_section: str | None
    matched_text: str


@dataclass(frozen=True)
class QueryFeatures:
    terms: tuple[str, ...]
    services: frozenset[str]
    components: frozenset[str]


@dataclass(frozen=True)
class CaseScore:
    reciprocal_rank: float
    recall_at_1: bool
    recall_at_3: bool
    recall_at_5: bool
    support_hits: tuple[str, ...]
    must_not_violations: tuple[str, ...]
    negative_abstained: bool | None


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    query: str
    platform: str | None
    ranked_hits: tuple[SearchHit, ...]
    related_hits: tuple[SearchHit, ...]
    score: CaseScore
    latency_ms: float
    failure_class: str | None


@dataclass(frozen=True)
class SchemeReport:
    name: str
    runs: tuple[CaseRun, ...]
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    must_not_violation_count: int
    support_hit_count: int
    negative_abstention_rate: float | None
    latency_p50_ms: float
    latency_p95_ms: float


@dataclass(frozen=True)
class EvaluationReport:
    version: int
    preliminary: bool
    case_count: int
    holdout_count: int
    knowledge_fingerprint: str
    schemes: tuple[SchemeReport, ...]
    recommendation: str


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{context} 必须是 mapping")
    return value


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise CorpusError(f"{field} 必须是非空字符串")
    return value


def _validate_path_list(
    value: Any,
    field: str,
    knowledge_root: Path,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CorpusError(f"{field} 必须是 list")

    root = knowledge_root.resolve()
    paths: list[str] = []
    for index, raw_path in enumerate(value):
        relative = _require_string(raw_path, f"{field}[{index}]")
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.suffix.lower() != ".md":
            raise CorpusError(f"{field}[{index}] 必须是知识库内的 .md 相对路径")
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise CorpusError(f"{field}[{index}] 越过知识库边界")
        if candidate.name == "_index.md" or "_templates" in candidate.parts:
            raise CorpusError(f"{field}[{index}] 不能指向目录索引或模板")
        if not resolved.is_file():
            raise CorpusError(f"{field}[{index}] 指向的文档不存在: {relative}")
        paths.append(candidate.as_posix())

    if len(paths) != len(set(paths)):
        raise CorpusError(f"{field} 不能包含重复路径")
    return tuple(paths)


def load_corpus(path: Path, knowledge_root: Path) -> Corpus:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"读取评测语料失败: {exc}") from exc

    root = _require_mapping(raw, "corpus")
    if set(root) != {"version", "cases"}:
        raise CorpusError("corpus 只允许 version 和 cases 字段")
    if root["version"] != 1:
        raise CorpusError("corpus version 必须为 1")
    if not isinstance(root["cases"], list):
        raise CorpusError("cases 必须是 list")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(root["cases"]):
        case = _require_mapping(raw_case, f"cases[{index}]")
        if set(case) != _CASE_FIELDS:
            missing = sorted(_CASE_FIELDS - set(case))
            unknown = sorted(set(case) - _CASE_FIELDS)
            raise CorpusError(
                f"cases[{index}] 字段不合法: missing={missing}, unknown={unknown}"
            )

        case_id = _require_string(case["id"], f"cases[{index}].id")
        if case_id in seen_ids:
            raise CorpusError(f"重复 case id: {case_id}")
        seen_ids.add(case_id)

        platform = case["platform"]
        clusters = tuple(item.knowledge_key for item in active_env_pack().clusters
                         if "knowledge" in item.roles)
        if platform is not None and platform not in clusters:
            raise CorpusError(f"cases[{index}].platform 必须是 {'、'.join(clusters)} 或 null")
        source = _require_string(case["source"], f"cases[{index}].source")
        if source not in _SOURCES:
            raise CorpusError(f"cases[{index}].source 不合法: {source}")
        split = _require_string(case["split"], f"cases[{index}].split")
        if split not in _SPLITS:
            raise CorpusError(f"cases[{index}].split 不合法: {split}")

        expected = _validate_path_list(
            case["expected_primary"], "expected_primary", knowledge_root
        )
        if source != "negative" and not expected:
            raise CorpusError(f"cases[{index}] 非 negative 用例必须有主文档")
        if source == "negative" and expected:
            raise CorpusError(f"cases[{index}] negative 用例不能有主文档")

        cases.append(
            EvalCase(
                case_id=case_id,
                query=_require_string(case["query"], f"cases[{index}].query"),
                platform=platform,
                expected_primary=expected,
                acceptable_support=_validate_path_list(
                    case["acceptable_support"], "acceptable_support", knowledge_root
                ),
                must_not_match=_validate_path_list(
                    case["must_not_match"], "must_not_match", knowledge_root
                ),
                source=source,
                split=split,
                notes=_require_string(
                    case["notes"], f"cases[{index}].notes", allow_empty=True
                ),
            )
        )

    holdout_count = sum(case.split == "holdout" for case in cases)
    return Corpus(
        version=1,
        cases=tuple(cases),
        preliminary=len(cases) < 40 or holdout_count < 12,
    )


def _split_sections(body: str, fallback_heading: str) -> tuple[Section, ...]:
    sections: list[Section] = []
    heading_stack: dict[int, str] = {}
    current_heading = fallback_heading
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text or not sections:
            sections.append(
                Section(heading=current_heading, ordinal=len(sections), text=text)
            )
        buffer.clear()

    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match is None:
            buffer.append(line)
            continue

        if buffer or sections:
            flush()
        level = len(match.group(1))
        heading_stack[level] = match.group(2).strip()
        for deeper in tuple(key for key in heading_stack if key > level):
            del heading_stack[deeper]
        current_heading = " > ".join(
            heading_stack[key] for key in sorted(heading_stack)
        )

    if buffer or not sections:
        flush()
    return tuple(sections)


def _normalize_related_target(value: str) -> str:
    target = value.strip()
    if target.startswith("[[") and target.endswith("]]" ):
        target = target[2:-2]
    target = target.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    return target


def load_search_documents(knowledge_root: Path) -> tuple[SearchDocument, ...]:
    root = knowledge_root.resolve()
    raw_docs: list[tuple[str, Any, tuple[Section, ...]]] = []
    for item in iter_documents(root):
        path, relative = item.path, Path(item.doc_id)
        if path.name == "_index.md":
            continue
        parsed = load_doc(path)
        if parsed.type == "oplog":
            continue
        source = path.read_text(encoding="utf-8").lstrip("\ufeff")
        body = _FM_RE.sub("", source, count=1)
        raw_docs.append(
            (
                relative.as_posix(),
                parsed,
                _split_sections(body, str(parsed.title or relative.stem)),
            )
        )

    alias_map: dict[str, str | None] = {}
    for relative, _parsed, _sections in raw_docs:
        aliases = {
            relative,
            relative.removesuffix(".md"),
            Path(relative).name,
            Path(relative).stem,
        }
        for alias in aliases:
            previous = alias_map.get(alias)
            alias_map[alias] = relative if previous in {None, relative} else None

    documents: list[SearchDocument] = []
    for relative, parsed, sections in raw_docs:
        related: list[str] = []
        raw_related = parsed.raw.get("related") or []
        if isinstance(raw_related, list):
            for raw_target in raw_related:
                target = _normalize_related_target(str(raw_target))
                resolved = alias_map.get(target) or alias_map.get(f"{target}.md")
                if resolved and resolved != relative and resolved not in related:
                    related.append(resolved)
        documents.append(
            SearchDocument(
                path=relative,
                title=str(parsed.title or Path(relative).stem),
                platform=str(parsed.platform or ""),
                service=str(parsed.service or ""),
                problem=str(parsed.problem or ""),
                symptoms=tuple(parsed.symptoms),
                alerts=tuple(parsed.alerts),
                status=str(parsed.status or ""),
                doc_type=str(parsed.type or ""),
                related=tuple(related),
                sections=sections,
            )
        )
    return tuple(sorted(documents, key=lambda item: item.path))


def _eligible_documents(
    docs: tuple[SearchDocument, ...], platform: str | None
) -> tuple[SearchDocument, ...]:
    if platform is None:
        return docs
    return tuple(doc for doc in docs if doc.platform in {platform, "shared"})


def _as_hit(
    doc: SearchDocument,
    *,
    score: float,
    matched_section: str | None,
    matched_text: str,
) -> SearchHit:
    return SearchHit(
        path=doc.path,
        platform=doc.platform,
        service=doc.service,
        doc_type=doc.doc_type,
        score=score,
        matched_section=matched_section,
        matched_text=matched_text,
    )


def search_current_filter(
    docs: tuple[SearchDocument, ...],
    query: str,
    *,
    platform: str | None,
    limit: int = 5,
) -> list[SearchHit]:
    del query
    ordered = sorted(
        _eligible_documents(docs, platform),
        key=lambda doc: (doc.platform, doc.service, doc.path),
    )
    return [
        _as_hit(doc, score=0.0, matched_section=None, matched_text="")
        for doc in ordered[: max(0, limit)]
    ]


def _query_terms(query: str) -> tuple[str, ...]:
    cleaned = query.lower()
    for stopword in _QUERY_STOPWORDS:
        cleaned = cleaned.replace(stopword, " ")

    terms: list[str] = []
    for part in _QUERY_PART_RE.findall(cleaned):
        if "\u3400" <= part[0] <= "\u9fff" and len(part) > 3:
            terms.extend(part[index : index + 3] for index in range(len(part) - 2))
        else:
            terms.append(part)
    return tuple(dict.fromkeys(term for term in terms if term.strip()))


def _contains_alias(text: str, alias: str) -> bool:
    escaped = re.escape(alias.lower())
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.lower()))


def _matching_entities(text: str, aliases: dict[str, tuple[str, ...]]) -> set[str]:
    return {
        entity
        for entity, values in aliases.items()
        if any(_contains_alias(text, alias) for alias in values)
    }


def _query_features(query: str) -> QueryFeatures:
    components = _matching_entities(query, _COMPONENT_ALIASES)
    services = _matching_entities(query, _SERVICE_ALIASES)
    services.update(
        _COMPONENT_SERVICES[component]
        for component in components
        if component in _COMPONENT_SERVICES
    )
    return QueryFeatures(
        terms=_query_terms(query),
        services=frozenset(services),
        components=frozenset(components),
    )


def _document_components(doc: SearchDocument) -> frozenset[str]:
    structural_text = "\n".join((doc.path, doc.title, doc.problem))
    return frozenset(_matching_entities(structural_text, _COMPONENT_ALIASES))


def _searchable_text(doc: SearchDocument) -> str:
    return "\n".join(
        (
            doc.title,
            doc.problem,
            "\n".join(doc.symptoms),
            "\n".join(doc.alerts),
            _document_text(doc),
        )
    ).lower()


def _fts_query(query: str) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in _query_terms(query))


def _document_text(doc: SearchDocument) -> str:
    return "\n".join(
        part
        for section in doc.sections
        for part in (section.heading, section.text)
        if part
    )


def search_fts_documents(
    docs: tuple[SearchDocument, ...],
    query: str,
    *,
    platform: str | None,
    limit: int = 5,
) -> list[SearchHit]:
    match_query = _fts_query(query)
    if not match_query or limit <= 0:
        return []

    eligible = _eligible_documents(docs, platform)
    by_path = {doc.path: doc for doc in eligible}
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE kb_docs USING fts5("
            "path UNINDEXED, platform UNINDEXED, title, problem, symptoms, alerts, body, "
            "tokenize='trigram')"
        )
        connection.executemany(
            "INSERT INTO kb_docs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    doc.path,
                    doc.platform,
                    doc.title,
                    doc.problem,
                    "\n".join(doc.symptoms),
                    "\n".join(doc.alerts),
                    _document_text(doc),
                )
                for doc in eligible
            ],
        )
        rows = connection.execute(
            "SELECT path, -bm25(kb_docs, 0.0, 0.0, 10.0, 8.0, 6.0, 6.0, 1.0) AS score "
            "FROM kb_docs WHERE kb_docs MATCH ? ORDER BY score DESC, path ASC LIMIT ?",
            (match_query, limit),
        ).fetchall()

    return [
        _as_hit(
            by_path[path],
            score=float(score),
            matched_section=None,
            matched_text=_document_text(by_path[path])[:320],
        )
        for path, score in rows
    ]


def search_fts_sections(
    docs: tuple[SearchDocument, ...],
    query: str,
    *,
    platform: str | None,
    limit: int = 5,
) -> list[SearchHit]:
    match_query = _fts_query(query)
    if not match_query or limit <= 0:
        return []

    eligible = _eligible_documents(docs, platform)
    by_path = {doc.path: doc for doc in eligible}
    section_lookup = {
        (doc.path, section.ordinal): section
        for doc in eligible
        for section in doc.sections
    }
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE kb_sections USING fts5("
            "path UNINDEXED, ordinal UNINDEXED, title, problem, symptoms, alerts, heading, body, "
            "tokenize='trigram')"
        )
        connection.executemany(
            "INSERT INTO kb_sections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    doc.path,
                    section.ordinal,
                    doc.title,
                    doc.problem,
                    "\n".join(doc.symptoms),
                    "\n".join(doc.alerts),
                    section.heading,
                    section.text,
                )
                for doc in eligible
                for section in doc.sections
            ],
        )
        rows = connection.execute(
            "SELECT path, ordinal, "
            "-bm25(kb_sections, 0.0, 0.0, 10.0, 8.0, 6.0, 6.0, 5.0, 1.0) AS score "
            "FROM kb_sections WHERE kb_sections MATCH ? ORDER BY score DESC, path ASC, ordinal ASC",
            (match_query,),
        ).fetchall()

    best: dict[str, tuple[int, float]] = {}
    for path, ordinal, score in rows:
        if path not in best:
            best[path] = (int(ordinal), float(score))
    ranked = sorted(best.items(), key=lambda item: (-item[1][1], item[0]))[:limit]
    return [
        _as_hit(
            by_path[path],
            score=score,
            matched_section=section_lookup[(path, ordinal)].heading,
            matched_text=section_lookup[(path, ordinal)].text[:320],
        )
        for path, (ordinal, score) in ranked
    ]


def search_fts_sections_reranked(
    docs: tuple[SearchDocument, ...],
    query: str,
    *,
    platform: str | None,
    limit: int = 5,
) -> list[SearchHit]:
    if limit <= 0:
        return []
    candidates = search_fts_sections(
        docs,
        query,
        platform=platform,
        limit=len(docs),
    )
    if not candidates:
        return []

    features = _query_features(query)
    by_path = {doc.path: doc for doc in docs}
    raw_scores = [hit.score for hit in candidates]
    minimum = min(raw_scores)
    maximum = max(raw_scores)

    reranked: list[SearchHit] = []
    for hit in candidates:
        doc = by_path[hit.path]
        normalized_bm25 = (
            1.0
            if maximum == minimum
            else (hit.score - minimum) / (maximum - minimum)
        )
        searchable = _searchable_text(doc)
        term_coverage = (
            sum(term in searchable for term in features.terms) / len(features.terms)
            if features.terms
            else 0.0
        )
        doc_components = _document_components(doc)
        service_match = bool(features.services and doc.service in features.services)
        component_match = bool(features.components & doc_components)
        component_mismatch = bool(features.components and not component_match)
        score = (
            normalized_bm25
            + 1.5 * term_coverage
            + 2.0 * service_match
            + 3.0 * component_match
            - 2.0 * component_mismatch
        )
        reranked.append(
            _as_hit(
                doc,
                score=score,
                matched_section=hit.matched_section,
                matched_text=hit.matched_text,
            )
        )
    return sorted(reranked, key=lambda hit: (-hit.score, hit.path))[:limit]


def expand_related(
    docs: tuple[SearchDocument, ...],
    primary_path: str,
    *,
    limit: int = 2,
) -> list[SearchHit]:
    by_path = {doc.path: doc for doc in docs}
    primary = by_path.get(primary_path)
    if primary is None or limit <= 0:
        return []
    return [
        _as_hit(
            by_path[path],
            score=0.0,
            matched_section=None,
            matched_text="related",
        )
        for path in primary.related
        if (
            path in by_path
            and path != primary_path
            and by_path[path].platform in {primary.platform, "shared"}
        )
    ][:limit]


def score_case(
    *,
    expected_primary: tuple[str, ...],
    acceptable_support: tuple[str, ...],
    must_not_match: tuple[str, ...],
    ranked_paths: list[str],
    support_paths: list[str],
) -> CaseScore:
    expected = set(expected_primary)
    first_rank = next(
        (rank for rank, path in enumerate(ranked_paths, start=1) if path in expected),
        None,
    )
    negative_abstained = None if expected else not ranked_paths
    return CaseScore(
        reciprocal_rank=0.0 if first_rank is None else 1.0 / first_rank,
        recall_at_1=first_rank is not None and first_rank <= 1,
        recall_at_3=first_rank is not None and first_rank <= 3,
        recall_at_5=first_rank is not None and first_rank <= 5,
        support_hits=tuple(
            path for path in support_paths if path in set(acceptable_support)
        ),
        must_not_violations=tuple(
            path for path in ranked_paths if path in set(must_not_match)
        ),
        negative_abstained=negative_abstained,
    )


def _knowledge_fingerprint(knowledge_root: Path) -> str:
    root = knowledge_root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _classify_failure(
    case: EvalCase,
    docs: tuple[SearchDocument, ...],
    ranked_paths: list[str],
) -> str | None:
    if not case.expected_primary or any(
        path in case.expected_primary for path in ranked_paths[:5]
    ):
        return None

    by_path = {doc.path: doc for doc in docs}
    expected_docs = [by_path[path] for path in case.expected_primary if path in by_path]
    if not expected_docs:
        return "coverage_gap"
    if case.platform is not None and all(
        doc.platform not in {case.platform, "shared"} for doc in expected_docs
    ):
        return "filtering"

    terms = _query_terms(case.query)
    for doc in expected_docs:
        searchable = "\n".join(
            (
                doc.title,
                doc.problem,
                "\n".join(doc.symptoms),
                "\n".join(doc.alerts),
                _document_text(doc),
            )
        ).lower()
        if any(term in searchable for term in terms):
            return "ranking"
    return "semantic_gap"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summarize_scheme(name: str, runs: list[CaseRun]) -> SchemeReport:
    positive = [run for run in runs if run.score.negative_abstained is None]
    negatives = [run for run in runs if run.score.negative_abstained is not None]
    denominator = len(positive) or 1
    latencies = [run.latency_ms for run in runs]
    return SchemeReport(
        name=name,
        runs=tuple(runs),
        recall_at_1=sum(run.score.recall_at_1 for run in positive) / denominator,
        recall_at_3=sum(run.score.recall_at_3 for run in positive) / denominator,
        recall_at_5=sum(run.score.recall_at_5 for run in positive) / denominator,
        mrr=sum(run.score.reciprocal_rank for run in positive) / denominator,
        must_not_violation_count=sum(
            len(run.score.must_not_violations) for run in runs
        ),
        support_hit_count=sum(len(run.score.support_hits) for run in runs),
        negative_abstention_rate=(
            sum(bool(run.score.negative_abstained) for run in negatives) / len(negatives)
            if negatives
            else None
        ),
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
    )


def production_schemes(repo_root: Path) -> tuple[tuple[str, Any, bool], ...]:
    """Schemes backed by the shipped indexes, so the report measures what agents get.

    ``prod_recall_top5`` is ``search_kb_candidates``' Top 5; ``prod_context_required``
    is the investigation ContextPack ``required_reads`` order.  A scheme is omitted
    when its index has not been built.
    """
    from lib.kb.recall import search_recall_index
    from lib.kb.retrieval import RetrievalRequest, RetrievalService

    root = Path(repo_root).resolve()
    schemes: list[tuple[str, Any, bool]] = []

    def _hits(docs, paths, limit):
        by_path = {doc.path: doc for doc in docs}
        hits = []
        for rank, path in enumerate(paths[:limit]):
            doc = by_path.get(path)
            hits.append(SearchHit(
                path=path, platform=doc.platform if doc else "",
                service=doc.service if doc else "", doc_type=doc.doc_type if doc else "",
                score=float(limit - rank), matched_section=None, matched_text="",
            ))
        return tuple(hits)

    build_dir = resolve_data_root(root) / "build"
    recall_index = build_dir / "kb-recall.sqlite"
    if recall_index.is_file():
        def recall_top5(docs, query, *, platform, limit):
            hits = search_recall_index(recall_index, query, platform or "shared", limit=limit)
            return _hits(docs, [hit.path for hit in hits], limit)
        schemes.append(("prod_recall_top5", recall_top5, False))

    if (build_dir / "kb-retrieval.sqlite").is_file():
        service = RetrievalService(root)

        def context_required(docs, query, *, platform, limit):
            pack = service.build_context(RetrievalRequest(
                query=query, profile="investigation", platform=platform,
            )).to_dict()
            return _hits(docs, [str(item.get("doc_id") or "") for item in pack["required_reads"]], limit)
        schemes.append(("prod_context_required", context_required, False))
    return tuple(schemes)


def evaluate(
    corpus: Corpus,
    docs: tuple[SearchDocument, ...],
    knowledge_root: Path,
    extra_schemes: tuple[tuple[str, Any, bool], ...] = (),
) -> EvaluationReport:
    schemes = (
        ("current_filter", search_current_filter, False),
        ("fts_document", search_fts_documents, False),
        ("fts_section", search_fts_sections, False),
        ("fts_section_reranked", search_fts_sections_reranked, False),
        ("fts_section_links", search_fts_sections, True),
        *extra_schemes,
    )
    scheme_reports: list[SchemeReport] = []
    for name, retriever, include_links in schemes:
        runs: list[CaseRun] = []
        for case in corpus.cases:
            started = perf_counter()
            hits = retriever(docs, case.query, platform=case.platform, limit=5)
            related = (
                expand_related(docs, hits[0].path, limit=2)
                if include_links and hits
                else []
            )
            latency_ms = (perf_counter() - started) * 1000.0
            ranked_paths = [hit.path for hit in hits]
            related_paths = [hit.path for hit in related]
            runs.append(
                CaseRun(
                    case_id=case.case_id,
                    query=case.query,
                    platform=case.platform,
                    ranked_hits=tuple(hits),
                    related_hits=tuple(related),
                    score=score_case(
                        expected_primary=case.expected_primary,
                        acceptable_support=case.acceptable_support,
                        must_not_match=case.must_not_match,
                        ranked_paths=ranked_paths,
                        support_paths=related_paths,
                    ),
                    latency_ms=latency_ms,
                    failure_class=_classify_failure(case, docs, ranked_paths),
                )
            )
        scheme_reports.append(_summarize_scheme(name, runs))

    if corpus.preliminary:
        recommendation = (
            "本次仅为预评测，只能比较召回方案；样本量不足，"
            "不能判定召回框架达标，也不能批准正式开发或据此决定引入 embedding。"
        )
    else:
        recommendation = "评测集规模已满足正式门禁，请根据指标人工审核开发决策。"
    return EvaluationReport(
        version=1,
        preliminary=corpus.preliminary,
        case_count=len(corpus.cases),
        holdout_count=sum(case.split == "holdout" for case in corpus.cases),
        knowledge_fingerprint=_knowledge_fingerprint(knowledge_root),
        schemes=tuple(scheme_reports),
        recommendation=recommendation,
    )


def _hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    return {
        "path": hit.path,
        "platform": hit.platform,
        "service": hit.service,
        "doc_type": hit.doc_type,
        "score": hit.score,
        "matched_section": hit.matched_section,
        "matched_text": hit.matched_text,
    }


def report_to_dict(report: EvaluationReport) -> dict[str, Any]:
    return {
        "version": report.version,
        "preliminary": report.preliminary,
        "case_count": report.case_count,
        "holdout_count": report.holdout_count,
        "knowledge_fingerprint": report.knowledge_fingerprint,
        "recommendation": report.recommendation,
        "schemes": [
            {
                "name": scheme.name,
                "metrics": {
                    "recall_at_1": scheme.recall_at_1,
                    "recall_at_3": scheme.recall_at_3,
                    "recall_at_5": scheme.recall_at_5,
                    "mrr": scheme.mrr,
                    "must_not_violation_count": scheme.must_not_violation_count,
                    "support_hit_count": scheme.support_hit_count,
                    "negative_abstention_rate": scheme.negative_abstention_rate,
                    "latency_p50_ms": scheme.latency_p50_ms,
                    "latency_p95_ms": scheme.latency_p95_ms,
                },
                "cases": [
                    {
                        "case_id": run.case_id,
                        "query": run.query,
                        "platform": run.platform,
                        "ranked_hits": [_hit_to_dict(hit) for hit in run.ranked_hits],
                        "related_hits": [_hit_to_dict(hit) for hit in run.related_hits],
                        "score": {
                            "reciprocal_rank": run.score.reciprocal_rank,
                            "recall_at_1": run.score.recall_at_1,
                            "recall_at_3": run.score.recall_at_3,
                            "recall_at_5": run.score.recall_at_5,
                            "support_hits": list(run.score.support_hits),
                            "must_not_violations": list(
                                run.score.must_not_violations
                            ),
                            "negative_abstained": run.score.negative_abstained,
                        },
                        "latency_ms": run.latency_ms,
                        "failure_class": run.failure_class,
                    }
                    for run in scheme.runs
                ],
            }
            for scheme in report.schemes
        ],
    }


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: EvaluationReport) -> str:
    state = "预评测" if report.preliminary else "正式评测"
    lines = [
        f"# 知识库召回{state}报告",
        "",
        f"- 用例数: {report.case_count}",
        f"- holdout: {report.holdout_count}",
        f"- preliminary: `{str(report.preliminary).lower()}`",
        f"- 知识指纹: `{report.knowledge_fingerprint}`",
        "",
        "## 方案指标",
        "",
        "| 方案 | R@1 | R@3 | R@5 | MRR | must-not | support | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scheme in report.schemes:
        lines.append(
            f"| {scheme.name} | {scheme.recall_at_1:.3f} | "
            f"{scheme.recall_at_3:.3f} | {scheme.recall_at_5:.3f} | "
            f"{scheme.mrr:.3f} | {scheme.must_not_violation_count} | "
            f"{scheme.support_hit_count} | {scheme.latency_p50_ms:.3f} | "
            f"{scheme.latency_p95_ms:.3f} |"
        )

    for scheme in report.schemes:
        lines.extend(["", f"## {scheme.name}", ""])
        for run in scheme.runs:
            lines.extend(
                [
                    f"### {run.case_id}",
                    "",
                    f"> {run.query}",
                    "",
                    f"platform: `{run.platform}`; latency: `{run.latency_ms:.3f} ms`; "
                    f"failure: `{run.failure_class or 'none'}`",
                    "",
                    "| # | 文档 | 分数 | 命中章节 |",
                    "|---:|---|---:|---|",
                ]
            )
            for rank, hit in enumerate(run.ranked_hits, start=1):
                lines.append(
                    f"| {rank} | `{_md_cell(hit.path)}` | {hit.score:.6f} | "
                    f"{_md_cell(hit.matched_section or '-')} |"
                )
            if not run.ranked_hits:
                lines.append("| - | 无结果 | - | - |")
            lines.append("")
            lines.append(
                "- support hits: "
                + (", ".join(f"`{path}`" for path in run.score.support_hits) or "无")
            )
            lines.append(
                "- must-not violations: "
                + (
                    ", ".join(
                        f"`{path}`" for path in run.score.must_not_violations
                    )
                    or "无"
                )
            )
            if run.related_hits:
                lines.append(
                    "- related expansion: "
                    + ", ".join(f"`{hit.path}`" for hit in run.related_hits)
                )

    lines.extend(
        [
            "",
            "## 结论",
            "",
            report.recommendation,
            "",
            "Embedding 决策：当前样本不足，先根据未命中的失败分类补齐评测集，"
            "不仅凭本次结果引入向量召回。",
            "",
        ]
    )
    return "\n".join(lines)
