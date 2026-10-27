"""知识库文档 frontmatter 加载器与封闭词表。

4 类型封闭集 + v2 文档层/用途/可信度词表,是 kb-lint / routing 编译 / 拓扑
构建三个消费者共享的真源。_index.md 走文件名约定,不属 4 类型。
"""
from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from lib.envpack import KNOWLEDGE_ENV_SCOPE, active_env_pack
from lib.inventory import InvalidResourceRef, parse_ref

DOC_TYPES = {"troubleshoot", "knowledge", "procedure", "oplog"}
STATUSES = {"draft", "verified"}
KNOWLEDGE_LAYERS = {"manual_task", "alert", "basic_info"}
ENTRYPOINTS = {"manual_task", "alert", "basic_info"}
DOCUMENT_PROFILES = {
    "diagnosis", "sop", "baseline", "topology", "reference", "oplog",
}
EXECUTION_SCOPES = {"agent_readonly", "human_only", "mixed"}
CONFIDENCE_LEVELS = {"observed", "verified", "historical", "inferred", "unknown"}
# 操作手册细分:日常 / 变更调优 / 部署(缺省日常,见 lib/kb/categories.py)
SOP_KINDS = {"routine", "change", "deploy"}


class _PackVocabulary(AbstractSet):
    """环境包里的封闭词表(env.yaml 的 knowledge 段),按需读取;集合运算结果为 frozenset。"""

    def __init__(self, attr: str):
        self._attr = attr

    def _values(self) -> tuple:
        return getattr(active_env_pack(), self._attr)

    def __contains__(self, value) -> bool:
        return value in self._values()

    def __iter__(self):
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())

    @classmethod
    def _from_iterable(cls, iterable):
        return frozenset(iterable)


# 任务类型与 service 封闭词表随环境包(env.yaml knowledge.task_types / knowledge.services),新增须编辑那里
TASK_TYPES = _PackVocabulary("task_types")
SERVICES = _PackVocabulary("services")


class FrontmatterError(ValueError):
    pass


@dataclass
class Doc:
    path: Path
    is_index: bool
    title: Optional[str] = None
    type: Optional[str] = None
    platform: Optional[str] = None
    # platform 对应的资源 ID(集群或环境级);platform 本身是知识库键(目录名),索引与检索按它
    platform_ref: Optional[str] = None
    service: Optional[str] = None
    problem: Optional[str] = None
    symptoms: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)
    playbook: Optional[str] = None
    playbooks: List[dict] = field(default_factory=list)
    diagnostic_paths: List[dict] = field(default_factory=list)
    relations: List[dict] = field(default_factory=list)
    knowledge_layer: Optional[str] = None
    entrypoints: List[str] = field(default_factory=list)
    profile: Optional[str] = None
    execution_scope: Optional[str] = None
    observed_at: Optional[str] = None
    valid_until: Optional[str] = None
    confidence: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    task_type: Optional[str] = None
    sop_kind: Optional[str] = None
    process_id: Optional[int] = None
    task_id: Optional[int] = None
    raw: dict = field(default_factory=dict)


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _check_membership(path: Path, field: str, value, valid: set):
    """封闭集校验,非 str/hashable 一律 FrontmatterError。"""
    if value is None:
        return
    if not isinstance(value, str):
        raise FrontmatterError(
            f"{path}: {field} 必须是字符串,得到 {type(value).__name__}: {value!r}"
        )
    if value not in valid:
        raise FrontmatterError(
            f"{path}: {field} {value!r} 不在封闭集 {sorted(valid)}"
        )


