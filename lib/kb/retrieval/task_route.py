"""分组库的确定性知识组装:通用文档 → 任务类型通用文档 → pid/tid 特化文档。

哪个库、哪张图谱、提示用的系统名由环境包 ``knowledge.assembly`` 声明(未声明即不组装)。
只读检索快照:pid/tid → task_type 取图谱快照的 task 节点;文档角色取 rebuild 时
由 kb.yaml(group_field/common_dir/resolve.common)与 frontmatter 生成的标签(见 index.py)。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from lib.envpack import AssemblyDecl, active_env_pack
from lib.kb.frontmatter import TASK_TYPES

# 任务拓扑超过该天数提示过期:之后新建或改过的任务可能识别不到类型
TOPOLOGY_STALE_DAYS = 7

# processId: 1016 / pid=1016 / 流程 1016;taskId: 13373 / tid 13373 / 任务13373;p1016-t13373。
# 中文关键词常见于普通叙述(「任务 3 次重试」),要求至少 3 位数字才认作编号。
_PROCESS_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])(?:process[_ ]?id|process|pid)\s*[:=#：-]?\s*(\d+)"
    r"|流程(?:ID|号)?\s*[:=#：-]?\s*(\d{3,}))(?!\d)",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])(?:task[_ ]?id|task|tid)\s*[:=#：-]?\s*(\d+)"
    r"|任务(?:ID|号)?\s*[:=#：-]?\s*(\d{3,}))(?!\d)",
    re.IGNORECASE,
)
_PAIR_RE = re.compile(r"(?<![A-Za-z0-9_])p(\d+)-t(\d+)(?!\d)", re.IGNORECASE)


def extract_task_ids(text: str) -> tuple[list[str], list[str]]:
    """查询文本里点名的流程号与任务号(去重保序)。"""
    process_ids = [left or right for left, right in _PROCESS_RE.findall(text)]
    task_ids = [left or right for left, right in _TASK_RE.findall(text)]
    for pid, tid in _PAIR_RE.findall(text):
        process_ids.append(pid)
        task_ids.append(tid)
    return list(dict.fromkeys(process_ids)), list(dict.fromkeys(task_ids))


@dataclass
class TaskRoute:
    process_id: Optional[int]
    task_id: Optional[int]
    platform: Optional[str] = None             # kb.yaml 登记的平台(分组库只属一个集群)
    task_type: Optional[str] = None
    type_source: Optional[str] = None          # request / topology
    task_name: Optional[str] = None
    topology_node: Optional[str] = None
    topology_generated_at: Optional[str] = None
    common: list[dict[str, Any]] = field(default_factory=list)
    general: list[dict[str, Any]] = field(default_factory=list)
    specific: list[dict[str, Any]] = field(default_factory=list)
    related: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def required(self) -> list[dict[str, Any]]:
        """固定组装顺序的必读:通用 → 类型通用 → pid/tid 特化;同组其他文档只作参考。"""
        return [*self.common, *self.general, *self.specific]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ROLE_REASONS = {
    "common": "{label} 任务通用排查",
    "general": "任务类型通用排查",
    "specific": "流程/任务特化排查",
    "related": "同类型任务案例",
}


def assembly() -> Optional[AssemblyDecl]:
    """环境包声明的分组库组装;未声明返回 None(不组装、不做语境加权)。"""
    return active_env_pack().assembly


def graph_key() -> str:
    decl = assembly()
    return decl.graph_key if decl is not None else ""


def _ref(connection: sqlite3.Connection, doc_id: str, role: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        "SELECT doc_type, profile, status, confidence, title FROM kb_documents WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    chunk_ids = [
        str(item[0]) for item in connection.execute(
            "SELECT chunk_id FROM kb_chunks WHERE doc_id = ? ORDER BY ordinal LIMIT 1", (doc_id,),
        )
    ]
    return {
        "ref_type": "document", "doc_id": doc_id, "chunk_ids": chunk_ids,
        "doc_type": str(row[0]), "profile": str(row[1]), "status": str(row[2]),
        "confidence": str(row[3]), "title": str(row[4]),
        "retrieval_reasons": [f"task_route:{role}"],
        "reason": _ROLE_REASONS[role].format(label=getattr(assembly(), "label", "")),
        "provenance": [{"source": "knowledge", "doc_id": doc_id, "chunk_id": chunk_id} for chunk_id in chunk_ids],
    }


def _tagged(connection: sqlite3.Connection, *tags: str) -> list[str]:
    placeholders = ",".join("?" for _ in tags)
    return [
        str(row[0]) for row in connection.execute(
            f"SELECT doc_id FROM kb_doc_tags WHERE doc_id LIKE ? AND tag IN ({placeholders}) "
            "GROUP BY doc_id HAVING COUNT(DISTINCT tag) = ? ORDER BY doc_id",
            (f"{assembly().library}/%", *tags, len(tags)),
        )
    ]


def _doc_tag(connection: sqlite3.Connection, doc_id: str, prefix: str) -> Optional[str]:
    row = connection.execute(
        "SELECT tag FROM kb_doc_tags WHERE doc_id = ? AND tag LIKE ?", (doc_id, f"{prefix}:%"),
    ).fetchone()
    return str(row[0]).split(":", 1)[1] if row else None


def group_platform(connection: sqlite3.Connection) -> str:
    """分组库登记的所属平台(rebuild 时取自 kb.yaml);读不到为空串。"""
    return str((_resolution(connection) or {}).get("platform") or "") if assembly() is not None else ""


def _resolution(connection: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = connection.execute(
        "SELECT value FROM retrieval_meta WHERE key = 'group_resolution'"
    ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0]).get(assembly().library)
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, dict) else None


def topology_freshness(connection: sqlite3.Connection,
                       now: Optional[datetime] = None) -> tuple[Optional[str], Optional[str]]:
    """→ (任务拓扑采集时间, 缺失或过期时的提示);两处(文档组装、依赖查询)共用。"""
    decl = assembly()
    if decl is None:
        return None, "环境包未声明分组库组装(knowledge.assembly)"
    row = connection.execute(
        "SELECT generated_at, status FROM topology_meta WHERE graph_id = ?", (decl.graph_key,),
    ).fetchone()
    refresh = decl.refresh_hint
    if row is None or str(row[1]) != "ok":
        return None, f"{decl.label} 任务拓扑快照缺失，无法按流程/任务号识别；{refresh}"
    generated_at = str(row[0] or "") or None
    try:
        generated = datetime.fromisoformat(str(row[0]))
    except ValueError:
        return generated_at, None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - generated).days
    if age > TOPOLOGY_STALE_DAYS:
        return generated_at, (f"{decl.label} 任务拓扑采集于 {generated.date()}({age} 天前)，"
                              f"之后新建或改过的任务可能识别不到；{refresh}")
    return generated_at, None


def _check_freshness(connection: sqlite3.Connection, route: TaskRoute, now: Optional[datetime] = None) -> None:
    route.topology_generated_at, gap = topology_freshness(connection, now)
    if gap:
        route.gaps.append(gap)


def resolve_task_route(
    connection: sqlite3.Connection,
    *,
    process_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_type: Optional[str] = None,
) -> TaskRoute:
    """按 pid/tid(或显式 task_type)组装分组库文档;解析不出的环节记入 gaps,不猜。"""
    route = TaskRoute(process_id=process_id, task_id=task_id)
    decl = assembly()
    if decl is None:
        route.gaps.append("环境包未声明分组库组装(knowledge.assembly)")
        return route
    resolution = _resolution(connection)
    if resolution is None:
        route.gaps.append(f"检索快照缺少 {decl.library} 分组组装配置，请运行 scripts/build_wiki.py")
        return route
    graph_id = decl.graph_key
    platform = route.platform = str(resolution.get("platform") or "")
    if not platform:
        route.gaps.append(f"{decl.library} 库未登记所属平台(kb.yaml),无法按流程/任务号识别")
        return route
    _check_freshness(connection, route)
    if task_id is not None:
        if process_id is not None:
            node_ids = [f"task:{platform}:{process_id}:{task_id}"]
        else:
            node_ids = [
                str(row[0]) for row in connection.execute(
                    "SELECT node_id FROM topology_nodes WHERE graph_id = ? AND kind = 'task' AND node_id LIKE ?",
                    (graph_id, f"task:{platform}:%:{task_id}"),
                )
            ]
        rows = [
            row for node_id in node_ids
            if (row := connection.execute(
                "SELECT node_id, name, properties_json FROM topology_nodes WHERE graph_id = ? AND node_id = ?",
                (graph_id, node_id),
            ).fetchone())
        ]
        if len(rows) == 1:
            node_id, name, properties = rows[0]
            route.topology_node, route.task_name = str(node_id), str(name)
            route.process_id = route.process_id or int(str(node_id).split(":")[2])
            topology_type = str(json.loads(properties or "{}").get("task_type") or "").upper()
            if topology_type and not task_type:
                route.task_type, route.type_source = topology_type, "topology"
        elif len(rows) > 1:
            route.gaps.append(f"任务 {task_id} 在拓扑快照中对应多个流程，请补充流程号")
        else:
            route.gaps.append(f"{decl.label} 拓扑快照未登记任务 {process_id or '?'}/{task_id}，无法确定任务类型")
    if task_type:
        value = str(task_type).strip().upper()
        if value in TASK_TYPES:
            route.task_type, route.type_source = value, "request"
        else:
            route.gaps.append(f"未知任务类型 {task_type!r}")
    if task_id is None and task_type is None and process_id is not None:
        route.gaps.append("只给了流程号，任务类型需按任务号确定")

    for doc_id in resolution.get("common") or []:
        if (ref := _ref(connection, str(doc_id), "common")) is not None:
            route.common.append(ref)
    if route.task_type:
        type_tag = f"{resolution.get('group_field') or 'task_type'}:{route.task_type}"
        route.general = [
            ref for doc_id in _tagged(connection, "group_role:general", type_tag)
            if (ref := _ref(connection, doc_id, "general")) is not None
        ]
        if not route.general:
            route.gaps.append(f"{route.task_type} 类型尚无通用排查文档")
        route.related = [
            ref for doc_id in _tagged(connection, "group_role:related", type_tag)
            if (ref := _ref(connection, doc_id, "related")) is not None
        ]
    if route.process_id is not None:
        exact, process_wide = [], []
        for doc_id in _tagged(connection, "group_role:specific", f"process_id:{route.process_id}"):
            doc_task = _doc_tag(connection, doc_id, "task_id")
            if doc_task is None:
                process_wide.append(doc_id)
            elif route.task_id is not None and doc_task == str(route.task_id):
                exact.append(doc_id)
        route.specific = [
            ref for doc_id in [*exact, *process_wide]
            if (ref := _ref(connection, doc_id, "specific")) is not None
        ]
    return route
