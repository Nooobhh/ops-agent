"""Build minimal, provenance-carrying ContextPacks from retrieval snapshots."""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from lib.data_root import resolve_data_root
from lib.envpack import active_env_pack, load_env_pack
from lib.graphs import graph_decls, graph_path
from lib.kb.bases import resolve_knowledge_root
from lib.kb.categories import doc_category
from lib.kb.frontmatter import SERVICES
from lib.kb.recall import (
    _GROUP_PENALTY,
    RecallIndexError,
    _query_terms,
    _rank_terms,
    search_recall_index_candidates,
)

from .contracts import CONTEXT_PACK_SCHEMA_VERSION, ContextPack, RetrievalRequest
from .profiles import RetrievalPolicy, policy_for
from .settings import VectorSettings, create_embedding_provider, create_rerank_provider, create_vector_store
from .task_deps import task_dependencies
from .task_route import assembly, extract_task_ids, resolve_task_route
from .reranker import RerankProvider
from .vector_store import VectorHit, VectorStore


class RetrievalSnapshotError(RuntimeError):
    """The retrieval snapshot is unavailable or not readable."""


# 开源组件的通用别名;环境专有服务的别名由环境包 knowledge.service_aliases 追加(见 _service_aliases)
_SERVICE_ALIASES = {
    "hdfs": ("hdfs",),
    "yarn": ("yarn",),
    "spark": ("spark", "spark2", "spark 2.3"),
    "hbase": ("hbase",),
    "hive": ("hive",),
    "mysql": ("mysql",),
}
_COMPONENT_ALIASES = {
    "datanode": ("datanode", "data node", "dn", "数据节点"),
    "namenode": ("namenode", "name node", "nn", "名称节点"),
    "nodemanager": ("nodemanager", "node manager", "nm"),
    "resourcemanager": ("resourcemanager", "resource manager", "rm"),
    "regionserver": ("regionserver", "region server", "rs"),
    "thriftserver": ("thriftserver", "thrift server"),
    "executor": ("executor",),
}
_COMPONENT_SERVICES = {
    "datanode": "hdfs",
    "namenode": "hdfs",
    "nodemanager": "yarn",
    "resourcemanager": "yarn",
    "regionserver": "hbase",
    "thriftserver": "spark",
    "executor": "spark",
}
_TOPOLOGY_RELATIONS = {
    "documents", "related_to", "component_of", "hosts_component", "depends_on",
    "uses_service", "uses_endpoint", "served_by", "handles_alert", "maps_to",
    "handled_by", "calls", "submits_to", "reads_from", "writes_to", "runs_on",
}
_SECTION_PRIORITY = {
    "适用现象": 0, "关键判据": 1, "机理": 2, "排查步骤": 3, "步骤": 3,
    "关键知识": 2, "关键配置": 0, "组件与节点": 0, "最新观测": 1,
    "摘要": 4, "正文": 5,
}
_CONTEXT_STOPWORDS = {
    "排查", "调查", "问题", "相关", "集群", "服务", "组件", "基础", "信息",
    "原因", "生成", "最近", "变化", "情况", "一个", "目前", "请", "查看",
}
# 分组库语境的按库检索策略(本库与机制文档原权重、任务运行时依赖服务低权重联查、其余更低)
# 由环境包 knowledge.assembly 声明,见 RetrievalService._group_context_factor
# 必读只收得分不低于最佳候选该比例的文档
_REQUIRED_READ_FLOOR = 0.5


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _final_score(item: Mapping[str, Any]) -> float:
    return float((item.get("scores") or {}).get("final") or 0.0)


def _short(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _context_stopwords() -> set[str]:
    # 知识库集群键(如平台名)在查询里不算内容词;同时是服务名的(mysql、redis)仍是内容词
    keys = {item.knowledge_key for item in active_env_pack().clusters if "knowledge" in item.roles}
    return _CONTEXT_STOPWORDS | (keys - set(SERVICES))


def _service_aliases() -> dict[str, tuple[str, ...]]:
    """通用别名 + 环境包声明的服务别名(服务键本身恒为别名之首)。"""
    aliases = dict(_SERVICE_ALIASES)
    for service, extra in active_env_pack().service_aliases.items():
        aliases[service] = (service, *extra)
    return aliases


def _entity_terms() -> set[str]:
    return set(_service_aliases()) | set(_COMPONENT_ALIASES)


def _meaningful_terms(query: str) -> tuple[str, ...]:
    return tuple(
        term for term in _rank_terms(query)
        if term.strip().lower() not in _context_stopwords() | _entity_terms()
        and len(term.strip()) >= 2
    )


def _contains(text: str, alias: str) -> bool:
    if alias.isascii():
        return bool(re.search(
            rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])",
            text.lower(),
        ))
    return alias.lower() in text.lower()


def _named_services(request: RetrievalRequest) -> set[str]:
    services = set()
    if request.service:
        services.add(request.service.lower())
    for service, aliases in _service_aliases().items():
        if any(_contains(request.query, alias) for alias in aliases):
            services.add(service)
    return services


def _normalize_profile(request: RetrievalRequest) -> tuple[RetrievalPolicy, bool]:
    fallback_requested = (
        str(request.retrieval_stage or "") == "alert_fallback"
        or (
            str(request.profile).lower() == "alert"
            and request.fallback_count > 0
            and str((request.execution_summary or {}).get("failure_reason") or "")
            in {"insufficient", "no_signal", "contradiction"}
        )
    )
    return policy_for("investigation" if fallback_requested else request.profile), fallback_requested


