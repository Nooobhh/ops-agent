"""Local document-level FTS index for manual knowledge recall."""
from __future__ import annotations

import math
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from lib.kb.bases import discover_bases, iter_documents
from lib.kb.frontmatter import _FM_RE, load_doc


SCHEMA_VERSION = 3
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
_REBUILD_HINT = "请运行 scripts/build_wiki.py 重建知识库索引"
_SEARCH_COLUMNS = ("title", "problem", "symptoms", "alerts", "body")
# 排序专用:通用动词/虚词不区分文档。只作用于排序分词,不改 _query_terms
# (浏览检索与 ContextPack 的片段挑选仍沿用原口径)。
_RANK_STOPWORDS = _QUERY_STOPWORDS + (
    "排查", "检查", "查看", "情况", "原因", "是否", "需要", "哪些", "什么",
    "计划", "一次", "所有", "之间", "目前", "当前", "现在", "最近", "会有",
    "知道", "下面", "几点", "之前", "之后", "提前", "注意", "事项", "一下",
    "给出", "列出", "结合",
)
# title / problem / symptoms / alerts 的字段权重,body 为 1 且做长度归一
_FIELD_WEIGHTS = (4.0, 3.0, 2.0, 3.0)
_BM25_K1 = 1.2
_BM25_B = 0.75
_PLATFORM_BOOST = 0.25
_OFF_SCOPE_PENALTY = 0.5
# 分组库(按任务类型分组)的类型目录文档是给点名类型或 pid/tid 时确定性组装用的;
# 查询没点名该类型、也不含特化文档的流程号时降权,让通用目录的方法论排在前面。
_GROUP_PENALTY = 0.15
_SPECIFIC_PID_RE = re.compile(r"-p(\d+)-")
# shared 文档里不绑定具体服务的两类:通用流程(system)与跨平台拓扑(topology)
_GENERIC_SERVICES = frozenset({"system", "topology"})
# 查询未写服务名时,靠组件名判断是否点到了该服务
_SERVICE_COMPONENT_ALIASES = {
    "hdfs": ("datanode", "namenode", "journalnode", "dn", "nn"),
    "yarn": ("resourcemanager", "nodemanager", "rm", "nm"),
    "hbase": ("regionserver", "hmaster"),
}


class RecallIndexError(RuntimeError):
    """The generated recall index cannot be safely queried."""


@dataclass(frozen=True)
class RecallHit:
    path: str
    platform: str
    service: str
    doc_type: str
    playbook: str
    status: str
    score: float
    snippet: str


@dataclass(frozen=True)
class DocumentChunk:
    """Stable section unit for future vector indexes and section reranking."""
    chunk_id: str
    path: str
    section: str
    platform: str
    service: str
    doc_type: str
    knowledge_layer: str
    profile: str
    confidence: str
    observed_at: str
    entrypoints: tuple[str, ...]
    title: str
    summary: str
    text: str


_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")


def _body_without_frontmatter(path: Path) -> str:
    source = path.read_text(encoding="utf-8").lstrip("\ufeff")
    return _FM_RE.sub("", source, count=1).strip()


def iter_document_chunks(knowledge_root: Path):
    """Yield section chunks with stable IDs without changing the source files."""
    root = Path(knowledge_root).resolve()
    for item in iter_documents(root):
        path, relative = item.path, Path(item.doc_id)
        if path.name == "_index.md":
            continue
        parsed = load_doc(path)
        if parsed.type == "oplog":
            continue
        body = _body_without_frontmatter(path)
        lines = body.splitlines()
        sections: list[tuple[str, list[str]]] = []
        current = "正文"
        buffer: list[str] = []
        in_fence = False
        for line in lines:
            if _FENCE_RE.match(line):
                in_fence = not in_fence
            if not in_fence:
                match = _H2_RE.match(line)
                if match:
                    if buffer:
                        sections.append((current, buffer))
                    current = match.group(1).strip()
                    buffer = []
                    continue
            buffer.append(line)
        if buffer or not sections:
            sections.append((current, buffer))
        for index, (section, content) in enumerate(sections, start=1):
            text = "\n".join(content).strip()
            if not text:
                continue
            yield DocumentChunk(
                chunk_id=f"{relative.as_posix()}#section-{index:02d}",
                path=relative.as_posix(),
                section=section,
                platform=str(parsed.platform or ""),
                service=str(parsed.service or ""),
                doc_type=str(parsed.type or ""),
                knowledge_layer=str(parsed.knowledge_layer or ""),
                profile=str(parsed.profile or ""),
                confidence=str(parsed.confidence or ""),
                observed_at=str(parsed.observed_at or ""),
                entrypoints=tuple(parsed.entrypoints),
                title=str(parsed.title or relative.stem),
                summary=str(parsed.summary or ""),
                text=text,
            )


