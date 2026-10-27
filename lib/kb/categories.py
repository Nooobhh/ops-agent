"""运维视角的 5 类文档:由 type/profile 推导,知识页与 ContextPack 共用一份口径。

故障处理 troubleshoot | 操作手册 procedure(细分 sop_kind) | 集群信息 knowledge/baseline、topology |
参考知识 knowledge/reference 及其他 knowledge | 操作记录 oplog
"""
from __future__ import annotations

from typing import Optional

from .frontmatter import SOP_KINDS

CATEGORIES = ("troubleshoot", "procedure", "cluster_info", "reference", "oplog")
CATEGORY_LABELS = {
    "troubleshoot": "故障处理", "procedure": "操作手册", "cluster_info": "集群信息",
    "reference": "参考知识", "oplog": "操作记录",
}
SOP_KIND_LABELS = {"routine": "日常", "change": "变更调优", "deploy": "部署"}
DEFAULT_SOP_KIND = "routine"
assert set(SOP_KIND_LABELS) == SOP_KINDS


def doc_category(doc_type: Optional[str], profile: Optional[str]) -> str:
    if doc_type in {"troubleshoot", "procedure", "oplog"}:
        return str(doc_type)
    if profile in {"baseline", "topology"}:
        return "cluster_info"
    return "reference"


def sop_kind_of(doc_type: Optional[str], sop_kind: Optional[str]) -> Optional[str]:
    """操作手册缺省为日常;其他类别没有 sop_kind。"""
    return (sop_kind or DEFAULT_SOP_KIND) if doc_type == "procedure" else None