class RetrievalService:
    """Read retrieval snapshots and assemble bounded ContextPacks."""

    def __init__(
        self,
        repo_root: Path,
        *,
        retrieval_index_path: Optional[Path] = None,
        recall_index_path: Optional[Path] = None,
        vector_store: Optional[VectorStore] = None,
        embedding_provider=None,
        rerank_provider: Optional[RerankProvider] = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.data_root = resolve_data_root(self.repo_root)
        self.retrieval_index_path = Path(
            retrieval_index_path or self.data_root / "build" / "kb-retrieval.sqlite"
        ).resolve()
        self.recall_index_path = Path(
            recall_index_path or self.data_root / "build" / "kb-recall.sqlite"
        ).resolve()
        self.vector_store = vector_store if vector_store is not None else create_vector_store(self.repo_root)
        self.embedding_provider = (
            embedding_provider if embedding_provider is not None
            else create_embedding_provider(self.repo_root)
        )
        self.rerank_provider = (
            rerank_provider if rerank_provider is not None
            else create_rerank_provider(self.repo_root)
        )
        rerank_settings = VectorSettings.load(self.repo_root)
        self.rerank_config_backend = rerank_settings.rerank_backend
        configured_max_candidates = getattr(self.rerank_provider, "max_candidates", None)
        try:
            self.rerank_max_candidates = int(
                configured_max_candidates
                if configured_max_candidates is not None
                else rerank_settings.rerank_max_candidates
            )
        except (ValueError, TypeError):
            self.rerank_max_candidates = 20

    def _open(self) -> sqlite3.Connection:
        path = self.retrieval_index_path
        if not path.is_file():
            raise RetrievalSnapshotError("知识库检索快照不存在，请运行 scripts/build_wiki.py")
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            row = connection.execute(
                "SELECT value FROM retrieval_meta WHERE key = 'schema_version'"
            ).fetchone()
            if not row or int(row[0]) != 1:
                raise RetrievalSnapshotError("知识库检索快照版本不匹配，请运行 scripts/build_wiki.py")
            return connection
        except RetrievalSnapshotError:
            connection.close()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            try:
                connection.close()
            except UnboundLocalError:
                pass
            raise RetrievalSnapshotError("知识库检索快照不可读，请运行 scripts/build_wiki.py") from exc

    def _freshness(self, connection: sqlite3.Connection) -> dict[str, Any]:
        built_row = connection.execute(
            "SELECT value FROM retrieval_meta WHERE key = 'built_at'"
        ).fetchone()
        built_at = str(built_row[0] or "") if built_row else ""
        built_mtime = self.retrieval_index_path.stat().st_mtime
        sources: list[Path] = [
            load_env_pack(self.repo_root).config_file("service-baseline.yaml"),
            load_env_pack(self.repo_root).config_file("log-catalog.yaml"),
            self.data_root / "build" / "kb-topology.json",
            *(graph_path(self.data_root / "state", decl.id)
              for decl in graph_decls(load_env_pack(self.repo_root))),
        ]
        sources.extend(resolve_knowledge_root(self.repo_root).rglob("*.md"))
        newest = max(
            (path.stat().st_mtime for path in sources if path.is_file()),
            default=0.0,
        )
        stale = newest > built_mtime + 0.000001
        return {
            "snapshot_consistent": not stale,
            "snapshot_stale": stale,
            "snapshot_built_at": built_at,
            "snapshot_source_latest_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest))
                if newest else None
            ),
        }

    def build_context(self, request: RetrievalRequest) -> ContextPack:
        if not str(request.query or "").strip():
            raise ValueError("query 不能为空")
        policy, fallback_requested = _normalize_profile(request)
        fingerprint_source = request
        with closing(self._open()) as connection:
            inferred_platform = (
                None if request.platform
                else self._platform_from_services(connection, request)
            )
            if inferred_platform:
                request = replace(request, platform=inferred_platform)
            entities, anchors = self._resolve_entities(connection, request)
            if inferred_platform:
                for entity in entities:
                    if entity.get("kind") == "platform":
                        entity.update(match_type="inferred_from_service", confidence="medium")
            effective_service = request.service or self._inferred_service(entities)
            task_route = self._task_route(connection, request, effective_service)
            baseline = self._baseline(connection, request, effective_service, policy)
            topology, topology_docs, topology_scores = self._topology(
                connection, request, anchors, policy,
            )
            dependency_docs = self._expand_dependencies(
                connection, anchors, policy, topology, topology_docs, topology_scores,
            )
            lexical_hits = self._lexical(request, effective_service)
            vector_hits, vector_status = self._vectors(request, policy, effective_service)
            knowledge, rerank_status = self._knowledge(
                connection,
                request,
                effective_service,
                policy,
                lexical_hits,
                topology_docs,
                topology_scores,
                vector_hits,
                dependency_docs,
                group_context=self._in_group_context(effective_service, task_route),
            )
            gaps = self._gaps(
                request, effective_service, baseline, topology, knowledge,
            )
            required_reads = self._required_reads(baseline, knowledge, policy, effective_service)
            if task_route is not None:
                # 任务组装是确定性的固定必读,排在相关度排序结果之前;QA 只留作参考
                knowledge["task_route"] = task_route.required() + task_route.related
                topology["task_resolution"] = {
                    key: value for key, value in task_route.to_dict().items()
                    if key not in {"common", "general", "specific", "related", "gaps"}
                }
                gaps.extend(task_route.gaps)
                if policy.name != "qa":
                    fixed = task_route.required()
                    fixed_ids = {item["doc_id"] for item in fixed}
                    required_reads = fixed + [
                        item for item in required_reads if str(item.get("doc_id")) not in fixed_ids
                    ]
            self._annotate_categories(connection, [
                *required_reads,
                *(item for bucket in knowledge.values() if isinstance(bucket, list) for item in bucket),
            ])
            freshness = self._freshness(connection)
            meta = {
                "fingerprint": hashlib.sha256(
                    json.dumps(fingerprint_source.to_dict(), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "platform_inferred": bool(inferred_platform),
                "fts_used": bool(lexical_hits),
                "topology_used": bool(anchors),
                "vector_used": bool(vector_hits),
                "vector_backend": vector_status.get("backend"),
                "vector_status": vector_status.get("status"),
                "vector_reason": vector_status.get("reason"),
                "fallback_requested": fallback_requested,
                "retrieval_stage": "alert_fallback" if fallback_requested else request.retrieval_stage,
                "lexical_candidate_count": len(lexical_hits),
                "topology_candidate_count": len(topology_docs),
                "dependency_hops": policy.dependency_hops,
                "dependency_candidate_count": len(dependency_docs),
                "task_route_used": task_route is not None,
                "group_context": self._in_group_context(effective_service, task_route),
                "vector_candidate_count": len(vector_hits),
                "rerank_used": bool(rerank_status.get("used")),
                "rerank_backend": rerank_status.get("backend"),
                "rerank_model": rerank_status.get("model"),
                "rerank_status": rerank_status.get("status"),
                "rerank_reason": rerank_status.get("reason"),
                "rerank_candidate_count": rerank_status.get("candidate_count", 0),
                **freshness,
                "truncated": False,
                "reference_only": True,
                "content_embedded": False,
            }
            return ContextPack(
                schema_version=CONTEXT_PACK_SCHEMA_VERSION,
                retrieval_profile=policy.name,
                query=request.query,
                scope={
                    "platform": request.platform,
                    "scope": request.scope,
                    "service": effective_service,
                    "component": request.component or self._inferred_component(entities),
                    "node": request.node,
                    "artifact_kind": request.artifact_kind,
                },
                entities=entities,
                baseline_context=baseline,
                topology_context=topology,
                knowledge_context=knowledge,
                required_reads=required_reads,
                gaps=gaps,
                retrieval_meta=meta,
            )

    def task_route(
        self,
        *,
        process_id: Optional[int] = None,
        task_id: Optional[int] = None,
        task_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """分组库任务的确定性文档组装(通用 → 类型通用 → pid/tid 特化),供 CLI/人工排查直查。"""
        with closing(self._open()) as connection:
            route = resolve_task_route(
                connection, process_id=process_id, task_id=task_id, task_type=task_type,
            )
        payload = route.to_dict()
        payload["required_reads"] = route.required()
        return payload

    def task_dependencies(self, *, process_id: int, task_id: Optional[int] = None) -> dict[str, Any]:
        """调度任务读写的表、同表上下游任务与流程依赖(本地图谱)。"""
        with closing(self._open()) as connection:
            return task_dependencies(connection, process_id=process_id, task_id=task_id)

    def read_chunks(self, chunk_ids: Sequence[str], *, max_chars: int = 1200) -> list[dict[str, Any]]:
        """Read bounded chunk excerpts by stable ID; never reads arbitrary paths."""
        actual_limit = max(80, min(4000, int(max_chars)))
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        with closing(self._open()) as connection:
            rows = connection.execute(
                f"SELECT chunk_id, doc_id, ordinal, section, title, summary, text "
                f"FROM kb_chunks WHERE chunk_id IN ({placeholders})",
                list(dict.fromkeys(str(item) for item in chunk_ids)),
            ).fetchall()
        by_id = {str(row[0]): row for row in rows}
        remaining = actual_limit
        result: list[dict[str, Any]] = []
        for chunk_id in chunk_ids:
            if chunk_id not in by_id or remaining <= 0:
                continue
            limit = min(actual_limit, remaining)
            text = _short(by_id[chunk_id][6], limit)
            result.append({
                "chunk_id": chunk_id,
                "doc_id": str(by_id[chunk_id][1]),
                "ordinal": int(by_id[chunk_id][2]),
                "section": str(by_id[chunk_id][3]),
                "title": str(by_id[chunk_id][4]),
                "summary": _short(by_id[chunk_id][5]),
                "text": text,
            })
            remaining -= len(text)
        return result

    def _resolve_entities(
        self,
        connection: sqlite3.Connection,
        request: RetrievalRequest,
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        text = request.query
        entities: list[dict[str, Any]] = []
        anchors: list[tuple[str, str]] = []

        platform = request.platform
        if platform:
            entities.append({"entity_id": f"platform:{platform}", "kind": "platform", "name": platform, "match_type": "request", "confidence": "high"})
        services = _named_services(request)
        components = set()
        if request.component:
            components.add(request.component.lower())
        for component, aliases in _COMPONENT_ALIASES.items():
            if any(_contains(text, alias) for alias in aliases):
                components.add(component)
        for component in sorted(components):
            entities.append({
                "entity_id": f"component:{platform or '*'}:{component}",
                "kind": "component", "name": component, "match_type": "request_or_alias",
                "confidence": "high" if request.component else "medium",
            })
        for service in sorted(services):
            entities.append({
                "entity_id": f"service:{platform or '*'}:{service}",
                "kind": "service", "name": service, "match_type": "request_or_alias",
                "confidence": "high" if request.service else "medium",
            })
        if request.alertname:
            entities.append({
                "entity_id": f"alert:{platform or '*'}:{request.alertname}",
                "kind": "alert", "name": request.alertname, "match_type": "request", "confidence": "high",
            })
        node_names = set()
        if request.node:
            node_names.add(request.node.lower())
        node_names.update(item.lower() for item in re.findall(r"\bnode\d+(?:\.\w+)*\b", text, re.IGNORECASE))

        process_ids, task_ids = extract_task_ids(text)
        for pid in sorted(process_ids):
            entities.append({
                "entity_id": f"process:{platform or '*'}:{pid}", "kind": "process", "name": pid,
                "match_type": "request", "confidence": "high",
            })
            if platform:
                anchors.extend(self._existing_anchor_ids(connection, f"process:{platform}:{pid}"))
        for tid in sorted(task_ids):
            entities.append({
                "entity_id": f"task:{platform or '*'}:{tid}", "kind": "task", "name": tid,
                "match_type": "request", "confidence": "high",
            })
            if platform and process_ids:
                for pid in sorted(process_ids):
                    anchors.extend(self._existing_anchor_ids(connection, f"task:{platform}:{pid}:{tid}"))
            elif platform:
                anchors.extend(self._find_task_anchors(connection, platform, tid))

        for service in sorted(services):
            if platform:
                anchors.extend(self._existing_anchor_ids(connection, f"service:{platform}:{service}"))
                # Shared topology documents are valid support context for a
                # platform-specific request (for example the shared MySQL
                # cluster used by HD applications).
                anchors.extend(self._existing_anchor_ids(connection, f"service:shared:{service}"))
        for component in sorted(components):
            anchors.extend(self._find_named_anchors(connection, "component", component, platform))
        for node in sorted(node_names):
            anchors.extend(self._find_named_anchors(connection, "node", node, platform))
        if request.alertname:
            anchors.extend(self._find_named_anchors(connection, "alert", request.alertname, platform))

        anchors = list(dict.fromkeys(anchors))
        for graph_id, node_id in anchors:
            row = connection.execute(
                "SELECT kind, name, platform FROM topology_nodes WHERE graph_id = ? AND node_id = ?",
                (graph_id, node_id),
            ).fetchone()
            if row:
                entities.append({
                    "entity_id": node_id, "kind": str(row[0]), "name": str(row[1]),
                    "platform": str(row[2]), "match_type": "topology_anchor", "confidence": "observed",
                })
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for entity in entities:
            unique[(str(entity.get("kind")), str(entity.get("name")).lower())] = entity
        return list(unique.values()), anchors

    @staticmethod
    def _task_route(
        connection: sqlite3.Connection,
        request: RetrievalRequest,
        service: Optional[str],
    ):
        """查询点名分组库的流程/任务号时组装固定文档;只有任务号或流程号时须明确是该语境(点名声明的服务),
        仅凭成对 pid/tid 时须在图谱快照中命中,避免把 Spark task 等编号误认作调度任务。"""
        decl = assembly()
        if decl is None:
            return None
        process_ids, task_ids = extract_task_ids(request.query)
        if not process_ids and not task_ids:
            return None
        named = service == decl.service or decl.service in str(request.alertname or "").lower()
        pid = int(process_ids[0]) if process_ids else None
        tid = int(task_ids[0]) if task_ids else None
        if not named and (pid is None or tid is None):
            return None
        route = resolve_task_route(connection, process_id=pid, task_id=tid)
        if request.platform and route.platform and request.platform != route.platform:
            return None
        if not named and route.topology_node is None:
            return None
        if len(process_ids) > 1 or len(task_ids) > 1:
            route.gaps.append(f"查询含多个流程/任务号，仅按首个组装 {decl.label} 文档")
        return route

    @staticmethod
    def _existing_anchor_ids(connection: sqlite3.Connection, node_id: str) -> list[tuple[str, str]]:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT graph_id, node_id FROM topology_nodes WHERE node_id = ?", (node_id,)
            ).fetchall()
        ]

    @staticmethod
    def _find_named_anchors(
        connection: sqlite3.Connection,
        kind: str,
        name: str,
        platform: Optional[str],
    ) -> list[tuple[str, str]]:
        clauses = ["kind = ?", "lower(name) = lower(?)"]
        params: list[Any] = [kind, name]
        if platform:
            clauses.append("(platform = ? OR platform = '' OR platform IS NULL)")
            params.append(platform)
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT graph_id, node_id FROM topology_nodes WHERE " + " AND ".join(clauses), params,
            ).fetchall()
        ]

    @staticmethod
    def _find_task_anchors(
        connection: sqlite3.Connection,
        platform: str,
        task_id: str,
    ) -> list[tuple[str, str]]:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT graph_id, node_id FROM topology_nodes "
                "WHERE kind = 'task' AND platform = ? AND node_id LIKE ?",
                (platform, f"task:{platform}:%:{task_id}"),
            ).fetchall()
        ]

    @staticmethod
    def _platform_from_services(
        connection: sqlite3.Connection,
        request: RetrievalRequest,
    ) -> Optional[str]:
        """点名服务只在一个管理面集群的拓扑里登记时(如某调度服务只部署在一个集群)反推平台。

        各服务的候选平台取交集;多个集群都有的服务(HDFS 等)不约束,也不猜。
        """
        pack = active_env_pack()
        managed = [pack.knowledge_key(key) for key in pack.cluster_keys("managed")]
        if not managed:
            return None
        candidates: Optional[set[str]] = None
        for service in _named_services(request):
            node_ids = [f"service:{key}:{service}" for key in managed]
            platforms = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT platform FROM topology_nodes "
                    f"WHERE kind = 'service' AND node_id IN ({','.join('?' for _ in node_ids)})",
                    node_ids,
                ).fetchall()
            }
            if platforms:
                candidates = platforms if candidates is None else candidates & platforms
        if candidates is not None and len(candidates) == 1:
            return next(iter(candidates))
        return None

    @staticmethod
    def _inferred_service(entities: Iterable[Mapping[str, Any]]) -> Optional[str]:
        for entity in entities:
            if entity.get("kind") == "service":
                return str(entity.get("name") or "") or None
        for entity in entities:
            if entity.get("kind") == "component":
                service = _COMPONENT_SERVICES.get(str(entity.get("name") or "").lower())
                if service:
                    return service
        return None

    @staticmethod
    def _inferred_component(entities: Iterable[Mapping[str, Any]]) -> Optional[str]:
        for entity in entities:
            if entity.get("kind") == "component":
                return str(entity.get("name") or "") or None
        return None

    def _baseline(
        self,
        connection: sqlite3.Connection,
        request: RetrievalRequest,
        service: Optional[str],
        policy: RetrievalPolicy,
    ) -> dict[str, Any]:
        platform = request.platform
        if not policy.include_baseline:
            return {"sources": [], "topology_sources": [], "facts": [], "config_refs": [], "log_locations": []}
        clauses = ["kind IN ('baseline_document', 'config_ref', 'log_location')"]
        params: list[Any] = []
        if platform:
            clauses.append("(platform = ? OR platform = '*' OR platform = '')")
            params.append(platform)
        if service:
            clauses.append("(service = ? OR service = '')")
            params.append(service)
        rows = connection.execute(
            "SELECT fact_id, platform, service, component, kind, key, value_json, source_doc_id, "
            "source_chunk_id, source_ref, observed_at, confidence FROM baseline_facts WHERE "
            + " AND ".join(clauses) + " ORDER BY kind, key, fact_id LIMIT 200",
            params,
        ).fetchall()
        facts: list[dict[str, Any]] = []
        config_refs: list[dict[str, Any]] = []
        log_locations: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        topology_sources: list[dict[str, Any]] = []
        for row in rows:
            value = _json(row[6], {})
            item = {
                "fact_id": str(row[0]), "kind": str(row[4]), "key": str(row[5]),
                "value": value, "source_doc_id": str(row[7] or ""),
                "source_chunk_id": str(row[8] or ""), "source": str(row[9] or ""),
                "observed_at": str(row[10] or ""), "confidence": str(row[11] or ""),
            }
            facts.append(item)
            if row[4] == "config_ref":
                config_refs.append(item)
            elif row[4] == "log_location":
                log_locations.append(item)
            if row[4] == "baseline_document" and row[7]:
                sources.append({
                    "ref_type": "document",
                    "doc_id": str(row[7]),
                    "chunk_ids": [str(row[8])] if row[8] else [],
                })

        if service and platform:
            baseline_docs = connection.execute(
                "SELECT doc_id, title, summary, observed_at, confidence FROM kb_documents "
                "WHERE profile = 'baseline' AND service = ? AND (platform = ? OR platform = 'shared') "
                "ORDER BY platform, doc_id LIMIT 5",
                (service, platform),
            ).fetchall()
            for row in baseline_docs:
                doc_id = str(row[0])
                chunk_ids = self._select_chunk_ids(connection, [doc_id], request.query, limit=4, baseline=True)
                sources.append({
                    "ref_type": "document",
                    "doc_id": doc_id,
                    "title": str(row[1]),
                    "chunk_ids": chunk_ids,
                    "observed_at": str(row[3] or ""),
                    "confidence": str(row[4] or ""),
                })
        if service and platform and policy.name in {"qa", "artifact"}:
            topology_rows = connection.execute(
                "SELECT doc_id, title, summary, observed_at, confidence FROM kb_documents "
                "WHERE profile = 'topology' AND (platform = ? OR platform = 'shared') "
                "AND (lower(title) LIKE ? OR lower(summary) LIKE ?) "
                "ORDER BY platform, doc_id LIMIT 5",
                (platform, f"%{service.lower()}%", f"%{service.lower()}%"),
            ).fetchall()
            for row in topology_rows:
                doc_id = str(row[0])
                topology_sources.append({
                    "ref_type": "document",
                    "doc_id": doc_id,
                    "title": str(row[1]),
                    "chunk_ids": self._select_chunk_ids(connection, [doc_id], request.query, limit=3),
                    "observed_at": str(row[3] or ""),
                    "confidence": str(row[4] or ""),
                })
        unique_sources = {item["doc_id"]: item for item in sources if item.get("doc_id")}
        unique_topology_sources = {item["doc_id"]: item for item in topology_sources if item.get("doc_id")}
        return {
            "sources": list(unique_sources.values()),
            "topology_sources": list(unique_topology_sources.values()),
            "facts": facts[:80],
            "config_refs": config_refs[:60],
            "log_locations": log_locations[:60],
        }

    def _topology(
        self,
        connection: sqlite3.Connection,
        request: RetrievalRequest,
        anchors: list[tuple[str, str]],
        policy: RetrievalPolicy,
    ) -> tuple[dict[str, Any], set[str], dict[str, float]]:
        metas = connection.execute(
            "SELECT graph_id, schema_version, snapshot_id, generated_at, source_fingerprint, status, warnings_json "
            "FROM topology_meta ORDER BY graph_id"
        ).fetchall()
        snapshot = []
        warnings: list[str] = []
        for row in metas:
            item = {
                "graph_id": str(row[0]), "schema_version": int(row[1]),
                "snapshot_id": str(row[2]), "generated_at": str(row[3] or ""),
                "source_fingerprint": str(row[4] or ""), "status": str(row[5]),
            }
            item["warnings"] = _json(row[6], [])
            snapshot.append(item)
            warnings.extend(str(value) for value in item["warnings"])
        paths: list[dict[str, Any]] = []
        topology_docs: set[str] = set()
        scores: dict[str, float] = {}
        node_clauses = ["kind = ?"]
        node_params: list[Any] = ["node"]
        if request.platform:
            node_clauses.append("(platform = ? OR platform = '' OR platform IS NULL)")
            node_params.append(request.platform)
        node_rows = connection.execute(
            "SELECT node_id, name, platform, properties_json FROM topology_nodes WHERE "
            + " AND ".join(node_clauses)
            + " ORDER BY name LIMIT 100",
            node_params,
        ).fetchall()
        candidate_nodes = [
            {
                "id": str(row[0]),
                "name": str(row[1]),
                "platform": str(row[2]),
                "properties": _json(row[3], {}),
            }
            for row in node_rows
        ]
        node_scoped = request.scope == "node" and bool(request.node)
        if node_scoped:
            candidate_nodes = [
                item for item in candidate_nodes
                if item["name"] == request.node
            ]
        if request.node and not any(item["name"] == request.node for item in candidate_nodes):
            candidate_nodes.insert(0, {
                "id": f"node:{request.platform or '*'}:{request.node}",
                "name": request.node,
                "platform": request.platform or "",
                "properties": {},
                "match_type": "request_only",
            })
        seen_paths: set[tuple[str, str, str, str]] = set()
        anchor_limit = 5 if node_scoped else 120
        for graph_id, node_id in anchors[:anchor_limit]:
            node = connection.execute(
                "SELECT kind, name, platform, properties_json, provenance_json FROM topology_nodes "
                "WHERE graph_id = ? AND node_id = ?", (graph_id, node_id),
            ).fetchone()
            if not node:
                continue
            paths.append({
                "nodes": [{
                    "id": node_id, "kind": str(node[0]), "name": str(node[1]),
                    "platform": str(node[2]), "properties": _json(node[3], {}),
                }],
                "relations": [], "depth": 0, "graph_id": graph_id,
            })
            edge_rows = connection.execute(
                "SELECT edge_id, source, target, relation, properties_json, provenance_json "
                "FROM topology_edges WHERE graph_id = ? AND (source = ? OR target = ?) "
                "AND relation IN (" + ",".join("?" for _ in _TOPOLOGY_RELATIONS) + ") "
                "ORDER BY relation, edge_id LIMIT 80",
                [graph_id, node_id, node_id, *_TOPOLOGY_RELATIONS],
            ).fetchall()
            for edge in edge_rows:
                other = str(edge[2]) if str(edge[1]) == node_id else str(edge[1])
                marker = (graph_id, node_id, other, str(edge[3]))
                if marker in seen_paths:
                    continue
                seen_paths.add(marker)
                other_row = connection.execute(
                    "SELECT kind, name, platform, properties_json FROM topology_nodes WHERE graph_id = ? AND node_id = ?",
                    (graph_id, other),
                ).fetchone()
                if not other_row:
                    continue
                path_item = {
                    "nodes": [
                        {"id": node_id, "kind": str(node[0]), "name": str(node[1]), "platform": str(node[2])},
                        {"id": other, "kind": str(other_row[0]), "name": str(other_row[1]), "platform": str(other_row[2])},
                    ],
                    "relations": [str(edge[3])], "depth": 1, "graph_id": graph_id,
                    "evidence": {"properties": _json(edge[4], {}), "provenance": _json(edge[5], [])},
                }
                paths.append(path_item)
            doc_rows = connection.execute(
                "SELECT doc_id, relation, depth, evidence_json FROM kb_doc_topology "
                "WHERE graph_id = ? AND node_id = ? ORDER BY depth, relation, doc_id LIMIT 100",
                (graph_id, node_id),
            ).fetchall()
            for row in doc_rows:
                doc_id = str(row[0])
                topology_docs.add(doc_id)
                scores[doc_id] = scores.get(doc_id, 0.0) + (1.0 if int(row[2]) == 0 else 0.6)
            # A component/node is normally connected to a service, while the
            # document edge points from the document to that service.  Project
            # this typed two-step path into document candidates without
            # flattening the whole graph into free-form tags.
            for edge in edge_rows:
                relation = str(edge[3])
                if relation not in {"component_of", "hosts_component", "maps_to", "submits_to"}:
                    continue
                other = str(edge[2]) if str(edge[1]) == node_id else str(edge[1])
                related_rows = connection.execute(
                    "SELECT doc_id, relation, depth, evidence_json FROM kb_doc_topology "
                    "WHERE graph_id = ? AND node_id = ? LIMIT 100",
                    (graph_id, other),
                ).fetchall()
                for row in related_rows:
                    doc_id = str(row[0])
                    topology_docs.add(doc_id)
                    scores[doc_id] = max(scores.get(doc_id, 0.0), 0.45)
        return {
            "snapshot": snapshot[:8] if node_scoped else snapshot,
            "anchors": [node_id for _graph_id, node_id in anchors[:anchor_limit]],
            "execution_node": request.node,
            "candidate_nodes": candidate_nodes[:1] if node_scoped else candidate_nodes,
            "paths": paths[:5] if node_scoped else paths[:120],
            "warnings": sorted(set(warnings))[:8 if node_scoped else 40],
            "warning_count": len(set(warnings)),
        }, topology_docs, scores

    _DEPENDENCY_SCORES = {1: 0.3, 2: 0.15}
    _DEPENDENCY_DOCS_PER_SERVICE = 20

    def _expand_dependencies(
        self,
        connection: sqlite3.Connection,
        anchors: list[tuple[str, str]],
        policy: RetrievalPolicy,
        topology: dict[str, Any],
        topology_docs: set[str],
        topology_scores: dict[str, float],
    ) -> frozenset[str]:
        """从锚点服务(含锚点组件所属服务)沿 depends_on 向上游走 N 跳,带回上游的故障类文档。

        上游文档只作弱拓扑信号:分数低于组件投影,且须过查询词过滤;扩展路径写入
        topology_context.dependency_paths 供审计。返回仅经依赖扩展进入的文档集合。
        """
        if policy.dependency_hops <= 0:
            return frozenset()
        starts: set[tuple[str, str]] = set()
        for graph_id, node_id in anchors:
            if node_id.startswith("service:"):
                starts.add((graph_id, node_id))
            elif node_id.startswith("component:"):
                for (service_id,) in connection.execute(
                    "SELECT target FROM topology_edges WHERE graph_id = ? AND source = ? "
                    "AND relation = 'component_of'", (graph_id, node_id),
                ):
                    starts.add((graph_id, str(service_id)))
        seen = set(starts)
        frontier = sorted(starts)
        paths: list[dict[str, Any]] = []
        added: set[str] = set()
        for hop in range(1, policy.dependency_hops + 1):
            reached: list[tuple[str, str, str]] = []
            for graph_id, service_id in frontier:
                for (target,) in connection.execute(
                    "SELECT target FROM topology_edges WHERE graph_id = ? AND source = ? "
                    "AND relation = 'depends_on' AND target LIKE 'service:%' ORDER BY target",
                    (graph_id, service_id),
                ):
                    if (graph_id, str(target)) in seen:
                        continue
                    seen.add((graph_id, str(target)))
                    reached.append((graph_id, service_id, str(target)))
            score = self._DEPENDENCY_SCORES.get(hop, 0.1)
            for graph_id, source_id, target_id in reached:
                paths.append({"graph_id": graph_id, "from": source_id, "to": target_id,
                              "relation": "depends_on", "hop": hop})
                rows = connection.execute(
                    "SELECT k.doc_id FROM kb_doc_topology k JOIN kb_documents d ON d.doc_id = k.doc_id "
                    "WHERE k.graph_id = ? AND k.node_id = ? AND k.relation = 'documents' "
                    "AND (d.doc_type = 'troubleshoot' OR d.profile = 'diagnosis') "
                    "ORDER BY k.doc_id LIMIT ?",
                    (graph_id, target_id, self._DEPENDENCY_DOCS_PER_SERVICE),
                ).fetchall()
                for (doc_id,) in rows:
                    doc_id = str(doc_id)
                    if doc_id not in topology_scores:
                        added.add(doc_id)
                    topology_docs.add(doc_id)
                    topology_scores[doc_id] = max(topology_scores.get(doc_id, 0.0), score)
            frontier = sorted((graph_id, target_id) for graph_id, _source, target_id in reached)
            if not frontier:
                break
        topology["dependency_paths"] = paths[:40]
        return frozenset(added)

    def _lexical(self, request: RetrievalRequest, service: Optional[str]) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "doc_id": hit.path, "score": float(hit.score), "snippet": _short(hit.snippet),
                    "platform": hit.platform, "service": hit.service, "doc_type": hit.doc_type,
                    "status": hit.status,
                }
                for hit in search_recall_index_candidates(
                    self.recall_index_path,
                    request.query,
                    request.platform,
                    request.service,
                    limit=50,
                )
            ]
        except (RecallIndexError, sqlite3.Error) as exc:
            raise RetrievalSnapshotError(str(exc)) from exc

    def _vectors(
        self,
        request: RetrievalRequest,
        policy: RetrievalPolicy,
        effective_service: Optional[str],
    ) -> tuple[list[VectorHit], dict[str, Any]]:
        if not policy.use_vector:
            return [], {"backend": None, "status": "disabled", "reason": "profile"}
        if self.vector_store is None:
            return [], {"backend": None, "status": "unavailable", "reason": "not_configured"}
        if request.query_vector is None:
            if self.embedding_provider is not None:
                try:
                    request_vector = self.embedding_provider.embed([request.query])[0]
                except Exception:
                    return [], {
                        "backend": self.vector_store.health().get("backend"),
                        "status": "error", "reason": "embedding_error",
                    }
            else:
                request_vector = None
            if request_vector is not None:
                return self._search_vectors(request_vector, request, policy, effective_service)
            health = self.vector_store.health()
            return [], {"backend": health.get("backend"), "status": "not_queried", "reason": "query_vector_missing"}
        return self._search_vectors(request.query_vector, request, policy, effective_service)

    def _search_vectors(
        self,
        query_vector: Sequence[float],
        request: RetrievalRequest,
        policy: RetrievalPolicy,
        effective_service: Optional[str],
    ) -> tuple[list[VectorHit], dict[str, Any]]:
        platforms = [request.platform, "shared"] if request.platform else [None]
        services: list[Optional[str]] = [effective_service]
        # Artifact reports often target a concrete service while the most
        # useful topology document is intentionally service=topology (for
        # example a shared MySQL cluster document).  Query both typed scopes;
        # do not remove the service filter and fall back to the whole platform.
        if policy.name == "artifact" and effective_service:
            services.append("topology")
        scopes = [
            {key: value for key, value in (("platform", platform), ("service", service)) if value}
            for platform in platforms for service in services
        ]
        if not scopes:
            scopes = [{}]
        try:
            health = self.vector_store.health()
            results: list[VectorHit] = []
            errors = 0
            for filters in scopes:
                try:
                    results.extend(self.vector_store.search(query_vector, filters, top_k=50))
                except Exception:
                    errors += 1
            unique = {item.record_id: item for item in results}
            if not unique and errors == len(scopes):
                return [], {"backend": health.get("backend"), "status": "error", "reason": "backend_error"}
            return list(unique.values()), {"backend": health.get("backend"), "status": "ok", "reason": None}
        except Exception:
            health = self.vector_store.health()
            return [], {"backend": health.get("backend"), "status": "error", "reason": "backend_error"}

    def _knowledge(
        self,
        connection: sqlite3.Connection,
        request: RetrievalRequest,
        service: Optional[str],
        policy: RetrievalPolicy,
        lexical_hits: list[dict[str, Any]],
        topology_docs: set[str],
        topology_scores: Mapping[str, float],
        vector_hits: list[VectorHit],
        dependency_docs: frozenset[str] = frozenset(),
        group_context: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        lexical = {str(item["doc_id"]): item for item in lexical_hits}
        vector_by_doc: dict[str, float] = {}
        vector_chunks: dict[str, list[str]] = {}
        for hit in vector_hits:
            if service and policy.name == "artifact":
                topology_doc = connection.execute(
                    "SELECT profile, title, summary FROM kb_documents WHERE doc_id = ?",
                    (hit.doc_id,),
                ).fetchone()
                if topology_doc and str(topology_doc[0]) == "topology":
                    topology_text = f"{topology_doc[1]} {topology_doc[2]}".lower()
                    if service.lower() not in topology_text:
                        continue
            vector_by_doc[hit.doc_id] = max(vector_by_doc.get(hit.doc_id, -1.0), float(hit.score))
            vector_chunks.setdefault(hit.doc_id, []).append(hit.chunk_id)
        doc_ids = set(lexical) | set(topology_docs) | set(vector_by_doc)
        if request.alertname:
            rows = connection.execute(
                "SELECT doc_id FROM kb_documents WHERE alerts_json LIKE ? AND (platform = ? OR platform = 'shared')",
                (f"%{request.alertname}%", request.platform or ""),
            ).fetchall()
            doc_ids.update(str(row[0]) for row in rows)
        if service and request.service:
            service_rows = connection.execute(
                "SELECT doc_id FROM kb_documents WHERE service = ? AND (platform = ? OR platform = 'shared') LIMIT 100",
                (service, request.platform or ""),
            ).fetchall()
            doc_ids.update(str(row[0]) for row in service_rows)
        if service and policy.name in {"qa", "artifact"}:
            # A topology document often uses service=topology because it
            # describes several concrete services.  Keep it eligible when its
            # title/summary explicitly names the requested service.
            topology_rows = connection.execute(
                "SELECT doc_id FROM kb_documents WHERE profile = 'topology' "
                "AND (platform = ? OR platform = 'shared') "
                "AND (lower(title) LIKE ? OR lower(summary) LIKE ?) LIMIT 50",
                (request.platform or "", f"%{service.lower()}%", f"%{service.lower()}%"),
            ).fetchall()
            for row in topology_rows:
                doc_id = str(row[0])
                doc_ids.add(doc_id)
                if isinstance(topology_scores, dict):
                    topology_scores.setdefault(doc_id, 0.35)
        if not doc_ids:
            return (
                {"diagnosis": [], "knowledge": [], "procedures": [], "alert_route": []},
                {"backend": None, "status": "not_run", "reason": "no_candidates", "used": False, "candidate_count": 0},
            )

        placeholders = ",".join("?" for _ in doc_ids)
        rows = connection.execute(
            "SELECT doc_id, platform, service, doc_type, profile, knowledge_layer, title, problem, summary, "
            "status, confidence, observed_at, symptoms_json, alerts_json FROM kb_documents WHERE doc_id IN (" + placeholders + ")",
            list(doc_ids),
        ).fetchall()
        group_tags: dict[str, dict[str, str]] = {}
        for tag_doc, tag in connection.execute(
            "SELECT doc_id, tag FROM kb_doc_tags WHERE doc_id IN (" + placeholders + ") "
            "AND (tag LIKE 'group_role:%' OR tag LIKE 'task_type:%' OR tag LIKE 'process_id:%')",
            list(doc_ids),
        ):
            key, _sep, value = str(tag).partition(":")
            group_tags.setdefault(str(tag_doc), {})[key] = value
        scores = [float(item["score"]) for item in lexical.values()]
        vectors = list(vector_by_doc.values())
        lex_min, lex_max = (min(scores), max(scores)) if scores else (0.0, 0.0)
        vec_min, vec_max = (min(vectors), max(vectors)) if vectors else (0.0, 0.0)
        buckets: dict[str, list[dict[str, Any]]] = {"diagnosis": [], "knowledge": [], "procedures": [], "alert_route": []}
        candidates: list[dict[str, Any]] = []
        for row in rows:
            doc_id = str(row[0])
            alert_match = bool(
                request.alertname and request.alertname in _json(row[13], [])
            )
            has_signal = (
                doc_id in lexical or doc_id in topology_scores or doc_id in vector_by_doc
            )
            if not has_signal and not alert_match:
                continue
            # 仅经依赖扩展而来的上游文档是弱信号:须与查询内容沾边才保留,不能凭拓扑灌入
            weak_topology = doc_id in dependency_docs
            if not alert_match and doc_id not in vector_by_doc and (
                weak_topology or doc_id not in topology_scores
            ):
                document_text = " ".join([
                    str(row[6] or ""), str(row[7] or ""), str(row[8] or ""),
                    " ".join(_json(row[12], [])), " ".join(_json(row[13], [])),
                ])
                if not any(term.lower() in document_text.lower() for term in _meaningful_terms(request.query)):
                    continue
            lexical_score = self._normalize(float(lexical.get(doc_id, {}).get("score", 0.0)), lex_min, lex_max)
            vector_score = self._normalize(vector_by_doc.get(doc_id, 0.0), vec_min, vec_max) if vector_by_doc else None
            # Topology scores already encode direct vs projected paths.  Do not
            # normalize by the maximum candidate score: when every candidate is
            # reached through the same service, max-normalization would make an
            # unrelated service document look like a direct component match.
            topology_score = min(1.0, max(0.0, topology_scores.get(doc_id, 0.0)))
            components = [(lexical_score, 0.65), (topology_score, 0.25)]
            if vector_score is not None:
                components.append((vector_score, 0.10))
            total_weight = sum(weight for _value, weight in components) or 1.0
            final_score = sum(value * weight for value, weight in components) / total_weight
            if str(row[10]) == "verified":
                final_score += 0.02
            if alert_match:
                final_score += 0.25
            group_factor = self._group_factor(group_tags.get(doc_id, {}), request.query)
            if group_context:
                group_factor *= self._group_context_factor(doc_id, str(row[2]))
            final_score *= group_factor
            doc_type, profile = str(row[3]), str(row[4])
            if doc_type == "troubleshoot" or profile == "diagnosis":
                bucket = "diagnosis"
            elif doc_type == "procedure":
                bucket = "procedures"
            elif profile == "baseline":
                bucket = "knowledge"
            else:
                bucket = "knowledge"
            if alert_match:
                alert_bucket = "alert_route"
            else:
                alert_bucket = None
            if bucket == "procedures" and not alert_match:
                # Generic words in a long procedure (for example “集群” or
                # “服务”) must not make an unrelated operational SOP appear
                # relevant.  Procedure recall is driven by its structured
                # metadata/title; baseline log/config references remain
                # available separately.
                metadata_text = " ".join([
                    str(row[6] or ""), str(row[7] or ""), str(row[8] or ""),
                    " ".join(_json(row[12], [])), " ".join(_json(row[13], [])),
                ])
                if not any(term.lower() in metadata_text.lower() for term in _meaningful_terms(request.query)):
                    continue
            if (bucket == "diagnosis" and not policy.include_diagnosis) or (
                bucket == "procedures" and not policy.include_procedures
            ) or (bucket == "knowledge" and not policy.include_knowledge):
                if alert_bucket != "alert_route":
                    continue
            selected_chunks = self._select_chunk_ids(
                connection, [doc_id], request.query, limit=2,
            )
            selected_chunks = list(dict.fromkeys([*vector_chunks.get(doc_id, []), *selected_chunks]))[:3]
            candidates.append({
                "doc_id": doc_id,
                "chunk_ids": selected_chunks,
                "row": row,
                "doc_type": doc_type,
                "profile": profile,
                "status": str(row[9]),
                "confidence": str(row[10]),
                "title": str(row[6]),
                "retrieval_reasons": self._reasons(
                    doc_id, lexical, topology_scores, vector_by_doc, dependency_docs,
                ),
                "lexical_score": lexical_score,
                "topology_score": topology_score,
                "vector_score": vector_score,
                "preliminary_score": final_score,
                "bucket": bucket,
                "alert_bucket": alert_bucket,
                "alert_match": alert_match,
                "group_factor": group_factor,
            })

        rerank_status = {
            "backend": getattr(self.rerank_provider, "backend", None),
            "model": getattr(self.rerank_provider, "model_id", None),
            "status": "disabled" if not policy.use_rerank or self.rerank_config_backend == "disabled" else "not_configured",
            "reason": "profile" if not policy.use_rerank else (
                "settings" if self.rerank_config_backend == "disabled" else "provider_missing"
            ),
            "used": False,
            "candidate_count": 0,
        }
        rerank_by_doc: dict[str, float] = {}
        if policy.use_rerank and self.rerank_provider is not None and candidates:
            rerank_candidates = sorted(
                candidates,
                key=lambda item: (-float(item["preliminary_score"]), str(item["doc_id"])),
            )[: max(1, self.rerank_max_candidates)]
            pairs = [
                self._rerank_text(connection, item["doc_id"], item["chunk_ids"])
                for item in rerank_candidates
            ]
            rerank_status["candidate_count"] = len(pairs)
            rerank_status["backend"] = getattr(self.rerank_provider, "backend", None)
            rerank_status["model"] = getattr(self.rerank_provider, "model_id", None)
            try:
                rerank_scores = self.rerank_provider.rerank(request.query, pairs)
                if len(rerank_scores) != len(rerank_candidates):
                    raise ValueError("rerank 返回数量不一致")
                rerank_by_doc = {
                    str(item["doc_id"]): float(score)
                    for item, score in zip(rerank_candidates, rerank_scores)
                }
                rerank_status.update({"status": "ok", "reason": None, "used": True})
            except Exception:
                # Rerank is an optimization layer.  A model/runtime error must
                # leave the deterministic FTS + topology + vector result
                # usable and visible to the downstream Agent.
                rerank_status.update({"status": "error", "reason": "provider_error"})
        elif policy.use_rerank and self.rerank_provider is not None:
            rerank_status["reason"] = "no_candidates"

        rerank_values = list(rerank_by_doc.values())
        rerank_min, rerank_max = (
            (min(rerank_values), max(rerank_values)) if rerank_values else (0.0, 0.0)
        )
        for item in candidates:
            doc_id = str(item["doc_id"])
            rerank_score = (
                self._normalize(rerank_by_doc[doc_id], rerank_min, rerank_max)
                if doc_id in rerank_by_doc else None
            )
            if rerank_score is None:
                final_score = float(item["preliminary_score"])
            else:
                components = [
                    (rerank_score, 0.72),
                    (float(item["lexical_score"]), 0.16),
                    (float(item["topology_score"]), 0.10),
                ]
                if item["vector_score"] is not None:
                    components.append((float(item["vector_score"]), 0.02))
                total_weight = sum(weight for _value, weight in components) or 1.0
                final_score = sum(value * weight for value, weight in components) / total_weight
                if item["confidence"] == "verified":
                    final_score += 0.02
                if item["alert_match"]:
                    final_score += 0.25
                final_score *= item["group_factor"]
                item["retrieval_reasons"] = [*item["retrieval_reasons"], "cross_encoder_match"]
            ref = {
                "ref_type": "document",
                "doc_id": doc_id,
                "chunk_ids": item["chunk_ids"],
                "doc_type": item["doc_type"],
                "profile": item["profile"],
                "status": item["status"],
                "confidence": item["confidence"],
                "title": item["title"],
                "retrieval_reasons": item["retrieval_reasons"],
                "scores": {
                    "lexical": round(float(item["lexical_score"]), 6),
                    "topology": round(float(item["topology_score"]), 6),
                    "vector": None if item["vector_score"] is None else round(float(item["vector_score"]), 6),
                    "rerank": None if rerank_score is None else round(rerank_score, 6),
                    "final": round(final_score, 6),
                },
                "provenance": [
                    {"source": "knowledge", "doc_id": doc_id, "chunk_id": chunk_id}
                    for chunk_id in item["chunk_ids"]
                ],
            }
            buckets[item["bucket"]].append(ref)
            if item["alert_bucket"]:
                buckets[item["alert_bucket"]].append(dict(ref))
        for bucket in buckets:
            unique = {item["doc_id"]: item for item in buckets[bucket]}
            buckets[bucket] = sorted(
                unique.values(), key=lambda item: (-item["scores"]["final"], item["doc_id"])
            )[: policy.max_documents_per_bucket]
        return buckets, rerank_status

    @staticmethod
    def _annotate_categories(connection: sqlite3.Connection, refs: list[dict[str, Any]]) -> None:
        """给文档引用补 5 类类别(操作手册另带 sop_kind),口径与知识页同源(lib.kb.categories)。"""
        doc_ids = sorted({str(ref.get("doc_id") or "") for ref in refs if isinstance(ref, dict)} - {""})
        if not doc_ids:
            return
        placeholders = ",".join("?" for _ in doc_ids)
        meta = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                f"SELECT doc_id, doc_type, profile FROM kb_documents WHERE doc_id IN ({placeholders})", doc_ids,
            )
        }
        kinds = {
            str(row[0]): str(row[1]).split(":", 1)[1]
            for row in connection.execute(
                f"SELECT doc_id, tag FROM kb_doc_tags WHERE doc_id IN ({placeholders}) AND tag LIKE 'sop_kind:%'",
                doc_ids,
            )
        }
        for ref in refs:
            doc_id = str(ref.get("doc_id") or "") if isinstance(ref, dict) else ""
            if doc_id not in meta:
                continue
            ref["category"] = doc_category(*meta[doc_id])
            if doc_id in kinds:
                ref["sop_kind"] = kinds[doc_id]

    @staticmethod
    def _in_group_context(service: Optional[str], task_route) -> bool:
        decl = assembly()
        return decl is not None and (service == decl.service or task_route is not None)

    @staticmethod
    def _group_context_factor(doc_id: str, service: str) -> float:
        decl = assembly()
        if doc_id.startswith(f"{decl.library}/") or service in decl.home_services:
            return 1.0
        return decl.runtime_weight if service in decl.runtime_services else decl.other_weight

    @staticmethod
    def _group_factor(tags: Mapping[str, str], query: str) -> float:
        """与召回同规则:分组库类型目录的文档,查询未点名其类型、也不含其流程号时降权。"""
        role = tags.get("group_role")
        if role in (None, "common"):
            return 1.0
        if role == "specific":     # 特化文档只认流程号:点名类型但流程号不符仍降权
            pid = tags.get("process_id")
            matched = pid and re.search(rf"(?<!\d){re.escape(pid)}(?!\d)", query)
            return 1.0 if matched else 1.0 - _GROUP_PENALTY
        if str(tags.get("task_type") or "").lower() in query.lower():
            return 1.0
        return 1.0 - _GROUP_PENALTY

    def _rerank_text(
        self,
        connection: sqlite3.Connection,
        doc_id: str,
        chunk_ids: Sequence[str],
    ) -> str:
        """Create a bounded model-only representation of a candidate doc."""
        selected = list(dict.fromkeys(str(item) for item in chunk_ids))[:2]
        chunks: list[tuple[str, str, str, str, str]] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            rows = connection.execute(
                "SELECT chunk_id, section, title, summary, text FROM kb_chunks "
                "WHERE chunk_id IN (" + placeholders + ")",
                selected,
            ).fetchall()
            by_id = {str(row[0]): row for row in rows}
            chunks = [by_id[item] for item in selected if item in by_id]
        if not chunks:
            row = connection.execute(
                "SELECT title, summary FROM kb_documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            return _short(f"{row[0] if row else doc_id} {row[1] if row else ''}", 1800)
        pieces = [
            _short(
                f"{row[2]}\n{row[1]}\n{row[3]}\n{row[4]}",
                1800,
            )
            for row in chunks
        ]
        return _short("\n\n".join(pieces), 3000)

    @staticmethod
    def _normalize(value: float, low: float, high: float) -> float:
        if high <= low:
            return 1.0 if value > 0 else 0.0
        return max(0.0, min(1.0, (value - low) / (high - low)))

    def _select_chunk_ids(
        self,
        connection: sqlite3.Connection,
        doc_ids: Sequence[str],
        query: str,
        *,
        limit: int,
        baseline: bool = False,
    ) -> list[str]:
        placeholders = ",".join("?" for _ in doc_ids)
        rows = connection.execute(
            "SELECT chunk_id, section, summary, text FROM kb_chunks WHERE doc_id IN (" + placeholders + ")",
            list(doc_ids),
        ).fetchall()
        terms = _query_terms(query)
        ranked = []
        for row in rows:
            content = f"{row[1]} {row[2]} {row[3]}".lower()
            overlap = sum(1 for term in terms if term.lower() in content)
            priority = _SECTION_PRIORITY.get(str(row[1]), 6 if not baseline else 10)
            ranked.append((-overlap, priority, str(row[0])))
        return [item[2] for item in sorted(ranked)[:max(0, int(limit))]]

    def _key_facts(self, connection: sqlite3.Connection, doc_id: str, query: str) -> list[str]:
        row = connection.execute(
            "SELECT summary FROM kb_documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        facts = [_short(row[0])] if row and row[0] else []
        terms = _query_terms(query)
        chunks = connection.execute(
            "SELECT section, text FROM kb_chunks WHERE doc_id = ? ORDER BY ordinal LIMIT 20", (doc_id,)
        ).fetchall()
        for section, text in chunks:
            for line in str(text or "").splitlines():
                clean = " ".join(line.split())
                if clean and any(term.lower() in clean.lower() for term in terms):
                    fact = _short(f"{section}: {clean}")
                    if fact not in facts:
                        facts.append(fact)
                    break
            if len(facts) >= 2:
                break
        return facts[:2]

    @staticmethod
    def _reasons(
        doc_id: str,
        lexical: Mapping[str, Mapping[str, Any]],
        topology_scores: Mapping[str, float],
        vector_by_doc: Mapping[str, float],
        dependency_docs: frozenset[str] = frozenset(),
    ) -> list[str]:
        reasons = []
        if doc_id in lexical:
            reasons.append("lexical_match")
        if doc_id in dependency_docs:
            reasons.append("upstream_dependency")
        elif doc_id in topology_scores:
            reasons.append("topology_path")
        if doc_id in vector_by_doc:
            reasons.append("semantic_match")
        return reasons

    @staticmethod
    def _required_reads(
        baseline: Mapping[str, Any],
        knowledge: Mapping[str, Any],
        policy: RetrievalPolicy,
        service: Optional[str],
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        # 未确定服务时 baseline sources 是全平台各服务的基线(hd 41 篇),只作参考
        # 留在 baseline_context;列进必读会让 Agent 把无关基线全读一遍。
        for source in (baseline.get("sources") or []) if service else []:
            if source.get("doc_id"):
                sources.append(dict(source))
        if policy.name in {"qa", "artifact"}:
            sources.extend(
                dict(source)
                for source in (baseline.get("topology_sources") or [])
                if source.get("doc_id")
            )
        ranked = [
            dict(item)
            for item in (knowledge.get("alert_route") or [])[:3]
            if item.get("doc_id")
        ]
        if policy.include_diagnosis:
            ranked.extend(
                dict(item) for item in (knowledge.get("diagnosis") or [])[:3]
            )
        if policy.include_procedures:
            ranked.extend(
                dict(item) for item in (knowledge.get("procedures") or [])[:2]
            )
        ranked.extend(
            dict(item)
            for item in (knowledge.get("knowledge") or [])[: policy.required_knowledge]
        )
        # 各桶仍按配额取，但按相关度跨桶合并并排在基线之前：最贴近症状的文档
        # 先读；远低于最佳候选的尾部只留在 knowledge_context 作参考。
        ranked.sort(key=lambda item: -_final_score(item))
        floor = _final_score(ranked[0]) * _REQUIRED_READ_FLOOR if ranked else 0.0
        values = [item for item in ranked if _final_score(item) >= floor] + sources
        unique: dict[str, dict[str, Any]] = {}
        for item in values:
            doc_id = str(item.get("doc_id") or item.get("path") or "")
            if doc_id and doc_id not in unique:
                unique[doc_id] = item
        return list(unique.values())

    @staticmethod
    def _gaps(
        request: RetrievalRequest,
        service: Optional[str],
        baseline: Mapping[str, Any],
        topology: Mapping[str, Any],
        knowledge: Mapping[str, Any],
    ) -> list[str]:
        gaps: list[str] = []
        if not request.platform:
            gaps.append("未确定平台")
        if not service and request.profile in {"investigation", "artifact", "alert"}:
            gaps.append("未确定目标服务")
        if request.profile in {"investigation", "artifact"} and not (
            baseline.get("sources") or baseline.get("topology_sources")
        ):
            gaps.append("未找到目标服务 baseline 文档")
        if not topology.get("anchors") and request.profile != "qa":
            gaps.append("未解析到拓扑实体")
        if request.profile in {"investigation", "artifact"} and not any(knowledge.get(bucket) for bucket in ("diagnosis", "knowledge", "procedures")):
            gaps.append("未找到相关知识、故障或流程文档")
        if (
            request.profile == "artifact"
            and request.artifact_kind == "report"
            and not baseline.get("config_refs")
            and not baseline.get("log_locations")
        ):
            gaps.append("未找到目标服务的配置、日志或监控数据源登记；实时数据需由后续消费者采集")
        if (
            request.profile == "artifact"
            and request.artifact_kind == "report"
            and not knowledge.get("diagnosis")
        ):
            gaps.append("未找到报告主题对应的历史分析文档；实时变化和原因需要后续消费者采集数据后分析")
        return gaps


def build_context(
    repo_root: Path,
    request: RetrievalRequest,
    *,
    vector_store: Optional[VectorStore] = None,
) -> ContextPack:
    return RetrievalService(repo_root, vector_store=vector_store).build_context(request)
