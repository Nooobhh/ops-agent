"""Stable, transport-neutral contracts for knowledge retrieval."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class RetrievalRequest:
    """Input accepted by the standalone retrieval layer.

    The caller supplies ``profile``.  Classification of a human task is outside
    this package; the profile only selects deterministic retrieval behavior.
    """

    query: str
    profile: str = "default"
    platform: Optional[str] = None
    scope: Optional[str] = None
    service: Optional[str] = None
    component: Optional[str] = None
    node: Optional[str] = None
    alertname: Optional[str] = None
    artifact_kind: Optional[str] = None
    execution_summary: Mapping[str, Any] = field(default_factory=dict)
    retrieval_stage: str = "initial"
    fallback_count: int = 0
    query_vector: Optional[Sequence[float]] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        if self.query_vector is not None:
            value["query_vector"] = list(self.query_vector)
        return value


CONTEXT_PACK_SCHEMA_VERSION = 2

# These names are intentionally broader than the current retrieval output.  A
# ContextPack is a model-facing boundary, so a future retrieval implementation
# must not smuggle document bodies through a newly-named field.
_DOCUMENT_BODY_KEYS = frozenset({
    "text", "body", "content", "raw_preview", "full_document", "document_text",
    "highlights", "key_facts", "summary", "snippet", "excerpt", "excerpts",
})
_REFERENCE_KEYS = frozenset({
    "ref_type", "doc_id", "path", "chunk_id", "chunk_ids", "title", "doc_type",
    "profile", "status", "confidence", "observed_at", "retrieval_reasons",
    "scores", "provenance", "source", "reason", "category", "sop_kind",
})


def _clean_reference(value: Any) -> Optional[Dict[str, Any]]:
    """Normalize one document reference and drop legacy body fields.

    Retrieval is allowed to keep richer internal records, but the ContextPack
    contract only carries identifiers and bounded index metadata.  This helper
    is also used when reading v1 snapshots so old tasks cannot re-inject a
    document body into a new Agent turn.
    """
    if isinstance(value, str):
        value = {"doc_id": value}
    if not isinstance(value, Mapping):
        return None
    doc_id = str(value.get("doc_id") or value.get("path") or "").strip()
    path = str(value.get("path") or "").strip()
    if not doc_id and not path:
        return None
    for candidate in (doc_id, path):
        if candidate and (
            candidate.startswith("/")
            or candidate.startswith("\\")
            or ".." in PurePosixPath(candidate.replace("\\", "/")).parts
        ):
            raise ValueError("ContextPack 文档引用必须是知识库内的相对路径")
    result: Dict[str, Any] = {
        "ref_type": str(value.get("ref_type") or "document"),
        "doc_id": doc_id or path,
        "chunk_ids": [
            str(item) for item in (value.get("chunk_ids") or [])
            if str(item).strip()
        ],
    }
    if path:
        result["path"] = path
    for key in _REFERENCE_KEYS - {"ref_type", "doc_id", "path", "chunk_id", "chunk_ids"}:
        if key in value and value[key] not in (None, "", [], {}):
            result[key] = value[key]
    if value.get("chunk_id") and not result["chunk_ids"]:
        result["chunk_ids"] = [str(value["chunk_id"])]
    return result


def _reference_bucket(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := _clean_reference(raw)) is not None]


def _normalize_context_section(value: Any) -> Any:
    """Drop known document-body fields from legacy nested sections.

    Structured baseline facts and topology nodes are not document bodies and
    are retained.  Known document-reference buckets are normalized explicitly.
    """
    if not isinstance(value, Mapping):
        return value
    result: Dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text in _DOCUMENT_BODY_KEYS:
            continue
        if key_text in {
            "sources", "topology_sources", "document_refs", "knowledge_refs",
            "diagnosis", "knowledge", "procedures", "alert_route", "basic_info", "task_route",
        }:
            result[key_text if key_text != "topology_sources" else "document_refs"] = _reference_bucket(item)
        elif isinstance(item, Mapping):
            result[key_text] = _normalize_context_section(item)
        elif isinstance(item, list) and key_text not in {"facts", "config_refs", "log_locations", "paths", "anchors", "warnings"}:
            result[key_text] = [
                _normalize_context_section(child) if isinstance(child, Mapping) else child
                for child in item
            ]
        else:
            result[key_text] = item
    return result


def normalize_context_pack(value: Mapping[str, Any], *, source_version: Optional[int] = None) -> Dict[str, Any]:
    """Return the model-facing reference-only ContextPack v2 projection.

    v1 snapshots are accepted for recovery.  Their document body-shaped fields
    are removed rather than copied forward.  Full retrieval snapshots remain
    available in TaskStore for audit and migration purposes.
    """
    if not isinstance(value, Mapping):
        raise ValueError("ContextPack 必须是 object")
    version = int(source_version or value.get("schema_version") or 1)
    result = {str(key): item for key, item in value.items() if str(key) not in _DOCUMENT_BODY_KEYS}
    baseline = _normalize_context_section(result.get("baseline_context") or {})
    topology = _normalize_context_section(result.get("topology_context") or {})
    knowledge = _normalize_context_section(result.get("knowledge_context") or {})
    # Older snapshots used topology_sources under baseline_context.  Keep the
    # normalized field in the same section while exposing the canonical name.
    result["baseline_context"] = baseline
    result["topology_context"] = topology
    result["knowledge_context"] = knowledge
    result["required_reads"] = _reference_bucket(result.get("required_reads") or [])
    meta = dict(result.get("retrieval_meta") or {})
    meta.update({
        "reference_only": True,
        "content_embedded": False,
        "source_schema_version": version,
    })
    if version != CONTEXT_PACK_SCHEMA_VERSION:
        meta["migrated_from_schema_version"] = version
    result["retrieval_meta"] = meta
    result["schema_version"] = CONTEXT_PACK_SCHEMA_VERSION
    return result


@dataclass(frozen=True)
class ContextPack:
    """The only formal output of the retrieval layer.

    The model-facing contract is reference-only: document bodies are read by a
    separate bounded tool after the Agent selects a doc/chunk reference.
    """

    schema_version: int
    retrieval_profile: str
    query: str
    scope: Dict[str, Any]
    entities: List[Dict[str, Any]]
    baseline_context: Dict[str, Any]
    topology_context: Dict[str, Any]
    knowledge_context: Dict[str, Any]
    required_reads: List[Dict[str, Any]]
    gaps: List[str]
    retrieval_meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        return normalize_context_pack(value)
