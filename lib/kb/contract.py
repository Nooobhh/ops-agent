"""Versioned knowledge document contract and deterministic Markdown renderer."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lib.envpack.fields import KnowledgePlatform

from .frontmatter import (
    DOCUMENT_PROFILES,
    ENTRYPOINTS,
    EXECUTION_SCOPES,
    KNOWLEDGE_LAYERS,
    SERVICES,
    SOP_KINDS,
    TASK_TYPES,
    platform_ref,
)


# v3 = v2 + platform 写资源引用(环境或集群);v1/v2 照旧可读
SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}
DOCUMENT_SOURCES = {"triage", "manual_import"}
SECTION_ORDER = {
    "troubleshoot": (
        "摘要", "适用现象", "关键判据", "机理", "排查步骤",
        "处置建议", "坑与误导信号", "历史案例",
    ),
    "knowledge": ("摘要", "适用范围", "关键知识", "边界与注意事项", "参考"),
    "procedure": ("摘要", "适用场景", "前置条件", "步骤", "验证", "回滚"),
    "oplog": ("摘要", "操作步骤", "验证过程", "回滚"),
}
OPTIONAL_SECTIONS = {"troubleshoot": {"坑与误导信号", "历史案例"}}
V2_SECTION_ORDER = {
    "diagnosis": (
        "摘要", "适用现象", "关键判据", "机理", "排查步骤",
        "证据与结果", "处置建议", "坑与误导信号", "历史案例", "补充材料",
    ),
    "baseline": (
        "摘要", "适用范围", "组件与节点", "关键配置", "最新观测",
        "边界与时效", "参考",
    ),
    "topology": (
        "摘要", "适用范围", "实体与关系", "关键事实", "边界与时效", "参考",
    ),
    "reference": ("摘要", "适用范围", "关键知识", "边界与注意事项", "参考"),
    "sop": (
        "摘要", "适用场景", "前置条件", "步骤", "验证", "回滚", "补充材料",
    ),
    "oplog": ("摘要", "操作步骤", "验证过程", "回滚", "补充材料"),
}
V2_OPTIONAL_SECTIONS = {
    "diagnosis": {"证据与结果", "坑与误导信号", "历史案例", "补充材料"},
    "baseline": set(),
    "topology": {"关键事实"},
    "reference": set(),
    "sop": {"补充材料"},
    "oplog": {"补充材料"},
}
_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_OPLOG_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")


class ImportManifest(BaseModel):
    """Structured content produced by the model before local-file import."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["new", "update"] = "new"
    knowledge_base: Optional[str] = None
    title: str = Field(min_length=1, max_length=160)
    type: Literal["troubleshoot", "knowledge", "procedure", "oplog"]
    platform: KnowledgePlatform
    service: str
    problem: str
    summary: str = Field(min_length=1, max_length=500)
    knowledge_layer: Optional[Literal["manual_task", "alert", "basic_info"]] = None
    entrypoints: List[Literal["manual_task", "alert", "basic_info"]] = Field(
        default_factory=list,
    )
    profile: Optional[Literal[
        "diagnosis", "sop", "baseline", "topology", "reference", "oplog",
    ]] = None
    execution_scope: Optional[Literal["agent_readonly", "human_only", "mixed"]] = None
    observed_at: Optional[str] = None
    valid_until: Optional[str] = None
    confidence: Optional[Literal[
        "observed", "verified", "historical", "inferred", "unknown",
    ]] = None
    filter_rules_sha256: Optional[str] = None
    symptoms: List[str] = Field(default_factory=list)
    alerts: List[str] = Field(default_factory=list)
    playbook: Optional[str] = None
    playbooks: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostic_paths: List[Dict[str, Any]] = Field(default_factory=list)
    relations: List[Dict[str, Any]] = Field(default_factory=list)
    related: List[str] = Field(default_factory=list)
    # 调度任务类文档(分组库):任务类型与(可选)特化到具体流程/任务
    task_type: Optional[str] = None
    # 操作手册细分 routine/change/deploy,缺省 routine 不写
    sop_kind: Optional[str] = None
    process_id: Optional[int] = Field(default=None, gt=0)
    task_id: Optional[int] = Field(default=None, gt=0)
    sections: Dict[str, str]

    @field_validator("service")
    @classmethod
    def validate_service(cls, value: str) -> str:
        if value not in SERVICES:
            raise ValueError(f"service 不在封闭集: {value}")
        return value

    @field_validator("sop_kind")
    @classmethod
    def validate_sop_kind(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in SOP_KINDS:
            raise ValueError(f"sop_kind 不在封闭集: {value}")
        return value

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in TASK_TYPES:
            raise ValueError(f"task_type 不在封闭集: {value}")
        return value

    @field_validator("problem")
    @classmethod
    def validate_problem(cls, value: str) -> str:
        if not (_SLUG_RE.fullmatch(value) or _OPLOG_SLUG_RE.fullmatch(value)):
            raise ValueError("problem 必须是小写 kebab-case")
        return value

    @model_validator(mode="before")
    @classmethod
    def normalize_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        doc_type = data.get("type")
        layer = data.get("knowledge_layer")
        if layer is None:
            layer = "alert" if doc_type == "troubleshoot" and data.get("alerts") else (
                "basic_info" if doc_type == "knowledge" else "manual_task"
            )
            data["knowledge_layer"] = layer
        if not data.get("entrypoints"):
            entrypoints = [layer]
            if layer != "manual_task":
                entrypoints.append("manual_task")
            data["entrypoints"] = entrypoints
        if not data.get("profile"):
            if doc_type == "troubleshoot":
                data["profile"] = "diagnosis"
            elif doc_type == "procedure":
                data["profile"] = "sop"
            elif doc_type == "oplog":
                data["profile"] = "oplog"
            else:
                data["profile"] = "reference"
        if not data.get("execution_scope"):
            data["execution_scope"] = (
                "agent_readonly" if layer == "basic_info" else "mixed"
            )
        if not data.get("confidence"):
            data["confidence"] = "historical"
        if data.get("playbook") and not data.get("playbooks"):
            data["playbooks"] = [{"path": data["playbook"], "order": 1}]
        return data

    @model_validator(mode="after")
    def validate_sections(self) -> "ImportManifest":
        order = V2_SECTION_ORDER[self.profile or "reference"]
        expected = set(order) - {"摘要"}
        optional = V2_OPTIONAL_SECTIONS.get(self.profile or "reference", set())
        actual = set(self.sections)
        missing = expected - optional - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"缺少章节: {', '.join(sorted(missing))}")
        if extra:
            raise ValueError(f"未知章节: {', '.join(sorted(extra))}")
        blank = [name for name, content in self.sections.items()
                 if name not in optional and not str(content).strip()]
        if blank:
            raise ValueError(f"章节内容不能为空: {', '.join(blank)}")
        if self.type == "troubleshoot" and not self.symptoms:
            raise ValueError("troubleshoot 至少需要一个 symptom")
        if self.type != "troubleshoot" and (
            self.alerts or self.playbook or self.playbooks or self.diagnostic_paths
        ):
            raise ValueError("只有 troubleshoot 可以声明 alerts/playbook")
        if self.sop_kind is not None and self.type != "procedure":
            raise ValueError("只有 procedure 可以声明 sop_kind")
        if self.type == "oplog" and not _OPLOG_SLUG_RE.fullmatch(self.problem):
            raise ValueError("oplog.problem 必须以 yyyy-mm-dd- 开头")
        if self.type == "troubleshoot" and self.profile not in {"diagnosis", "sop"}:
            raise ValueError("troubleshoot.profile 必须是 diagnosis 或 sop")
        for index, item in enumerate(self.playbooks):
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                raise ValueError(f"playbooks[{index}] 必须包含非空 path")
        for index, path in enumerate(self.diagnostic_paths):
            if not isinstance(path, dict) or not str(path.get("id") or "").strip():
                raise ValueError(f"diagnostic_paths[{index}] 必须包含非空 id")
            refs = path.get("playbooks", [])
            if not isinstance(refs, list):
                raise ValueError(f"diagnostic_paths[{index}].playbooks 必须是列表")
        for index, relation in enumerate(self.relations):
            if not isinstance(relation, dict) or not str(relation.get("target") or relation.get("target_service") or "").strip():
                raise ValueError(f"relations[{index}] 必须包含 target")
            if not str(relation.get("relation") or "").strip():
                raise ValueError(f"relations[{index}] 必须包含 relation")
        return self


