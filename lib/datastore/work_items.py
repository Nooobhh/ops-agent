"""工作项与计划的本地存储:每项一个 JSON,目录锁下读-改-原子替换,读不加锁;只取消不删除。

工作项 = 一件要做的事;计划 = 导入的阶段性计划表(节点 → 事项 + 产物验证),事项本身就是工作项。
挂载只存三种引用(task/troubleshoot/worklog);报告、产物、审批、写操作经任务派生,不在此存。
一个任务、一条排查记录、一条 worklog 至多挂一个工作项,保证周报分组唯一。
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from lib.atomic_file import atomic_write_json
from lib.envpack import env_timezone
from lib.file_lock import FileLock

from .models import DataStoreError, WorkItemConflict, WorkItemNotFound, validate_author_id

STATUSES = ("todo", "in_progress", "waiting", "done", "cancelled")
CLOSED_STATUSES = frozenset({"done", "cancelled"})
SOURCES = ("manual", "alert", "channel", "plan")
PRIORITIES = ("high", "normal", "low")
KINDS = ("task", "acceptance")
LINK_KINDS = ("task", "troubleshoot", "worklog")
WORK_ITEM_ID_RE = re.compile(r"wi-\d{8}-[0-9a-f]{6}")
PLAN_ID_RE = re.compile(r"plan-\d{8}-[0-9a-f]{6}")
WORKLOG_REF_RE = re.compile(r"\d{4}-\d{2}-\d{2}#\d{1,4}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:#-]{0,159}")
TITLE_MAX = 200
DESCRIPTION_MAX = 4000
NOTE_MAX = 500
SOURCE_REF_MAX = 300
NODE_MAX = 60
# 可经 update 修改的字段;source/source_ref/kind/created_* 建项后不变
EDITABLE = ("title", "description", "status", "assignee", "priority",
            "start_date", "due_date", "plan_id", "plan_node")
_PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}


def _now() -> str:
    return datetime.now(env_timezone()).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(env_timezone()):%Y%m%d}-{secrets.token_hex(3)}"


def _single_line(value: Any, field: str, limit: int, *, required: bool = False) -> Optional[str]:
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        raise DataStoreError(f"{field} 不能包含换行")
    if len(text) > limit:
        raise DataStoreError(f"{field} 不能超过 {limit} 字")
    if required and not text:
        raise DataStoreError(f"{field} 不能为空")
    return text or None


def _choice(value: Any, field: str, choices: Iterable[str]) -> str:
    text = str(value or "").strip()
    if text not in choices:
        raise DataStoreError(f"{field} 只能是 {'/'.join(choices)}")
    return text


def _date(value: Any, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise DataStoreError(f"{field} 必须是 YYYY-MM-DD") from exc


def validate_link(kind: Any, ref: Any) -> tuple[str, str]:
    kind = _choice(kind, "挂载类型", LINK_KINDS)
    text = str(ref or "").strip()
    pattern = WORKLOG_REF_RE if kind == "worklog" else _REF_RE
    if not pattern.fullmatch(text):
        raise DataStoreError(f"{kind} 引用不合法: {ref!r}")
    return kind, text


def sort_items(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """未关闭在前;再按截止(无截止最后)、优先级;同档按创建时间倒序。"""
    newest_first = sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return sorted(newest_first, key=lambda item: (
        item.get("status") in CLOSED_STATUSES,
        item.get("due_date") or "9999-12-31",
        _PRIORITY_RANK.get(str(item.get("priority")), 1),
    ))


class LocalWorkItemStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.plans_root = self.root / "plans"
        self._lock_path = self.root / ".lock"

    # ── 读 ────────────────────────────────────────────────────────────
    def get(self, item_id: str) -> Dict[str, Any]:
        return self._read(self._item_path(item_id), f"工作项不存在: {item_id}")

    def list(self, *, status: Optional[Iterable[str]] = None, include_closed: bool = True,
             plan_id: Optional[str] = None, source: Optional[str] = None,
             assignee: Optional[str] = None, q: Optional[str] = None) -> List[Dict[str, Any]]:
        wanted = set(status) if status else None
        needle = (q or "").strip().lower()
        items = []
        for item in self._all_items():
            if wanted is not None and item.get("status") not in wanted:
                continue
            if not include_closed and item.get("status") in CLOSED_STATUSES:
                continue
            if plan_id is not None and item.get("plan_id") != plan_id:
                continue
            if source is not None and item.get("source") != source:
                continue
            if assignee is not None and item.get("assignee") != assignee:
                continue
            if needle and needle not in f"{item.get('title') or ''}\n{item.get('description') or ''}".lower():
                continue
            items.append(item)
        return sort_items(items)

    def find_link(self, kind: str, ref: str) -> Optional[Dict[str, Any]]:
        kind, ref = validate_link(kind, ref)
        return next(
            (item for item in self._all_items()
             if any(link["kind"] == kind and link["ref"] == ref for link in item.get("links") or [])),
            None,
        )

    def get_plan(self, plan_id: str) -> Dict[str, Any]:
        return self._read(self._plan_path(plan_id), f"计划不存在: {plan_id}")

    def list_plans(self) -> List[Dict[str, Any]]:
        plans = [self._load(path) for path in sorted(self.plans_root.glob("plan-*.json"))]
        return sorted((plan for plan in plans if plan), key=lambda plan: plan.get("created_at") or "", reverse=True)

    # ── 写 ────────────────────────────────────────────────────────────
    def create(self, fields: Mapping[str, Any], *, author: Optional[str] = None) -> Dict[str, Any]:
        with self._lock():
            return self._create_unlocked(fields, author=author)

    def update(self, item_id: str, changes: Mapping[str, Any], *, author: Optional[str] = None,
               note: Optional[str] = None) -> Dict[str, Any]:
        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            raise DataStoreError(f"不可修改的字段: {', '.join(unknown)}")
        note = _single_line(note, "备注", NOTE_MAX)
        with self._lock():
            item = self.get(item_id)
            candidate = {**item, **changes}
            self._normalize(candidate)
            diff = {
                key: [item.get(key), candidate.get(key)]
                for key in EDITABLE if item.get(key) != candidate.get(key)
            }
            if not diff and not note:
                return item
            now = _now()
            if "status" in diff:
                candidate["closed_at"] = now if candidate["status"] in CLOSED_STATUSES else None
            candidate["updated_at"] = now
            action = "status" if set(diff) == {"status"} else "updated"
            candidate["history"] = [*item.get("history", []), self._event(action, author, diff, note)]
            self._write(self._item_path(item_id), candidate)
            return candidate

    def link(self, item_id: str, kind: str, ref: str, *, author: Optional[str] = None) -> Dict[str, Any]:
        kind, ref = validate_link(kind, ref)
        with self._lock():
            item = self.get(item_id)
            for other in self._all_items():
                if any(link["kind"] == kind and link["ref"] == ref for link in other.get("links") or []):
                    if other["id"] == item_id:
                        return item
                    raise WorkItemConflict(f"{kind} {ref} 已挂到工作项 {other['id']}", other["id"])
            now = _now()
            item["links"] = [*item.get("links", []), {
                "kind": kind, "ref": ref, "linked_at": now, "linked_by": self._author(author),
            }]
            item["updated_at"] = now
            item["history"] = [*item.get("history", []),
                               self._event("linked", author, {"link": [None, f"{kind}:{ref}"]})]
            self._write(self._item_path(item_id), item)
            return item

    def unlink(self, item_id: str, kind: str, ref: str, *, author: Optional[str] = None) -> Dict[str, Any]:
        kind, ref = validate_link(kind, ref)
        with self._lock():
            item = self.get(item_id)
            links = [link for link in item.get("links") or [] if not (link["kind"] == kind and link["ref"] == ref)]
            if len(links) == len(item.get("links") or []):
                raise WorkItemNotFound(f"工作项 {item_id} 没有挂载 {kind} {ref}")
            now = _now()
            item["links"] = links
            item["updated_at"] = now
            item["history"] = [*item.get("history", []),
                               self._event("unlinked", author, {"link": [f"{kind}:{ref}", None]})]
            self._write(self._item_path(item_id), item)
            return item

    def create_plan(self, fields: Mapping[str, Any], *, author: Optional[str] = None) -> Dict[str, Any]:
        with self._lock():
            return self._create_plan_unlocked(fields, author=author)

    def update_plan(self, plan_id: str, changes: Mapping[str, Any], *,
                    author: Optional[str] = None) -> Dict[str, Any]:
        unknown = sorted(set(changes) - {"title", "description"})
        if unknown:
            raise DataStoreError(f"不可修改的计划字段: {', '.join(unknown)}")
        with self._lock():
            plan = self.get_plan(plan_id)
            candidate = {**plan, **changes}
            candidate["title"] = _single_line(candidate.get("title"), "计划标题", TITLE_MAX, required=True)
            candidate["description"] = self._description(candidate.get("description"))
            diff = {key: [plan.get(key), candidate.get(key)] for key in ("title", "description")
                    if plan.get(key) != candidate.get(key)}
            if not diff:
                return plan
            candidate["updated_at"] = _now()
            candidate["history"] = [*plan.get("history", []), self._event("updated", author, diff)]
            self._write(self._plan_path(plan_id), candidate)
            return candidate

    def import_rows(self, rows: List[Mapping[str, Any]], *, plan_id: Optional[str] = None,
                    plan_title: Optional[str] = None, author: Optional[str] = None,
                    dry_run: bool = False) -> Dict[str, Any]:
        """导入计划表或批量登记。

        带节点的行进计划(plan_id 已有或 plan_title 新建),按「节点 + 标题 + 类型」匹配已有事项:
        只更新开始/截止,不覆盖状态、不删中台已有项。无节点的行建成不属于计划的单条事项(source=manual)。
        """
        planned = any(row.get("node") for row in rows)
        if planned and not (plan_id or plan_title):
            raise DataStoreError("带项目节点的表格须指定已有计划或新计划标题")
        if not planned and (plan_id or plan_title):
            raise DataStoreError("表格没有项目节点列,不能导入到计划;去掉计划选择即批量登记")
        if any(not row.get("node") for row in rows) and planned:
            raise DataStoreError("计划表每行都须有项目节点")
        with self._lock():
            plan = self.get_plan(plan_id) if plan_id else None
            if planned and plan is None:
                plan = {"id": None, "title": _single_line(plan_title, "计划标题", TITLE_MAX, required=True),
                        "nodes": []}
            existing = {
                (item.get("plan_node"), item.get("title"), item.get("kind")): item
                for item in self._all_items() if plan is not None and plan["id"] and item.get("plan_id") == plan["id"]
            }
            outcome: List[Dict[str, Any]] = []
            nodes = list(plan["nodes"]) if plan is not None else []
            for row in rows:
                node = _single_line(row.get("node"), "项目节点", NODE_MAX) if planned else None
                if node and node not in nodes:
                    nodes.append(node)
                fields = {
                    "title": row.get("title"),
                    "kind": row.get("kind") or "task",
                    "start_date": row.get("start_date"),
                    "due_date": row.get("due_date"),
                }
                match = existing.get((node, _single_line(row.get("title"), "事项", TITLE_MAX), fields["kind"]))
                if match is not None:
                    changes = {key: fields[key] for key in ("start_date", "due_date")
                               if (_date(fields[key], key) or None) != match.get(key)}
                    outcome.append({"action": "update" if changes else "unchanged", "row": row,
                                    "item_id": match["id"], "changes": changes})
                else:
                    outcome.append({"action": "create", "row": row, "fields": {
                        **fields, "plan_node": node, "done": bool(row.get("done")),
                    }})
            summary = {
                "plan": None if plan is None else {**plan, "nodes": nodes},
                "create": sum(1 for item in outcome if item["action"] == "create"),
                "update": sum(1 for item in outcome if item["action"] == "update"),
                "unchanged": sum(1 for item in outcome if item["action"] == "unchanged"),
                "rows": [{"action": item["action"], **dict(item["row"]),
                          "item_id": item.get("item_id")} for item in outcome],
            }
            if dry_run:
                return summary
            if plan is not None and not plan["id"]:
                plan = self._create_plan_unlocked({"title": plan["title"], "nodes": nodes}, author=author)
            elif plan is not None and nodes != plan["nodes"]:
                plan = {**plan, "nodes": nodes, "updated_at": _now(),
                        "history": [*plan.get("history", []),
                                    self._event("imported", author, {"nodes": [plan["nodes"], nodes]})]}
                self._write(self._plan_path(plan["id"]), plan)
            for item in outcome:
                if item["action"] == "update":
                    self._update_dates_unlocked(item["item_id"], item["changes"], author=author)
                elif item["action"] == "create":
                    fields = item["fields"]
                    done = fields.pop("done")
                    created = self._create_unlocked({
                        **fields,
                        "source": "plan" if plan is not None else "manual",
                        "plan_id": plan["id"] if plan is not None else None,
                        "status": "done" if done else "todo",
                    }, author=author, imported=True)
                    item["item_id"] = created["id"]
            summary["plan"] = plan
            summary["rows"] = [{"action": item["action"], **dict(item["row"]),
                                "item_id": item.get("item_id")} for item in outcome]
            return summary

    # ── 内部 ──────────────────────────────────────────────────────────
    def _create_unlocked(self, fields: Mapping[str, Any], *, author: Optional[str],
                         imported: bool = False) -> Dict[str, Any]:
        unknown = sorted(set(fields) - set(EDITABLE) - {"source", "source_ref", "kind"})
        if unknown:
            raise DataStoreError(f"未知字段: {', '.join(unknown)}")
        now = _now()
        author_id = self._author(author)
        item: Dict[str, Any] = {
            "schema_version": 1,
            "id": _new_id("wi"),
            "title": fields.get("title"),
            "description": fields.get("description"),
            "source": fields.get("source") or "manual",
            "source_ref": fields.get("source_ref"),
            "status": fields.get("status") or "todo",
            "assignee": fields.get("assignee") or author_id,
            "priority": fields.get("priority") or "normal",
            "kind": fields.get("kind") or "task",
            "plan_id": fields.get("plan_id"),
            "plan_node": fields.get("plan_node"),
            "start_date": fields.get("start_date"),
            "due_date": fields.get("due_date"),
            "created_by": author_id,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "links": [],
            "history": [],
        }
        self._normalize(item)
        item["source"] = _choice(item["source"], "来源", SOURCES)
        item["kind"] = _choice(item["kind"], "类型", KINDS)
        item["source_ref"] = _single_line(item["source_ref"], "来源引用", SOURCE_REF_MAX)
        if item["source"] in {"alert", "channel"} and not item["source_ref"]:
            raise DataStoreError("告警/渠道来源须带来源引用")
        if item["source"] == "alert":
            for other in self._all_items():
                if other.get("source") == "alert" and other.get("source_ref") == item["source_ref"] \
                        and other.get("status") not in CLOSED_STATUSES:
                    raise WorkItemConflict(f"该告警已有未关闭工作项 {other['id']}", other["id"])
        if item["status"] in CLOSED_STATUSES:
            # 导入时已完成的事项按截止日当天收口,免得整批算进导入那周的「本周完成」
            item["closed_at"] = (f"{item['due_date']}T23:59:59{now[19:]}"
                                 if imported and item["due_date"] else now)
        item["history"] = [self._event("imported" if imported else "created", author, {}, None)]
        self._write(self._item_path(item["id"]), item)
        return item

    def _update_dates_unlocked(self, item_id: str, changes: Mapping[str, Any], *,
                               author: Optional[str]) -> None:
        item = self.get(item_id)
        candidate = {**item, **changes}
        self._normalize(candidate)
        diff = {key: [item.get(key), candidate.get(key)] for key in changes}
        candidate["updated_at"] = _now()
        candidate["history"] = [*item.get("history", []), self._event("imported", author, diff)]
        self._write(self._item_path(item_id), candidate)

    def _create_plan_unlocked(self, fields: Mapping[str, Any], *, author: Optional[str]) -> Dict[str, Any]:
        now = _now()
        nodes = [_single_line(node, "项目节点", NODE_MAX, required=True) for node in fields.get("nodes") or []]
        if len(set(nodes)) != len(nodes):
            raise DataStoreError("项目节点不能重复")
        plan = {
            "schema_version": 1,
            "id": _new_id("plan"),
            "title": _single_line(fields.get("title"), "计划标题", TITLE_MAX, required=True),
            "description": self._description(fields.get("description")),
            "nodes": nodes,
            "created_by": self._author(author),
            "created_at": now,
            "updated_at": now,
            "history": [self._event("created", author, {})],
        }
        self._write(self._plan_path(plan["id"]), plan)
        return plan

    def _normalize(self, item: Dict[str, Any]) -> None:
        item["title"] = _single_line(item.get("title"), "标题", TITLE_MAX, required=True)
        item["description"] = self._description(item.get("description"))
        item["status"] = _choice(item.get("status"), "状态", STATUSES)
        item["priority"] = _choice(item.get("priority"), "优先级", PRIORITIES)
        item["assignee"] = validate_author_id(item["assignee"]) if item.get("assignee") else None
        item["start_date"] = _date(item.get("start_date"), "开始日期")
        item["due_date"] = _date(item.get("due_date"), "截止日期")
        if item["start_date"] and item["due_date"] and item["start_date"] > item["due_date"]:
            raise DataStoreError("开始日期不能晚于截止日期")
        plan_id = item.get("plan_id") or None
        node = _single_line(item.get("plan_node"), "项目节点", NODE_MAX)
        if plan_id is None:
            if node:
                raise DataStoreError("项目节点须属于某个计划")
        else:
            if not PLAN_ID_RE.fullmatch(str(plan_id)):
                raise DataStoreError(f"计划 ID 不合法: {plan_id!r}")
            plan = self.get_plan(plan_id)
            if node and node not in plan.get("nodes", []):
                raise DataStoreError(f"计划 {plan_id} 没有项目节点「{node}」")
        item["plan_id"], item["plan_node"] = plan_id, node

    @staticmethod
    def _description(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if len(text) > DESCRIPTION_MAX:
            raise DataStoreError(f"描述不能超过 {DESCRIPTION_MAX} 字")
        return text or None

    @staticmethod
    def _author(author: Optional[str]) -> Optional[str]:
        return validate_author_id(author) if author else None

    def _event(self, action: str, author: Optional[str], changes: Mapping[str, Any],
               note: Optional[str] = None) -> Dict[str, Any]:
        event = {"ts": _now(), "by": self._author(author), "action": action, "changes": dict(changes)}
        if note:
            event["note"] = note
        return event

    def _all_items(self) -> List[Dict[str, Any]]:
        items = [self._load(path) for path in sorted(self.root.glob("wi-*.json"))]
        return [item for item in items if item]

    def _item_path(self, item_id: str) -> Path:
        if not WORK_ITEM_ID_RE.fullmatch(str(item_id or "")):
            raise WorkItemNotFound(f"工作项不存在: {item_id}")
        return self.root / f"{item_id}.json"

    def _plan_path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.fullmatch(str(plan_id or "")):
            raise WorkItemNotFound(f"计划不存在: {plan_id}")
        return self.plans_root / f"{plan_id}.json"

    @staticmethod
    def _load(path: Path) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _read(self, path: Path, missing: str) -> Dict[str, Any]:
        value = self._load(path)
        if value is None:
            raise WorkItemNotFound(missing)
        return value

    def _write(self, path: Path, value: Mapping[str, Any]) -> None:
        atomic_write_json(path, value)

    def _lock(self) -> FileLock:
        return FileLock(str(self._lock_path), timeout=10)
