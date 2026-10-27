"""Build the read-only retrieval snapshot used by ContextPack construction."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from lib.data_root import resolve_data_root
from lib.kb.bases import (
    LEGACY_BASE, discover_bases, group_role, iter_documents, resolve_knowledge_root, split_doc_path,
)
from lib.kb.categories import doc_category, sop_kind_of
from lib.kb.frontmatter import FrontmatterError, load_doc
from lib.kb.recall import _FM_RE, iter_document_chunks
from lib.envpack import load_env_pack
from lib.graphs import graph_decls, graph_path, migrate_legacy


SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fingerprint(paths: Iterable[Path], payloads: Iterable[tuple[str, Any]] = ()) -> str:
    digest = hashlib.sha256()
    for path in sorted({Path(item).resolve() for item in paths}, key=str):
        if not path.is_file():
            continue
        digest.update(str(path).encode("utf-8"))
        digest.update(path.read_bytes())
    for key, value in sorted(payloads, key=lambda item: item[0]):
        digest.update(key.encode("utf-8"))
        digest.update(_json(value).encode("utf-8"))
    return digest.hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid"
    return (payload if isinstance(payload, dict) else None), "ok"


def _source_record(path: Path, kind: str) -> dict[str, Any]:
    return {"kind": kind, "path": str(path)}


def _component_tag(relative: Path, bases=None) -> str | None:
    """组件取自 platform/service/<component>/ 目录;flat 库没有组件层。"""
    base, inner = split_doc_path(relative, bases or (LEGACY_BASE,))
    if base is None or base.layout != "platform_service":
        return None
    if len(inner.parts) >= 4 and inner.parts[2] != "_index":
        value = str(inner.parts[2]).strip()
        if value:
            return value.lower()
    return None


def _topology_files(repo_root: Path) -> tuple[tuple[str, Path], ...]:
    """知识库拓扑 + 环境包声明的各图谱(graph_id 为 ``<图谱>_topology``)。"""
    data_root = resolve_data_root(repo_root)
    decls = graph_decls(load_env_pack(repo_root))
    for decl in decls:
        migrate_legacy(data_root / "state", decl)
    return (
        ("platform_topology", data_root / "build" / "kb-topology.json"),
        *((f"{decl.id}_topology", graph_path(data_root / "state", decl.id)) for decl in decls),
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE kb_documents("
        "doc_id TEXT PRIMARY KEY, platform TEXT NOT NULL, service TEXT NOT NULL, "
        "doc_type TEXT NOT NULL, profile TEXT NOT NULL, knowledge_layer TEXT NOT NULL, "
        "title TEXT NOT NULL, problem TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, "
        "confidence TEXT NOT NULL, observed_at TEXT NOT NULL, entrypoints_json TEXT NOT NULL, "
        "symptoms_json TEXT NOT NULL, alerts_json TEXT NOT NULL, playbooks_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE kb_chunks("
        "chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
        "section TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, text TEXT NOT NULL, "
        "platform TEXT NOT NULL, service TEXT NOT NULL, doc_type TEXT NOT NULL, profile TEXT NOT NULL, "
        "confidence TEXT NOT NULL, observed_at TEXT NOT NULL)"
    )
    connection.execute("CREATE INDEX kb_chunks_doc_idx ON kb_chunks(doc_id, ordinal)")
    connection.execute(
        "CREATE TABLE kb_doc_tags("
        "doc_id TEXT NOT NULL, tag TEXT NOT NULL, source TEXT NOT NULL, confidence TEXT NOT NULL, "
        "PRIMARY KEY(doc_id, tag))"
    )
    connection.execute(
        "CREATE TABLE topology_meta("
        "graph_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, snapshot_id TEXT NOT NULL, "
        "generated_at TEXT NOT NULL, source_fingerprint TEXT NOT NULL, status TEXT NOT NULL, "
        "warnings_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE topology_nodes("
        "graph_id TEXT NOT NULL, node_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, "
        "platform TEXT NOT NULL, properties_json TEXT NOT NULL, provenance_json TEXT NOT NULL, "
        "PRIMARY KEY(graph_id, node_id))"
    )
    connection.execute(
        "CREATE TABLE topology_edges("
        "graph_id TEXT NOT NULL, edge_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, "
        "relation TEXT NOT NULL, properties_json TEXT NOT NULL, provenance_json TEXT NOT NULL, "
        "PRIMARY KEY(graph_id, edge_id))"
    )
    connection.execute("CREATE INDEX topology_edges_source_idx ON topology_edges(graph_id, source, relation)")
    connection.execute("CREATE INDEX topology_edges_target_idx ON topology_edges(graph_id, target, relation)")
    connection.execute(
        "CREATE TABLE kb_doc_topology("
        "doc_id TEXT NOT NULL, graph_id TEXT NOT NULL, node_id TEXT NOT NULL, relation TEXT NOT NULL, "
        "depth INTEGER NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY(doc_id, graph_id, node_id, relation))"
    )
    connection.execute(
        "CREATE TABLE baseline_facts("
        "fact_id TEXT PRIMARY KEY, platform TEXT NOT NULL, service TEXT NOT NULL, component TEXT NOT NULL, "
        "kind TEXT NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL, source_doc_id TEXT NOT NULL, "
        "source_chunk_id TEXT NOT NULL, source_ref TEXT NOT NULL, observed_at TEXT NOT NULL, confidence TEXT NOT NULL)"
    )
    connection.execute("CREATE INDEX baseline_facts_scope_idx ON baseline_facts(platform, service, component, kind)")


def _insert_documents(connection: sqlite3.Connection, knowledge_root: Path) -> tuple[set[str], list[str]]:
    included: set[str] = set()
    warnings: list[str] = []
    bases = discover_bases(knowledge_root)
    for item in iter_documents(knowledge_root, bases):
        path, relative = item.path, Path(item.doc_id)
        if path.name == "_index.md":
            continue
        try:
            doc = load_doc(path)
        except FrontmatterError as exc:
            warnings.append(f"{relative.as_posix()}: {exc}")
            continue
        if doc.type == "oplog":
            continue
        doc_id = relative.as_posix()
        included.add(doc_id)
        connection.execute(
            "INSERT INTO kb_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id, str(doc.platform or ""), str(doc.service or ""), str(doc.type or ""),
                str(doc.profile or ""), str(doc.knowledge_layer or ""), str(doc.title or relative.stem),
                str(doc.problem or ""), str(doc.raw.get("summary") or ""), str(doc.status or ""),
                str(doc.confidence or ""), str(doc.observed_at or ""), _json(list(doc.entrypoints)),
                _json(list(doc.symptoms)), _json(list(doc.alerts)), _json(list(doc.playbooks)),
            ),
        )
        tags = [
            (f"scope:{doc.platform}", "frontmatter", "observed"),
            (f"service:{doc.service}", "frontmatter", "observed"),
            (f"type:{doc.type}", "frontmatter", "observed"),
            (f"profile:{doc.profile}", "frontmatter", "observed"),
            (f"layer:{doc.knowledge_layer}", "frontmatter", "observed"),
            (f"category:{doc_category(doc.type, doc.profile)}", "frontmatter", "observed"),
        ]
        sop_kind = sop_kind_of(doc.type, doc.sop_kind)
        if sop_kind:
            tags.append((f"sop_kind:{sop_kind}", "frontmatter", "observed"))
        component = _component_tag(relative, bases)
        if component:
            tags.append((f"component:{component}", "path", "observed"))
        tags.extend((f"alert:{item}", "frontmatter", "observed") for item in doc.alerts)
        tags.extend((f"entrypoint:{item}", "frontmatter", "observed") for item in doc.entrypoints)
        # 分组库(如按 task_type 分组的库)的确定性组装键,见 lib/kb/retrieval/task_route.py
        role = group_role(item.base, item.inner, str(doc.problem or ""), doc.process_id)
        if role:
            tags.append((f"group_role:{role}", "path", "observed"))
            group_value = getattr(doc, str(item.base.group_field), None)
            if group_value:
                tags.append((f"{item.base.group_field}:{group_value}", "frontmatter", "observed"))
            if doc.process_id:
                tags.append((f"process_id:{doc.process_id}", "frontmatter", "observed"))
            if doc.task_id:
                tags.append((f"task_id:{doc.task_id}", "frontmatter", "observed"))
        for tag, source, confidence in tags:
            if tag.strip(" :"):
                connection.execute(
                    "INSERT OR IGNORE INTO kb_doc_tags VALUES (?, ?, ?, ?)",
                    (doc_id, tag, source, confidence),
                )
    return included, warnings


def _insert_chunks(connection: sqlite3.Connection, knowledge_root: Path, included: set[str]) -> None:
    for chunk in iter_document_chunks(knowledge_root):
        if chunk.path not in included:
            continue
        connection.execute(
            "INSERT INTO kb_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id, chunk.path,
                int(chunk.chunk_id.rsplit("-", 1)[-1]), chunk.section,
                chunk.title, chunk.summary, chunk.text, chunk.platform, chunk.service,
                chunk.doc_type, chunk.profile, chunk.confidence, chunk.observed_at,
            ),
        )


def _insert_topology(connection: sqlite3.Connection, repo_root: Path, included: set[str]) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for graph_id, path in _topology_files(repo_root):
        payload, status = _load_json(path)
        if payload is None:
            connection.execute(
                "INSERT INTO topology_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                (graph_id, 1, f"{graph_id}:missing", "", "", status, _json([])),
            )
            continue
        nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
        edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
        fingerprint = str(payload.get("source_fingerprint") or hashlib.sha256(_json(payload).encode()).hexdigest())
        snapshot_id = f"{graph_id}:{fingerprint[:16]}"
        warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
        connection.execute(
            "INSERT INTO topology_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                graph_id, int(payload.get("schema_version") or 1), snapshot_id,
                str(payload.get("generated_at") or payload.get("completed_at") or ""),
                fingerprint, "ok", _json(warnings),
            ),
        )
        for item in nodes:
            if not isinstance(item, Mapping) or not str(item.get("id") or "").strip():
                continue
            connection.execute(
                "INSERT INTO topology_nodes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    graph_id, str(item["id"]), str(item.get("kind") or ""), str(item.get("name") or ""),
                    str(item.get("platform") or ""), _json(item.get("properties") or {}),
                    _json(item.get("provenance") or []),
                ),
            )
        for item in edges:
            if not isinstance(item, Mapping) or not str(item.get("id") or "").strip():
                continue
            connection.execute(
                "INSERT INTO topology_edges VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    graph_id, str(item["id"]), str(item.get("source") or ""),
                    str(item.get("target") or ""), str(item.get("relation") or ""),
                    _json(item.get("properties") or {}), _json(item.get("provenance") or []),
                ),
            )
        loaded.append({"graph_id": graph_id, "payload": payload, "path": path})

    for graph in loaded:
        graph_id = graph["graph_id"]
        for item in graph["payload"].get("nodes") or []:
            if not isinstance(item, Mapping):
                continue
            node_id = str(item.get("id") or "")
            if not node_id.startswith("document:"):
                continue
            doc_id = node_id.removeprefix("document:")
            if doc_id not in included:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO kb_doc_topology VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, graph_id, node_id, "document", 0, _json(item.get("provenance") or [])),
            )
            for edge in graph["payload"].get("edges") or []:
                if not isinstance(edge, Mapping):
                    continue
                source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
                if source != node_id and target != node_id:
                    continue
                other = target if source == node_id else source
                connection.execute(
                    "INSERT OR IGNORE INTO kb_doc_topology VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        doc_id, graph_id, other, str(edge.get("relation") or ""), 1,
                        _json({"properties": edge.get("properties") or {}, "provenance": edge.get("provenance") or []}),
                    ),
                )
    return loaded


def _insert_baseline_facts(connection: sqlite3.Connection, repo_root: Path, included: set[str]) -> None:
    pack = load_env_pack(repo_root)
    service_path = pack.config_file("service-baseline.yaml")
    log_path = pack.config_file("log-catalog.yaml")
    try:
        service_data = yaml.safe_load(service_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        service_data = {}
    service_map = service_data.get("service_map") if isinstance(service_data, Mapping) else {}
    config_whitelist = service_data.get("config_whitelist") if isinstance(service_data, Mapping) else {}
    if isinstance(service_map, Mapping) and isinstance(config_whitelist, Mapping):
        for native_service, service in service_map.items():
            configs = config_whitelist.get(native_service) or {}
            if not isinstance(configs, Mapping):
                continue
            for config_file, keys in configs.items():
                if not isinstance(keys, list):
                    continue
                for key in keys:
                    fact_id = f"config:{service}:{config_file}:{key}"
                    connection.execute(
                        "INSERT OR REPLACE INTO baseline_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fact_id, "*", str(service), "", "config_ref",
                            f"{config_file}.{key}", _json({"file": config_file, "key": key}),
                            "", "", service_path.relative_to(pack.root).as_posix(), "", "observed",
                        ),
                    )
    try:
        log_data = yaml.safe_load(log_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log_data = {}
    platforms = log_data.get("platforms") if isinstance(log_data, Mapping) else {}
    if isinstance(platforms, Mapping):
        for platform, pdata in platforms.items():
            services = pdata.get("services") if isinstance(pdata, Mapping) else {}
            if not isinstance(services, Mapping):
                continue
            for service, entries in services.items():
                if not isinstance(entries, list):
                    continue
                for index, entry in enumerate(entries):
                    if not isinstance(entry, Mapping) or not entry.get("path"):
                        continue
                    fact_id = f"log:{platform}:{service}:{index}:{entry['path']}"
                    connection.execute(
                        "INSERT OR REPLACE INTO baseline_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fact_id, str(platform), str(service), "", "log_location",
                            str(entry["path"]), _json(dict(entry)), "", "", log_path.relative_to(pack.root).as_posix(),
                            str(entry.get("collected_at") or ""), "observed",
                        ),
                    )
    knowledge_root = resolve_knowledge_root(repo_root)
    bases = discover_bases(knowledge_root)
    for doc_id in sorted(included):
        try:
            doc = load_doc(knowledge_root / doc_id)
        except (FrontmatterError, OSError):
            continue
        if doc.profile != "baseline":
            continue
        connection.execute(
            "INSERT OR REPLACE INTO baseline_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"document:{doc_id}", str(doc.platform or ""), str(doc.service or ""),
                _component_tag(Path(doc_id), bases) or "", "baseline_document", doc_id,
                _json({"title": doc.title, "summary": doc.raw.get("summary") or ""}),
                doc_id, f"{doc_id}#section-01", "knowledge", str(doc.observed_at or ""),
                str(doc.confidence or ""),
            ),
        )


def _group_resolution(knowledge_root: Path, included: set[str], warnings: list[str]) -> dict[str, Any]:
    """分组库的确定性组装配置(kb.yaml),运行时只读快照不读 kb.yaml。"""
    result: dict[str, Any] = {}
    for base in discover_bases(knowledge_root):
        if not base.group_field:
            continue
        common = [doc_id for doc_id in base.common_doc_ids() if doc_id in included]
        warnings.extend(
            f"{base.id}/kb.yaml: resolve.common 文档不存在 {doc_id}"
            for doc_id in base.common_doc_ids() if doc_id not in included
        )
        result[base.id] = {
            "group_field": base.group_field, "platform": base.platform or "",
            "service": base.service or "", "common": common,
        }
    return result


def build_retrieval_index(repo_root: Path, output: Path | None = None) -> Path:
    """Build the retrieval snapshot atomically beside the destination."""
    root = Path(repo_root).resolve()
    destination = Path(output or resolve_data_root(root) / "build" / "kb-retrieval.sqlite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pack = load_env_pack(root)
    files = [pack.config_file("service-baseline.yaml"), pack.config_file("log-catalog.yaml")]
    files.extend(path for _graph_id, path in _topology_files(root))
    knowledge_root = resolve_knowledge_root(root)
    files.extend(knowledge_root.rglob("*.md"))
    fingerprint = _fingerprint(files)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(raw_temp)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            with connection:
                _create_schema(connection)
                included, warnings = _insert_documents(connection, knowledge_root)
                _insert_chunks(connection, knowledge_root, included)
                _insert_topology(connection, root, included)
                _insert_baseline_facts(connection, root, included)
                now = datetime.now(timezone.utc).isoformat()
                meta = {
                    "schema_version": SCHEMA_VERSION,
                    "knowledge_fingerprint": fingerprint,
                    "built_at": now,
                    "warnings": warnings,
                    "group_resolution": _group_resolution(knowledge_root, included, warnings),
                }
                connection.executemany(
                    "INSERT INTO retrieval_meta VALUES (?, ?)",
                    ((key, _json(value) if not isinstance(value, str) else value) for key, value in meta.items()),
                )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-shm", "-wal"):
            Path(f"{temporary}{suffix}").unlink(missing_ok=True)
    return destination