def _iter_documents(knowledge_root: Path):
    root = knowledge_root.resolve()
    for item in iter_documents(root):
        path, relative = item.path, Path(item.doc_id)
        if path.name == "_index.md":
            continue
        parsed = load_doc(path)
        if parsed.type == "oplog":
            continue
        source = path.read_text(encoding="utf-8").lstrip("\ufeff")
        body = _FM_RE.sub("", source, count=1).strip()
        indexed_metadata = "\n".join(
            value for value in (
                parsed.raw.get("summary"),
                parsed.raw.get("knowledge_layer"),
                parsed.raw.get("profile"),
                " ".join(str(item) for item in parsed.raw.get("entrypoints") or []),
            )
            if value
        )
        if indexed_metadata:
            body = f"{indexed_metadata}\n{body}"
        yield (
            relative.as_posix(),
            str(parsed.platform or ""),
            str(parsed.service or ""),
            str(parsed.type or ""),
            str(parsed.playbook or ""),
            str(parsed.status or ""),
            str(parsed.title or relative.stem),
            str(parsed.problem or ""),
            "\n".join(parsed.symptoms),
            "\n".join(parsed.alerts),
            body,
        )


def _create_index(connection: sqlite3.Connection, knowledge_root: Path) -> None:
    connection.execute("CREATE TABLE kb_meta(schema_version INTEGER NOT NULL)")
    connection.execute("INSERT INTO kb_meta VALUES (?)", (SCHEMA_VERSION,))
    connection.execute("CREATE TABLE kb_groups(base TEXT PRIMARY KEY, common_dir TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO kb_groups VALUES (?, ?)",
        ((base.id, base.common_dir or "") for base in discover_bases(knowledge_root) if base.group_field),
    )
    connection.execute(
        "CREATE VIRTUAL TABLE kb_docs USING fts5("
        "path UNINDEXED, platform UNINDEXED, service UNINDEXED, "
        "doc_type UNINDEXED, playbook UNINDEXED, status UNINDEXED, "
        "title, problem, symptoms, alerts, body, tokenize='trigram')"
    )
    connection.execute(
        "CREATE TABLE kb_chunks("
        "chunk_id TEXT PRIMARY KEY, path TEXT NOT NULL, section TEXT NOT NULL, "
        "platform TEXT NOT NULL, service TEXT NOT NULL, doc_type TEXT NOT NULL, "
        "knowledge_layer TEXT NOT NULL, profile TEXT NOT NULL, confidence TEXT NOT NULL, "
        "observed_at TEXT NOT NULL, entrypoints TEXT NOT NULL, title TEXT NOT NULL, "
        "summary TEXT NOT NULL, text TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO kb_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _iter_documents(knowledge_root),
    )
    connection.executemany(
        "INSERT INTO kb_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                chunk.chunk_id, chunk.path, chunk.section, chunk.platform,
                chunk.service, chunk.doc_type, chunk.knowledge_layer,
                chunk.profile, chunk.confidence, chunk.observed_at,
                "\n".join(chunk.entrypoints), chunk.title, chunk.summary, chunk.text,
            )
            for chunk in iter_document_chunks(knowledge_root)
        ),
    )


def build_recall_index(knowledge_root: Path, index_path: Path) -> Path:
    """Build beside the destination and atomically replace the prior index."""
    destination = Path(index_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temp_path = Path(raw_temp_path)
    try:
        with closing(sqlite3.connect(temp_path)) as connection:
            with connection:
                _create_index(connection, Path(knowledge_root))
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
        for suffix in ("-journal", "-shm", "-wal"):
            Path(f"{temp_path}{suffix}").unlink(missing_ok=True)
    return destination


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


def _match_query(query: str) -> str:
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"'
        for term in _query_terms(query)
        if len(term) >= 3
    )


def _short_query_terms(query: str) -> tuple[str, ...]:
    return tuple(term for term in _query_terms(query) if len(term) < 3)


def _literal_match_clause(terms: tuple[str, ...]) -> str:
    field_match = " OR ".join(f"lower({column}) LIKE ?" for column in _SEARCH_COLUMNS)
    return " OR ".join(f"({field_match})" for _ in terms)


