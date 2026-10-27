"""图谱:环境包 ``graphs`` 扩展采集的业务拓扑(如作业调度的流程/任务/表依赖),与知识库拓扑分开。

核心只管通用部分:声明解析、快照读取(按 mtime 缓存)、单飞采集执行器与运行状态、周期刷新的声明。
采集器由环境包提供(``模块:函数``,入参 ``GraphCollectContext``,返回写好的图);快照落
``<state>/graphs/<id>/graph-latest.json``,图须带 ``schema_version: 1`` 与 ``topology_kind: <id>``。
可选 ``view`` 段给前端:{core_kinds, default_visible_kinds, optional_kinds, count_kind, count}。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from lib.atomic_file import atomic_write_json
from lib.envpack import EnvPack, EnvPackError
from lib.envpack.extensions import extension_decl, load_object

GRAPH_SCHEMA_VERSION = 1
REFRESH_SCHEDULES = ("weekly",)
_GRAPH_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


class GraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphDecl:
    id: str
    title: str
    collector: str
    refresh: Optional[str] = None           # weekly:每周日 03:00(环境时区)全量刷新
    legacy_state_dir: Optional[str] = None  # 迁移前的状态目录(相对 state/),首次使用时搬到 graphs/<id>


@dataclass(frozen=True)
class GraphCollectContext:
    repo_root: Path
    state_dir: Path
    graph_dir: Path
    mode: str                               # full | incremental


def graph_decls(pack: EnvPack) -> Tuple[GraphDecl, ...]:
    raw = extension_decl(pack, "graphs") or []
    if not isinstance(raw, list):
        raise EnvPackError("extensions.graphs 须为列表")
    decls = []
    for index, item in enumerate(raw):
        where = f"extensions.graphs[{index}]"
        if not isinstance(item, dict) or not item.get("id") or not item.get("collector"):
            raise EnvPackError(f"{where} 须带 id 与 collector")
        graph_id = str(item["id"])
        refresh = item.get("refresh")
        if not _GRAPH_ID_RE.match(graph_id) or (refresh is not None and refresh not in REFRESH_SCHEDULES):
            raise EnvPackError(f"{where}: 非法 id 或 refresh(可选 {', '.join(REFRESH_SCHEDULES)})")
        decls.append(GraphDecl(
            id=graph_id, title=str(item.get("title") or graph_id), collector=str(item["collector"]),
            refresh=refresh, legacy_state_dir=str(item["legacy_state_dir"]) if item.get("legacy_state_dir") else None,
        ))
    if len({item.id for item in decls}) != len(decls):
        raise EnvPackError("extensions.graphs 的 id 重复")
    return tuple(decls)


def graph_dir(state_dir: Path, graph_id: str) -> Path:
    return Path(state_dir).resolve() / "graphs" / graph_id


def graph_path(state_dir: Path, graph_id: str) -> Path:
    return graph_dir(state_dir, graph_id) / "graph-latest.json"


def migrate_legacy(state_dir: Path, decl: GraphDecl) -> None:
    """把迁移前的状态目录(state/<legacy_state_dir>)复制到 state/graphs/<id>;只做一次,目标已在即不动。

    复制而不搬:回退到上一版时它仍读旧目录。中台启动与检索索引重建都会调:升级后 prepare
    先重建索引,不能等中台起来才迁。
    """
    target = graph_dir(state_dir, decl.id)
    if not decl.legacy_state_dir or target.exists():
        return
    legacy = Path(state_dir).resolve() / decl.legacy_state_dir
    if legacy.is_dir():
        staging = target.with_name(f".{target.name}.migrating")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(legacy, staging)
        staging.replace(target)


def read_graph(state_dir: Path, graph_id: str) -> Optional[dict[str, Any]]:
    """最新图;缺失、不可读或不是本图谱的返回 None。"""
    path = graph_path(state_dir, graph_id)
    try:
        stat = path.stat()
    except OSError:
        return None
    key, version = str(path), (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[:2] == version:
            return cached[2]
    graph = _load_json(path, None)
    if not _valid(graph, graph_id):
        return None
    materialized = dict(graph)
    with _CACHE_LOCK:
        _CACHE[key] = (*version, materialized)
    return materialized


class GraphManager:
    """单飞的后台采集执行器:API 手动刷新与周期刷新共用。"""

    def __init__(self, repo_root: Path, state_dir: Path, decl: GraphDecl, pack: EnvPack) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.decl = decl
        self.pack = pack
        self.dir = graph_dir(self.state_dir, decl.id)
        self.runtime_path = self.dir / "runtime.json"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"graph-{decl.id}")
        self._future: Future | None = None
        self._lock = threading.Lock()
        self._stopped = False
        migrate_legacy(self.state_dir, decl)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def status(self) -> dict[str, Any]:
        value = _load_json(self.runtime_path, {})
        result = dict(value) if isinstance(value, Mapping) else {}
        future = self._future
        result["running"] = bool(future and not future.done())
        for key in ("mode", "last_result", "last_error"):
            result.setdefault(key, None)
        graph = _load_json(self.dir / "graph-latest.json", {})
        result["graph"] = {
            key: graph.get(key)
            for key in ("generated_at", "completed_at", "source_fingerprint", "stats", "change_summary")
        } if _valid(graph, self.decl.id) else None
        return result

    def trigger(self, mode: str = "incremental") -> dict[str, Any]:
        if mode not in {"full", "incremental"}:
            raise ValueError("mode must be full or incremental")
        with self._lock:
            if self._stopped:
                raise GraphError(f"{self.decl.title} 图谱服务已停止")
            if self._future and not self._future.done():
                return {**self.status(), "accepted": False, "reason": "already_running"}
            self._future = self._executor.submit(self._run, mode)
            job_id = f"graph-{self.decl.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            atomic_write_json(self.runtime_path, {"job_id": job_id, "state": "running", "mode": mode,
                                                  "started_at": _now(), "last_error": None})
            return {**self.status(), "accepted": True, "job_id": job_id}

    def run_sync(self, mode: str = "full") -> dict[str, Any]:
        return self._run(mode)

    def _run(self, mode: str) -> dict[str, Any]:
        started = _now()
        try:
            collect = load_object(self.pack, self.decl.collector)
            graph = collect(GraphCollectContext(self.repo_root, self.state_dir, self.dir, mode))
            atomic_write_json(self.runtime_path, {
                "state": "ready", "mode": graph.get("update_mode", mode), "started_at": started,
                "completed_at": _now(), "last_error": None, "last_result": graph.get("change_summary"),
            })
            return graph
        except Exception as exc:
            atomic_write_json(self.runtime_path, {"state": "error", "mode": mode, "started_at": started,
                                                  "completed_at": _now(), "last_error": f"{type(exc).__name__}: {exc}"})
            raise


def _valid(graph: Any, graph_id: str) -> bool:
    return isinstance(graph, Mapping) and graph.get("schema_version") == GRAPH_SCHEMA_VERSION \
        and graph.get("topology_kind") == graph_id


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "GRAPH_SCHEMA_VERSION", "GraphCollectContext", "GraphDecl", "GraphError", "GraphManager",
    "graph_decls", "graph_dir", "graph_path", "migrate_legacy", "read_graph",
]