def _require_list(path: Path, field: str, value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FrontmatterError(
            f"{path}: {field} 必须是 YAML 列表,得到 {type(value).__name__}: {value!r} "
            f"(单值也要写成 [xxx])"
        )
    return [str(x).strip() for x in value if str(x).strip()]


def _require_mapping_list(path: Path, field: str, value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FrontmatterError(
            f"{path}: {field} 必须是 YAML 列表,得到 {type(value).__name__}: {value!r}"
        )
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise FrontmatterError(
                f"{path}: {field}[{index}] 必须是映射,得到 {type(item).__name__}"
            )
        result.append(dict(item))
    return result


def _legacy_platform(path: Path, value):
    """platform 新旧并存:知识库键原样返回;资源引用(环境或 knowledge 集群)归一为知识库键。"""
    if not isinstance(value, str):
        return value
    pack = active_env_pack()
    if value in pack.knowledge_platforms():
        return value
    if value == pack.id:
        return KNOWLEDGE_ENV_SCOPE
    if "/" not in value:
        return value
    try:
        ref = parse_ref(value)
    except InvalidResourceRef as exc:
        raise FrontmatterError(f"{path}: platform 资源引用非法: {exc}") from exc
    decl = next((item for item in pack.clusters if item.id == ref.cluster), None)
    if ref.env != pack.id or ref.kind != "cluster" or decl is None or "knowledge" not in decl.roles:
        raise FrontmatterError(f"{path}: platform 只能引用当前环境或其 knowledge 集群: {value}")
    return decl.knowledge_key


def is_platform_ref(value) -> bool:
    """写法判断(不校验合法性):资源引用是环境 ID 本身或带 / 的集群引用,旧短键两者都不是。"""
    return isinstance(value, str) and (value == active_env_pack().id or "/" in value)


def platform_ref(platform: Optional[str]) -> Optional[str]:
    """知识库键 → 资源 ID(环境级范围键对应环境本身)。"""
    if platform is None:
        return None
    pack = active_env_pack()
    if platform == KNOWLEDGE_ENV_SCOPE:
        return pack.id
    decl = pack.knowledge_cluster(platform)
    return f"{pack.id}/{decl.id}" if decl is not None else None


def load_doc(path: Path) -> Doc:
    path = Path(path)
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    m = _FM_RE.match(text)
    if m is None:
        if text.startswith("---"):
            raise FrontmatterError(f"{path}: frontmatter 未闭合(缺 --- 结束标记)")
        raise FrontmatterError(f"{path}: 缺少 frontmatter")
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise FrontmatterError(f"{path}: frontmatter YAML 语法错误 — {e}")
    if not isinstance(data, dict):
        raise FrontmatterError(f"{path}: frontmatter 顶层必须是映射")

    is_index = path.stem.endswith("_index") or path.name == "_index.md"
    platforms = set(active_env_pack().knowledge_platforms())
    doc = Doc(path=path, is_index=is_index, raw=data, title=data.get("title"),
              platform=_legacy_platform(path, data.get("platform")),
              service=data.get("service"))

    if is_index:
        # index 只校验 platform/service 可选
        _check_membership(path, "platform", doc.platform, platforms)
        doc.platform_ref = platform_ref(doc.platform)
        return doc

    # 非 index:走 4 类型封闭集校验
    doc.type = data.get("type")
    _check_membership(path, "type", doc.type, DOC_TYPES)
    if doc.type is None:
        raise FrontmatterError(
            f"{path}: type 不能为空,应为 {sorted(DOC_TYPES)}"
        )
    _check_membership(path, "platform", doc.platform, platforms)
    if doc.platform is None:
        raise FrontmatterError(
            f"{path}: platform 不能为空,应为 {sorted(platforms)}"
        )
    doc.platform_ref = platform_ref(doc.platform)
    _check_membership(path, "service", doc.service, SERVICES)
    doc.problem = data.get("problem")
    doc.symptoms = _require_list(path, "symptoms", data.get("symptoms"))
    doc.alerts = _require_list(path, "alerts", data.get("alerts"))
    doc.playbooks = _require_mapping_list(path, "playbooks", data.get("playbooks"))
    doc.diagnostic_paths = _require_mapping_list(
        path, "diagnostic_paths", data.get("diagnostic_paths"),
    )
    doc.relations = _require_mapping_list(path, "relations", data.get("relations"))
    legacy_playbook = data.get("playbook")
    if legacy_playbook is not None and not isinstance(legacy_playbook, str):
        raise FrontmatterError(
            f"{path}: playbook 必须是字符串或 null,得到 {type(legacy_playbook).__name__}"
        )
    if legacy_playbook and not doc.playbooks:
        doc.playbooks = [{"path": legacy_playbook, "order": 1}]
    if doc.playbooks:
        for index, item in enumerate(doc.playbooks):
            if not isinstance(item.get("path"), str) or not str(item["path"]).strip():
                raise FrontmatterError(f"{path}: playbooks[{index}].path 不能为空")
        doc.playbook = str(doc.playbooks[0]["path"])
    else:
        doc.playbook = str(legacy_playbook or "") or None
    doc.knowledge_layer = data.get("knowledge_layer")
    _check_membership(path, "knowledge_layer", doc.knowledge_layer, KNOWLEDGE_LAYERS)
    doc.entrypoints = _require_list(path, "entrypoints", data.get("entrypoints"))
    for item in doc.entrypoints:
        if item not in ENTRYPOINTS:
            raise FrontmatterError(
                f"{path}: entrypoints {item!r} 不在封闭集 {sorted(ENTRYPOINTS)}"
            )
    doc.profile = data.get("profile")
    _check_membership(path, "profile", doc.profile, DOCUMENT_PROFILES)
    doc.execution_scope = data.get("execution_scope")
    _check_membership(path, "execution_scope", doc.execution_scope, EXECUTION_SCOPES)
    doc.observed_at = data.get("observed_at")
    doc.valid_until = data.get("valid_until")
    doc.confidence = data.get("confidence")
    _check_membership(path, "confidence", doc.confidence, CONFIDENCE_LEVELS)
    doc.summary = data.get("summary")
    doc.status = data.get("status")
    _check_membership(path, "status", doc.status, STATUSES)
    doc.task_type = data.get("task_type")
    _check_membership(path, "task_type", doc.task_type, TASK_TYPES)
    doc.sop_kind = data.get("sop_kind")
    _check_membership(path, "sop_kind", doc.sop_kind, SOP_KINDS)
    for name in ("process_id", "task_id"):
        value = data.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
            raise FrontmatterError(f"{path}: {name} 必须是正整数,得到 {value!r}")
        setattr(doc, name, int(value))
    return doc