def _literal_match_parameters(terms: tuple[str, ...]) -> list[str]:
    return [f"%{term}%" for term in terms for _ in _SEARCH_COLUMNS]


def _literal_match_count(terms: tuple[str, ...], fields: tuple[object, ...]) -> int:
    text = "\n".join(str(field or "") for field in fields).lower()
    return sum(
        bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
                text,
            )
            if term.isascii()
            else term in text
        )
        for term in terms
    )


def _open_readonly(index_path: Path) -> sqlite3.Connection:
    path = Path(index_path).resolve()
    if not path.is_file():
        raise RecallIndexError(f"知识库召回索引不存在；{_REBUILD_HINT}")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT schema_version FROM kb_meta"
        ).fetchone()
        if row != (SCHEMA_VERSION,):
            raise RecallIndexError(f"知识库召回索引版本不匹配；{_REBUILD_HINT}")
        connection.execute("SELECT rowid FROM kb_docs LIMIT 0").fetchall()
        connection.execute("SELECT chunk_id FROM kb_chunks LIMIT 0").fetchall()
        connection.execute("SELECT base FROM kb_groups LIMIT 0").fetchall()
        return connection
    except RecallIndexError:
        connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        try:
            connection.close()
        except UnboundLocalError:
            pass
        raise RecallIndexError(f"知识库召回索引不可读；{_REBUILD_HINT}") from exc


def _rank_terms(query: str) -> tuple[str, ...]:
    """Ranking terms: ASCII words (plus ``_``-separated parts) and CJK bigrams.

    Chinese ops vocabulary is mostly two-character words (内存、重启、空转), which
    the trigram FTS index cannot see; bigrams of each CJK run keep them, and
    the spurious cross-word bigrams rarely occur in documents.
    """
    cleaned = query.lower()
    for stopword in _RANK_STOPWORDS:
        cleaned = cleaned.replace(stopword, " ")
    terms: list[str] = []
    for part in _QUERY_PART_RE.findall(cleaned):
        if "\u3400" <= part[0] <= "\u9fff":
            terms.extend(part[index : index + 2] for index in range(len(part) - 1))
        elif len(part) >= 2:
            terms.append(part)
            if "_" in part:
                terms.extend(piece for piece in part.split("_") if len(piece) >= 2)
    return tuple(dict.fromkeys(terms))


def _term_counter(term: str):
    if not term.isascii():
        return lambda text: text.count(term)
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
    return lambda text: len(pattern.findall(text)) if term in text else 0