def target_relative(manifest: ImportManifest) -> Path:
    if (
        manifest.type == "knowledge"
        and manifest.platform == "shared"
        and manifest.service == "topology"
    ):
        return Path("shared", "topology", f"{manifest.problem}.md")
    if manifest.type == "procedure" and manifest.platform == "shared":
        return Path("shared", "procedures", f"{manifest.problem}.md")
    if manifest.type == "oplog":
        return Path("oplog", manifest.platform, f"{manifest.problem}.md")
    return Path(
        manifest.platform,
        manifest.service,
        f"{manifest.platform}-{manifest.service}-{manifest.problem}.md",
    )


def source_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render_document(
    manifest: ImportManifest,
    *,
    source: Literal["manual_import", "triage"],
    source_ref: str,
    source_sha256: Optional[str] = None,
    status: Optional[str] = None,
    verified_by: Optional[str] = None,
    verified_at: Optional[str] = None,
    verified_reason: Optional[str] = None,
    author: Optional[str] = None,
) -> str:
    profile = manifest.profile or "reference"
    # knowledge 是可直接检索的基础事实/参考资料，不进入排查文档的
    # draft→verified 晋升生命周期；其可信度由 confidence/observed_at 表达。
    effective_status = None if manifest.type == "knowledge" else (status or "draft")
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_ref": source_ref,
        "title": manifest.title,
        "type": manifest.type,
        # 输入契约(模型提案/导入 manifest)仍是短键,落盘写资源引用
        "platform": platform_ref(manifest.platform),
        "service": manifest.service,
        "problem": manifest.problem,
        "summary": manifest.summary,
        "knowledge_layer": manifest.knowledge_layer,
        "entrypoints": manifest.entrypoints,
        "profile": profile,
        "execution_scope": manifest.execution_scope,
        "confidence": manifest.confidence,
        "related": manifest.related,
    }
    if author:
        frontmatter["author"] = author
    if effective_status is not None:
        frontmatter["status"] = effective_status
    if manifest.type != "knowledge" and verified_by:
        frontmatter["verified_by"] = verified_by
    if manifest.type != "knowledge" and verified_at:
        frontmatter["verified_at"] = verified_at
    if manifest.type != "knowledge" and verified_reason:
        frontmatter["verified_reason"] = verified_reason
    if manifest.observed_at:
        frontmatter["observed_at"] = manifest.observed_at
    if manifest.valid_until:
        frontmatter["valid_until"] = manifest.valid_until
    if source_sha256:
        frontmatter["source_sha256"] = source_sha256
    if manifest.filter_rules_sha256:
        frontmatter["filter_rules_sha256"] = manifest.filter_rules_sha256
    if manifest.type == "troubleshoot":
        frontmatter.update({
            "symptoms": manifest.symptoms,
            "alerts": manifest.alerts,
            "playbooks": manifest.playbooks,
            "diagnostic_paths": manifest.diagnostic_paths,
        })
    if manifest.relations:
        frontmatter["relations"] = manifest.relations
    for name in ("sop_kind", "task_type", "process_id", "task_id"):
        if getattr(manifest, name) is not None:
            frontmatter[name] = getattr(manifest, name)

    header = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False,
    ).rstrip()
    content = {"摘要": manifest.summary, **manifest.sections}
    lines = ["---", header, "---", "", f"# {manifest.title}", ""]
    optional = V2_OPTIONAL_SECTIONS.get(profile, set())
    for section in V2_SECTION_ORDER[profile]:
        body = str(content.get(section) or "").strip()
        if not body and section in optional:
            continue
        lines.extend([f"## {section}", "", body, ""])
    return "\n".join(lines).rstrip() + "\n"