def _snippet(body: str, terms: tuple[str, ...], width: int = 48) -> str:
    text = " ".join(body.split())
    lowered = text.lower()
    position = next((lowered.find(term) for term in terms if term in lowered), 0)
    start = max(0, position - width // 2)
    return text[start : start + width * 2].strip()


def _rank_documents(
    rows: list[tuple[object, ...]],
    terms: tuple[str, ...],
) -> list[tuple[float, tuple[object, ...]]]:
    """Score ``rows`` with field-weighted BM25 (BM25F-lite).

    ``rows`` end with lower-cased title, problem, symptoms, alerts and body.
    Metadata fields are short and curated, so a hit there outweighs body
    mentions; only the body is length-normalised.
    """
    documents = [tuple(str(value or "").lower() for value in row[-5:]) for row in rows]
    if not documents:
        return []
    counters = [(term, _term_counter(term)) for term in terms]
    counts = [
        [tuple(counter(field) for field in fields) for _term, counter in counters]
        for fields in documents
    ]
    total = len(documents)
    idf = [
        math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
        for frequency in (
            sum(any(per_term[index]) for per_term in counts)
            for index in range(len(counters))
        )
    ]
    average_body = sum(len(fields[-1]) for fields in documents) / total or 1.0
    ranked: list[tuple[float, tuple[object, ...]]] = []
    for row, fields, per_term in zip(rows, documents, counts):
        body_norm = 1.0 - _BM25_B + _BM25_B * len(fields[-1]) / average_body
        score = 0.0
        for weight, term_counts in zip(idf, per_term):
            weighted = sum(
                field_weight * count
                for field_weight, count in zip(_FIELD_WEIGHTS, term_counts[:-1])
            ) + term_counts[-1] / body_norm
            if weighted:
                score += weight * weighted * (_BM25_K1 + 1.0) / (weighted + _BM25_K1)
        if score > 0:
            ranked.append((score, row))
    return sorted(ranked, key=lambda item: (-item[0], str(item[1][0])))


def _names_service(query: str, service: str) -> bool:
    lowered = query.lower()
    if service in lowered:
        return True
    return any(
        re.search(rf"(?<![a-z0-9]){alias}(?![a-z0-9])", lowered)
        for alias in _SERVICE_COMPONENT_ALIASES.get(service, ())
    )


def _scope_factor(row: tuple[object, ...], platform: str | None, query: str) -> float:
    """Prefer documents scoped to the requested platform.

    A shared document bound to one concrete service (the MySQL cluster SOP,
    the Redis deploy SOPs) is off-scope for an HD/IoT request that never names
    that service, even when its generic operational wording matches.
    """
    doc_platform, doc_service = str(row[1]), str(row[2])
    if platform in (None, "shared"):
        return 1.0
    if doc_platform == platform:
        return 1.0 + _PLATFORM_BOOST
    if doc_service and doc_service not in _GENERIC_SERVICES and not _names_service(query, doc_service):
        return 1.0 - _OFF_SCOPE_PENALTY
    return 1.0


def _group_factor(path: str, groups: dict[str, str], query: str) -> float:
    """分组库类型目录下的文档,查询未点名该目录名、也不含其流程号(p<pid>-)时降权。"""
    parts = path.split("/")
    if len(parts) < 3 or parts[0] not in groups or parts[1] == groups[parts[0]]:
        return 1.0
    specific = _SPECIFIC_PID_RE.search(parts[-1])
    if specific:               # 特化文档只认流程号:点名类型但流程号不符仍降权
        matched = re.search(rf"(?<!\d){specific.group(1)}(?!\d)", query)
        return 1.0 if matched else 1.0 - _GROUP_PENALTY
    if parts[1].lower() in query.lower():
        return 1.0
    return 1.0 - _GROUP_PENALTY


def search_recall_index_candidates(
    index_path: Path,
    query: str,
    platform: str | None,
    service: str | None = None,
    *,
    limit: int = 50,
) -> tuple[RecallHit, ...]:
    """Return a bounded ranked candidate pool without mutating the index.

    Existing callers use the compatibility wrapper below, which preserves the
    historical Top 5 contract.  Context construction needs a larger pool so
    topology and profile rules can rescue documents ranked below the first five
    lexical matches.  ``platform=None`` searches every platform; only the
    platform-less ContextPack uses it, the Top 5 wrapper always scopes.
    """
    terms = _rank_terms(query)
    actual_limit = min(100, max(0, int(limit)))
    if not terms or actual_limit == 0:
        return ()

    filters = "1 = 1" if platform is None else "platform IN (?, 'shared')"
    filter_parameters: list[object] = [] if platform is None else [platform]
    if service is not None:
        filters += " AND service = ?"
        filter_parameters.append(service)
    sql = (
        "SELECT path, platform, service, doc_type, playbook, status, "
        f"{', '.join(_SEARCH_COLUMNS)} FROM kb_docs WHERE {filters} ORDER BY path"
    )
    try:
        with closing(_open_readonly(index_path)) as connection:
            rows = connection.execute(sql, filter_parameters).fetchall()
            groups = dict(connection.execute("SELECT base, common_dir FROM kb_groups").fetchall())
    except RecallIndexError:
        raise
    except sqlite3.Error as exc:
        raise RecallIndexError(f"知识库召回索引不可读；{_REBUILD_HINT}") from exc

    return tuple(
        RecallHit(
            path=str(row[0]),
            platform=str(row[1]),
            service=str(row[2]),
            doc_type=str(row[3]),
            playbook=str(row[4]),
            status=str(row[5]),
            score=round(score, 6),
            snippet=_snippet(str(row[-1] or ""), terms),
        )
        for score, row in sorted(
            (
                (score * _scope_factor(row, platform, query) * _group_factor(str(row[0]), groups, query), row)
                for score, row in _rank_documents(rows, terms)
            ),
            key=lambda item: (-item[0], str(item[1][0])),
        )[:actual_limit]
    )


def search_recall_index(
    index_path: Path,
    query: str,
    platform: str,
    service: str | None = None,
    *,
    limit: int = 5,
) -> tuple[RecallHit, ...]:
    """Return ranked Top 5 candidates from an existing index."""
    return search_recall_index_candidates(
        index_path,
        query,
        platform,
        service,
        limit=min(5, max(0, int(limit))),
    )