def triage_manifest(
    proposal: dict,
    session: dict,
    case_line: str,
    *,
    confidence: Literal["observed", "verified", "historical", "inferred", "unknown"] = "verified",
) -> ImportManifest:
    symptoms = [str(item).strip() for item in proposal.get("symptoms") or [] if str(item).strip()]
    evidence = [str(item).strip() for item in proposal.get("evidence") or [] if str(item).strip()]
    remediation = [str(item).strip() for item in proposal.get("remediation") or [] if str(item).strip()]
    steps = session.get("steps") or []
    rendered_steps = "\n".join(
        f"- `$ {step.get('cmd', '')}` (exit={step.get('exit_code', '?')})"
        for step in steps
    ) or "- 本次通过已登记 playbook 或诊断动作完成采证。"
    mechanism = str(proposal.get("mechanism") or "").strip()
    summary = str(proposal.get("summary") or mechanism).strip()
    return ImportManifest.model_validate({
        "title": proposal.get("title"),
        "type": "troubleshoot",
        "platform": proposal.get("platform"),
        "service": proposal.get("service"),
        "problem": proposal.get("problem"),
        "summary": summary,
        "knowledge_layer": "manual_task",
        "entrypoints": ["manual_task"],
        "profile": "diagnosis",
        "execution_scope": "mixed",
        "confidence": confidence,
        "symptoms": symptoms,
        "alerts": proposal.get("alerts") or [],
        "playbooks": proposal.get("playbooks") or [],
        "diagnostic_paths": proposal.get("diagnostic_paths") or [],
        "related": [],
        "sections": {
            "适用现象": "\n".join(f"- {item}" for item in symptoms),
            "关键判据": "\n".join(f"- {item}" for item in evidence)
                         or "- 当前没有通过校验的可引用证据，详见排查报告。",
            "机理": mechanism,
            "排查步骤": rendered_steps,
            "证据与结果": "\n".join(f"- {item}" for item in evidence)
                           or "- 当前没有通过校验的可引用证据，详见排查报告。",
            "处置建议": "\n".join(f"- {item}" for item in remediation)
                         or "- 本次没有提出生产变更建议。",
            "历史案例": case_line,
        },
    })
