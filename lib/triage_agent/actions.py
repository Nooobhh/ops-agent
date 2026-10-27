from __future__ import annotations

import http.client
import inspect
import json
import secrets
import shlex
import time
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import unquote

from lib.data_root import resolve_data_root
from lib.datastore import open_datastore
from lib.envpack import EnvPackError, env_timezone, load_env_pack
from lib.kb.bases import resolve_doc_id
from lib.cmd_filter import CommandDenied
from lib.credentials import CredentialStoreError, CredentialUnavailable
from lib.ssh_pool import PushChannelFailed, PushTimeout, SSHConnectFailed
from lib.acquisition.settings import load_settings, resolve_storage_path
from lib.acquisition.store import iter_snapshots, latest_snapshots
from lib.kb.frontmatter import FrontmatterError, load_doc
from lib.kb.recall import RecallIndexError, search_recall_index
from lib.kb.retrieval import RetrievalRequest, RetrievalService
from lib.playbook import SEVERITIES, VERDICTS
from lib.plugins import PluginCallError, PluginNotFound, PluginService
from lib.plugins import approval_token
from lib.plugins.catalog import write_role
from lib.plugins.client import call_tool as call_plugin_tool
from lib.plugins.exposure import normalize_result
from lib.record import TroubleshootSession
from lib.sanitize import redact_secrets
from lib.runner import run_playbook
from lib.triage import (
    iter_alert_entries,
    load_routing,
    match_alert,
)
from lib.worklog import (
    WorklogEntry,
    append_from_troubleshoot,
    entry_from_troubleshoot,
    render_entry,
)
from scripts._common import build_pool_for_platform

from .command_policy import render_pipeline
from .diagnostics import DiagnosticDenied, DiagnosticRegistry, RenderedDiagnostic
from .errors import ActionDenied
from .paths import AppPaths
from .remote_session import (
    RemoteExecutionError,
    TaskRemoteSession,
    remote_error_result,
)
from .targets import is_registered_target, load_target, resolve_node as resolve_registered_node

DIAGNOSTIC_MAX_OUTPUT_BYTES = 1024 * 1024
# 自由只读命令的执行上限:大日志 grep 常超 30s,但仍需有界防挂死
FREE_COMMAND_TIMEOUT_SECONDS = 60
# 裸 shell(新运行时)单条超时,与命令闸常量一致(A2 §3.3)
SHELL_TIMEOUT_SECONDS = 120

__all__ = ["ActionDenied", "ActionGateway", "DIAGNOSTIC_MAX_OUTPUT_BYTES"]


class ActionGateway:
    def __init__(
        self,
        paths: AppPaths,
        diagnostics: DiagnosticRegistry,
        *,
        pool_factory: Optional[Callable[[str], Any]] = None,
    ):
        self.paths = paths
        self.diagnostics = diagnostics
        # records 读写经数据层;根目录沿用 AppPaths(本身即由数据根推导)
        self.datastore = open_datastore(
            paths.repo_root,
            worklog_root=paths.worklog_root,
            troubleshoot_root=paths.records_root,
        )
        self._pool_factory = pool_factory or (
            lambda platform: build_pool_for_platform(platform, paths.repo_root)
        )
        # SSH state belongs to one Task execution, never to the worker or the
        # model client.  ``run_id`` remains a compatibility fallback for
        # direct callers that predate execution_id.
        self._remote_sessions: Dict[str, TaskRemoteSession] = {}

    def start_session(
        self,
        *,
        platform: str,
        node: str,
        trigger: str,
        service: str = "",
        run_id: Optional[str] = None,
        session_type: str = "triage",
        nodes: Optional[List[str]] = None,
        author: Optional[str] = None,
    ) -> TroubleshootSession:
        if not is_registered_target(self.paths.repo_root, platform):
            raise ActionDenied(f"unsupported platform: {platform}")
        if not node:
            raise ActionDenied("node 不能为空")
        try:
            canonical = self.resolve_node(platform, node)
        except ActionDenied:
            # Some unit/integration callers inject an HTTP-only or fake pool
            # without copying topology YAML.  The real SSHPool boundary still
            # validates the node when the first remote action is opened.
            canonical = node
        if canonical is None:
            raise ActionDenied(f"节点 {node} 未在平台 {platform} 登记")
        actual_run_id = run_id or _new_run_id()
        return self.datastore.troubleshoot.start(
            actual_run_id,
            author=author,
            node=canonical,
            trigger=trigger,
            platform=platform,
            service=service,
            session_type=session_type,
            nodes=[self.resolve_node(platform, item) or item for item in nodes] if nodes else nodes,
            report_mode="execution",
        )

    def start_workspace(
        self, *, task_id: str, trigger: str, author: Optional[str] = None,
    ) -> TroubleshootSession:
        """Create a records-compatible workspace for chat/artifact-only Ops tasks."""
        return self.datastore.troubleshoot.start(
            f"op-{task_id}",
            author=author,
            node="local",
            trigger=trigger,
            platform="local",
            service="ops",
            report_mode="execution",
        )

    def update_session_context(
        self,
        run_id: str,
        *,
        platform: str,
        node: str,
        service: str = "",
        session_type: str = "triage",
        nodes: Optional[List[str]] = None,
    ) -> None:
        self._session(run_id).update_context(
            platform=platform,
            node=node,
            service=service,
            session_type=session_type,
            nodes=nodes,
        )

    def create_task_artifact(
        self,
        run_id: str,
        *,
        filename: str,
        content: str,
        purpose: str,
    ) -> Dict[str, Any]:
        if Path(filename).name != filename or Path(filename).suffix not in {".md", ".sh", ".py"}:
            raise ActionDenied("产物文件名或类型不合法")
        _validate_artifact_content(filename, content)
        directory = self.paths.records_root / run_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        stem, suffix = Path(filename).stem, Path(filename).suffix
        candidate = directory / filename
        version = 1
        while candidate.exists():
            version += 1
            candidate = directory / f"{stem}-v{version}{suffix}"
        safe_content = redact_secrets(content)
        candidate.write_text(safe_content, encoding="utf-8")
        return {
            "filename": candidate.name,
            "path": self._display_path(candidate),
            "purpose": purpose,
            "size": candidate.stat().st_size,
            "version": version,
        }

    def _display_path(self, value: Path) -> str:
        try:
            return Path(value).resolve().relative_to(
                resolve_data_root(self.paths.repo_root).resolve()
            ).as_posix()
        except ValueError:
            return str(value)

    def known_nodes(self, platform: str) -> List[str]:
        """平台已登记节点名;写入任务节点前据此校验,避免脏值漏到 SSHPool。"""
        if not is_registered_target(self.paths.repo_root, platform):
            raise ActionDenied(f"unsupported platform: {platform}")
        try:
            return sorted(load_target(self.paths.repo_root, platform).nodes)
        except (OSError, ValueError, KeyError) as exc:
            raise ActionDenied(f"平台拓扑不可读: {platform}") from exc

    def resolve_node(self, platform: str, value: str) -> Optional[str]:
        """Canonicalise a registered node key, IP, alias or FQDN."""
        if not is_registered_target(self.paths.repo_root, platform):
            raise ActionDenied(f"unsupported platform: {platform}")
        try:
            return resolve_registered_node(self.paths.repo_root, platform, value)
        except (OSError, ValueError, KeyError) as exc:
            raise ActionDenied(f"平台拓扑不可读: {platform}") from exc

    def lookup_route(self, alertname: str, platform: str) -> Optional[Dict[str, Any]]:
        # 路由按知识库键编译;告警/任务带的是旧短键
        hit = match_alert(self._routing(), alertname, load_env_pack(self.paths.repo_root).knowledge_key(platform))
        return asdict(hit) if hit is not None else None

    def search_candidates(
        self,
        query: str,
        platform: str,
        service: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        try:
            return [
                asdict(item)
                for item in search_recall_index(
                    self.paths.recall_index_path,
                    query,
                    platform,
                    service,
                )
            ]
        except RecallIndexError as exc:
            raise ActionDenied(str(exc)) from exc

    def build_context(self, request: RetrievalRequest) -> Dict[str, Any]:
        """Build a read-only ContextPack for the selected Agent type."""
        try:
            service = RetrievalService(
                self.paths.repo_root,
                retrieval_index_path=getattr(
                    self.paths,
                    "retrieval_index_path",
                    resolve_data_root(self.paths.repo_root) / "build" / "kb-retrieval.sqlite",
                ),
                recall_index_path=getattr(
                    self.paths,
                    "recall_index_path",
                    resolve_data_root(self.paths.repo_root) / "build" / "kb-recall.sqlite",
                ),
            )
            return service.build_context(request).to_dict()
        except Exception as exc:  # noqa: BLE001
            raise ActionDenied(
                f"ContextPack 构建失败: {type(exc).__name__}: {exc}"
            ) from exc

    def task_route(self, *, process_id: Optional[int] = None, task_id: Optional[int] = None) -> Dict[str, Any]:
        """按流程/任务号经图谱识别类型并组装分组库固定文档(参考信息)。"""
        try:
            service = RetrievalService(
                self.paths.repo_root,
                retrieval_index_path=getattr(
                    self.paths, "retrieval_index_path",
                    resolve_data_root(self.paths.repo_root) / "build" / "kb-retrieval.sqlite",
                ),
            )
            return service.task_route(process_id=process_id, task_id=task_id)
        except Exception as exc:  # noqa: BLE001
            raise ActionDenied(f"任务类型识别失败: {type(exc).__name__}: {exc}") from exc

    def task_dependencies(self, *, process_id: int, task_id: Optional[int] = None) -> Dict[str, Any]:
        """调度任务的读写表与同表上下游(本地图谱,参考信息非运行证据)。"""
        try:
            service = RetrievalService(
                self.paths.repo_root,
                retrieval_index_path=getattr(
                    self.paths, "retrieval_index_path",
                    resolve_data_root(self.paths.repo_root) / "build" / "kb-retrieval.sqlite",
                ),
            )
            return service.task_dependencies(process_id=process_id, task_id=task_id)
        except Exception as exc:  # noqa: BLE001
            raise ActionDenied(f"任务依赖查询失败: {type(exc).__name__}: {exc}") from exc

    def _current_chunk_id(self, chunk_id: str) -> str:
        """历史任务里的 chunk 引用带迁移前文档 ID,映射成现行 ID 再查。"""
        doc_id, sep, section = str(chunk_id).partition("#")
        return resolve_doc_id(self.paths.knowledge_root, doc_id) + sep + section

    def read_kb_chunks(
        self,
        chunk_ids: List[str],
        *,
        max_chars: int = 4000,
    ) -> Dict[str, Any]:
        """Read bounded registered knowledge chunks by stable ID.

        This is deliberately separate from ``read_document``: ContextPack v2
        carries references, while the Agent asks for only the sections it
        selected.  The retrieval index remains the only source of chunk text;
        arbitrary filesystem paths are never accepted here.
        """
        try:
            service = RetrievalService(
                self.paths.repo_root,
                retrieval_index_path=getattr(
                    self.paths,
                    "retrieval_index_path",
                    resolve_data_root(self.paths.repo_root) / "build" / "kb-retrieval.sqlite",
                ),
                recall_index_path=getattr(
                    self.paths,
                    "recall_index_path",
                    resolve_data_root(self.paths.repo_root) / "build" / "kb-recall.sqlite",
                ),
            )
            chunk_ids = [self._current_chunk_id(item) for item in chunk_ids]
            chunks = service.read_chunks(chunk_ids, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001
            raise ActionDenied(
                f"知识 Chunk 读取失败: {type(exc).__name__}: {exc}"
            ) from exc
        requested = list(dict.fromkeys(str(item) for item in chunk_ids))
        found = {str(item.get("chunk_id")) for item in chunks}
        missing = [item for item in requested if item not in found]
        if missing:
            raise ActionDenied(f"知识 Chunk 未登记或不可读: {', '.join(missing[:5])}")
        return {
            "schema_version": 1,
            "kind": "knowledge_chunks",
            "chunks": chunks,
            "reference_only": False,
            "provenance": [
                {
                    "source": "knowledge",
                    "doc_id": item.get("doc_id"),
                    "chunk_id": item.get("chunk_id"),
                }
                for item in chunks
            ],
        }

    def read_document(self, doc: str) -> str:
        doc = resolve_doc_id(self.paths.knowledge_root, doc)
        routing = self._routing()
        registered = {
            item.get("path")
            for item in routing.get("docs") or []
            if isinstance(item, dict)
        }
        if doc not in registered:
            raise ActionDenied(f"知识文档未登记: {doc}")
        path = (self.paths.knowledge_root / doc).resolve()
        try:
            path.relative_to(self.paths.knowledge_root.resolve())
        except ValueError as exc:
            raise ActionDenied("知识文档路径越界") from exc
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ActionDenied(f"知识文档不可读: {doc}") from exc
        return content + self._baseline_resource_overlay(path, content)

    def _baseline_resource_overlay(self, path: Path, content: str) -> str:
        """Append local latest resource observations when reading a baseline.

        The overlay is deliberately computed at read time.  Resource snapshots
        are volatile local state and must not rewrite or get embedded into the
        static Markdown knowledge source.
        """
        try:
            parsed = load_doc(path)
        except (OSError, FrontmatterError):
            return ""
        is_baseline = (
            parsed.profile == "baseline"
            or parsed.raw.get("problem") == "baseline"
            or path.stem.endswith("-baseline")
        )
        if not is_baseline or parsed.platform not in load_env_pack(self.paths.repo_root).cluster_keys("managed"):
            return ""
        try:
            settings = load_settings(self.paths.state_dir)
            root = resolve_storage_path(
                self.paths.repo_root, settings["resources"]["snapshot_root"],
            )
            snapshot = latest_snapshots(root).get(parsed.platform)
        except Exception:
            snapshot = None
        if not snapshot:
            return (
                "\n\n## 当前资源观测\n\n"
                "> 当前没有可用的本地资源快照；本文静态基线不代表当前运行状态。\n"
            )

        service = str(parsed.service or "").lower()
        service_rows = [
            row for row in snapshot.get("services") or []
            if str(row.get("service") or "").lower() == service
            or str(row.get("component") or "").lower() == service
        ]
        node_rows = snapshot.get("nodes") or []
        lines = [
            "",
            "## 当前资源观测",
            "",
            "> 以下内容由本地资源轮询快照在读取时叠加，不写回本文；资源观测状态不等于业务健康结论。",
            "",
            f"- 快照时间：`{snapshot.get('collected_at') or '未知'}`",
            f"- 采集状态：`{snapshot.get('status') or 'unknown'}`",
        ]
        coverage = snapshot.get("coverage") or {}
        if coverage:
            lines.append(
                f"- 节点覆盖：`{coverage.get('observed_nodes', 0)}/"
                f"{coverage.get('expected_nodes', 0)}`；服务实例覆盖：`"
                f"{coverage.get('observed_services', 0)}/"
                f"{coverage.get('expected_services', 0)}`"
            )
        if service_rows:
            lines.extend(["", "### 对应服务实例", "", "```json"])
            lines.append(json.dumps([
                {
                    key: row.get(key)
                    for key in (
                        "node", "component", "job", "role", "status",
                        "sample_at", "metrics", "gc", "old_gen_after_gc_bytes",
                    )
                    if key in row
                }
                for row in service_rows
            ], ensure_ascii=False, sort_keys=True, indent=2))
            lines.append("```")
        else:
            lines.extend([
                "",
                f"> 最新快照中没有 `{service}` 服务实例的资源观测；这表示无数据或未纳入当前 JMX 目录，不能解释为服务健康。",
            ])
        if node_rows:
            relevant_nodes = {
                str(item.get("node")) for item in service_rows if item.get("node")
            }
            selected_nodes = [
                row for row in node_rows
                if not relevant_nodes or str(row.get("node")) in relevant_nodes
            ]
            lines.extend(["", "### 对应节点资源状态", "", "```json"])
            lines.append(json.dumps([
                {
                    key: row.get(key)
                    for key in ("node", "status", "sample_at", "metrics", "filesystems")
                    if key in row
                }
                for row in selected_nodes
            ], ensure_ascii=False, sort_keys=True, indent=2))
            lines.append("```")
        return "\n" + "\n".join(lines) + "\n"

    def run_registered_playbook(
        self,
        *,
        run_id: str,
        platform: str,
        node: str,
        playbook: str,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        routing = self._routing()
        if playbook not in _registered_playbooks(routing):
            raise ActionDenied(f"playbook 未登记: {playbook}")
        try:
            path = load_env_pack(self.paths.repo_root).playbook_path(playbook)
        except EnvPackError as exc:
            raise ActionDenied(f"playbook 路径非法: {exc}") from exc
        if not path.is_file():
            raise ActionDenied(f"playbook 不存在: {playbook}")
        remote = self._remote_session(execution_id or run_id)
        pool = remote.pool(platform)
        was_connected = pool.transport_is_active(node) if hasattr(pool, "transport_is_active") else False
        was_used = remote.is_used(platform, node)
        try:
            result = run_playbook(
                pool,
                node,
                path,
                run_id=run_id,
                timeout=180,
            )
        except (SSHConnectFailed, PushChannelFailed, PushTimeout) as exc:
            raise RemoteExecutionError(
                remote_error_result(
                    "run_registered_playbook", platform, node, exc,
                ),
                exc,
            ) from exc
        remote.mark_used(platform, node)
        result_dict = result.to_session_dict()
        result_dict["schema_version"] = 1
        result_dict["ok"] = True
        result_dict["operation"] = "run_registered_playbook"
        result_dict["target"] = {"platform": platform, "node": node}
        result_dict["connection"] = remote.connection_info(
            platform,
            node,
            was_connected=was_connected,
            was_used=was_used,
            channel="playbook",
        ).to_dict()
        self._session(run_id).record_playbook_result(result_dict)
        return result_dict

    def diagnostic_catalog(
        self, platform: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.diagnostics.catalog(platform)

    def latest_resource_snapshot(
        self,
        platform: str,
        node: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return bounded local polling data for QA before any SSH action."""
        try:
            settings = load_settings(self.paths.state_dir)
            root = resolve_storage_path(
                self.paths.repo_root, settings["resources"]["snapshot_root"],
            )
            snapshot = latest_snapshots(root).get(platform)
        except Exception:  # noqa: BLE001
            snapshot = None
        if not isinstance(snapshot, dict):
            return {"available": False, "platform": platform, "node": node}
        nodes = [
            item for item in snapshot.get("nodes") or []
            if not node or item.get("node") == node
        ]
        services = [
            item for item in snapshot.get("services") or []
            if not node or item.get("node") == node
        ]
        return {
            "available": True,
            "platform": platform,
            "node": node,
            "collected_at": snapshot.get("collected_at"),
            "status": snapshot.get("status"),
            "coverage": snapshot.get("coverage") or {},
            "nodes": nodes[:64],
            "services": services[:128],
            "errors": (snapshot.get("errors") or [])[:32],
        }

    def read_resource_risk(
        self,
        *,
        risk_ref: str,
        platform: str,
        node: str,
        window_hours: int = 24,
        max_samples: int = 24,
    ) -> Dict[str, Any]:
        """Read a target-bound resource-risk finding without exposing paths."""
        if (
            not isinstance(risk_ref, str)
            or not risk_ref.startswith("resource_forecast:")
            or len(risk_ref) > 256
            or any(part in risk_ref for part in ("/", "\\", ".."))
        ):
            raise ActionDenied("resource risk 引用格式不合法")
        try:
            risk_path = self.paths.state_dir / "resource-risks.json"
            payload = json.loads(risk_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ActionDenied("资源风险状态不可读") from exc
        findings = payload.get("findings") if isinstance(payload, dict) else None
        finding = next(
            (
                item for item in findings or []
                if isinstance(item, dict) and item.get("key") == risk_ref
            ),
            None,
        )
        if not isinstance(finding, dict):
            raise ActionDenied("当前资源风险中没有该引用")
        if finding.get("platform") != platform or finding.get("node") != node:
            raise ActionDenied("资源风险引用与任务目标不一致")
        try:
            hours = max(1, min(24 * 90, int(window_hours)))
            samples_limit = max(1, min(256, int(max_samples)))
        except (TypeError, ValueError) as exc:
            raise ActionDenied("资源风险窗口参数不合法") from exc
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        samples = self._resource_risk_samples(
            platform,
            node,
            evidence.get("metric"),
            hours,
        )[-samples_limit:]
        return {
            "schema_version": 1,
            "kind": "resource_risk",
            "risk_ref": risk_ref,
            "finding": {
                key: finding.get(key)
                for key in (
                    "key", "title", "description", "severity", "platform", "node",
                    "resource", "alert_type", "first_observed_at", "last_updated_at",
                    "duration_seconds", "evidence",
                )
                if key in finding
            },
            "samples": samples,
            "window_hours": hours,
            "reference_only": True,
        }

    def _resource_risk_samples(
        self,
        platform: str,
        node: str,
        metric: Any,
        window_hours: int,
    ) -> List[Dict[str, Any]]:
        try:
            settings = load_settings(self.paths.state_dir)
            root = resolve_storage_path(
                self.paths.repo_root, settings["resources"]["snapshot_root"],
            )
            from datetime import timedelta

            since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
            values: List[Dict[str, Any]] = []
            for snapshot in iter_snapshots(root, since=since, platform=platform):
                for item in snapshot.get("business_metrics") or []:
                    if (
                        item.get("node") == node
                        and item.get("metric") == metric
                        and item.get("status") == "ok"
                    ):
                        values.append({
                            key: item.get(key)
                            for key in ("sample_at", "value", "unit", "severity", "status")
                            if key in item
                        })
            return values
        except Exception:  # noqa: BLE001
            return []

    def diagnostic_log_dirs(
        self, platform: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.diagnostics.log_dirs(platform)

    def run_command(
        self,
        run_id: str,
        *,
        platform: str,
        node: str,
        pipeline: List[List[str]],
        approved: bool = False,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """自由命令:安全由 SSHPool.run 唯一出口上的红线判定担保,不经诊断白名单。

        `approved` 只应由运行时在兑现人工批准时传 True——红线 WRITE
        (rm / 服务启停)靠它放行。默认 False:任何没被人看过的命令都当未批准。
        """
        return self._execute_free_command(
            run_id,
            platform=platform,
            node=node,
            cmd=render_pipeline(pipeline),
            approved=approved,
            timeout=FREE_COMMAND_TIMEOUT_SECONDS,
            operation="run_command",
            execution_id=execution_id,
        )

    def run_shell(
        self,
        run_id: str,
        *,
        platform: str,
        node: str,
        command: str,
        approved: bool = False,
        write: bool = True,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """裸 shell(新运行时):准入由 lib/command_guard 判定,SSHPool.run 上的红线照旧兜底。

        write=False:人工批准的是非写命令(未登记只读、凭据读取),步骤不记为 approved,
        不触发「approved 步骤必带 oplog」——oplog 只属于真正的写动作。
        """
        return self._execute_free_command(
            run_id,
            platform=platform,
            node=node,
            cmd=command,
            approved=approved,
            timeout=SHELL_TIMEOUT_SECONDS,
            operation="shell",
            execution_id=execution_id,
            record_approved=approved and write,
        )

    def _execute_free_command(
        self,
        run_id: str,
        *,
        platform: str,
        node: str,
        cmd: str,
        approved: bool,
        timeout: int,
        operation: str,
        execution_id: Optional[str],
        record_approved: Optional[bool] = None,
    ) -> Dict[str, Any]:
        action_started = time.monotonic()
        remote_exec_ms = 0
        session_write_ms = 0
        try:
            remote = self._remote_session(execution_id or run_id)
            pool = remote.pool(platform)
            run_kwargs: Dict[str, Any] = {
                "timeout": timeout,
                "approved": approved,
            }
            if _accepts_max_output_bytes(pool.run):
                run_kwargs["max_output_bytes"] = DIAGNOSTIC_MAX_OUTPUT_BYTES
            remote_started = time.monotonic()
            result, connection = remote.run(platform, node, cmd, **run_kwargs)
            remote_exec_ms = max(0, int((time.monotonic() - remote_started) * 1000))
        except CommandDenied as exc:
            # 红线判为写/违禁:模型换只读写法即可自纠,不是基础设施故障
            raise ActionDenied(
                f"命令被红线拒绝,只读排查请换等价的只读写法: {exc}"
            ) from exc
        except SSHConnectFailed as exc:
            raise RemoteExecutionError(
                remote_error_result(operation, platform, node, exc),
                exc,
            ) from exc
        stdout, gateway_truncated = _bounded_text(result.stdout)
        truncated = bool(getattr(result, "truncated", False)) or gateway_truncated
        stderr = result.stderr
        if truncated:
            stderr = _append_stderr(stderr, _command_truncation_note())
        session_started = time.monotonic()
        self._session(run_id).record_deep_dive_step(
            # 与 run_diagnostic 同约定:cmd 自述执行位置,否则报告里的证据
            # 无法回答「在哪台机看到的」
            cmd=f"[{node}] {cmd}",
            exit_code=result.exit_code if result.exit_code is not None else -1,
            stdout=stdout,
            stderr=stderr,
            approved=approved if record_approved is None else record_approved,
        )
        session_write_ms = max(0, int((time.monotonic() - session_started) * 1000))
        total_ms = max(0, int((time.monotonic() - action_started) * 1000))
        return {
            "schema_version": 1,
            "ok": True,
            "operation": operation,
            "target": {"platform": platform, "node": node},
            "connection": connection.to_dict(),
            "execution": {
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "duration": result.duration,
                "timed_out": result.timed_out,
                "truncated": truncated,
            },
            # Keep the existing top-level fields for old prompt/tool consumers.
            "command": cmd,
            "node": node,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration": result.duration,
            "timed_out": result.timed_out,
            "truncated": truncated,
            "timing": {
                "dispatch_ms": max(0, total_ms - remote_exec_ms - session_write_ms),
                "remote_exec_ms": remote_exec_ms,
                "session_write_ms": session_write_ms,
                "total_ms": total_ms,
            },
        }

    def file_sizes(
        self,
        *,
        platform: str,
        node: str,
        paths: List[str],
        execution_id: str,
    ) -> Dict[str, int]:
        """命令闸重查询防护:执行前查文件字节数。Harness 内部探测,不落 records 步骤。"""
        cmd = "stat -c '%s %n' -- " + " ".join(shlex.quote(path) for path in paths) + " 2>/dev/null"
        result, _connection = self._remote_session(execution_id).run(
            platform, node, cmd, timeout=30, approved=False,
        )
        sizes: Dict[str, int] = {}
        for line in (result.stdout or "").splitlines():
            size, _, name = line.partition(" ")
            if size.isdigit():
                sizes[name] = int(size)
        return sizes

    def run_diagnostic(
        self,
        run_id: str,
        *,
        platform: str,
        node: str,
        diagnostic_id: str,
        params: Dict[str, Any],
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rendered = self.diagnostics.render(
            diagnostic_id,
            params,
            platform=platform,
        )
        if rendered.transport == "ssh":
            return self._run_ssh_diagnostic(
                run_id, platform, node, diagnostic_id, rendered,
                execution_id=execution_id,
            )
        if rendered.transport == "http":
            return self._run_http_diagnostic(
                run_id, platform, diagnostic_id, rendered
            )
        raise DiagnosticDenied(
            f"诊断动作 {diagnostic_id} transport 未知: {rendered.transport}"
        )

    # ---- 插件(MCP 服务):外部系统的只读查询,不经节点、不过命令闸 ----

    def _plugins(self) -> PluginService:
        return PluginService(self.paths.repo_root, self.paths.state_dir)

    def plugin_snapshot(self, platform: Optional[str], *, writable: bool = False) -> Dict[str, Any]:
        return self._plugins().snapshot(platform, writable=writable)

    def plugin_health(self, plugin_id: str) -> Dict[str, Any]:
        service = self._plugins()
        try:
            return service.health(service.registry.get(plugin_id))
        except PluginNotFound:
            return {"state": "protocol_error", "detail": "插件已不存在"}

    def run_plugin_tool(
        self,
        run_id: str,
        *,
        plugin_id: str,
        tool: str,
        arguments: Dict[str, Any],
        content_hash: str,
    ) -> Dict[str, Any]:
        """调用前按当前插件页状态复核(停用、内容变更、缺凭据即拒,不落步骤);
        调用了就落一个 records 步骤,失败也落,结果逐值脱敏后才交模型。"""
        service = self._plugins()
        try:
            plugin = service.registry.get(plugin_id)
        except PluginNotFound:
            raise ActionDenied(f"PLUGIN_DISABLED: 插件 {plugin_id} 已不存在") from None
        if not plugin.enabled or not plugin.tool_enabled(tool, content_hash):
            raise ActionDenied(f"PLUGIN_DISABLED: 插件 {plugin.title} 的工具 {tool} 已在插件页停用或内容已变更")
        try:
            headers = service.headers(plugin)
        except PluginCallError as exc:
            raise ActionDenied(f"{exc.code}: {exc.message}") from None
        started = time.monotonic()
        result, _delivered = self._call_plugin(plugin, headers, tool, arguments)
        truncated = self._record_plugin_step(run_id, plugin, tool, arguments, result)
        return {**result, "duration": time.monotonic() - started, "truncated": truncated}

    # ---- 插件两阶段写:以下只由 Harness 调用,工具名来自环境包声明,模型够不着 ----

    def run_plugin_write_tool(
        self,
        run_id: Optional[str],
        *,
        plugin_id: str,
        role: str,
        arguments: Dict[str, Any],
        record: bool = False,
    ) -> Dict[str, Any]:
        """Harness 侧的只读调用(track / reconcile / impact)。

        不看插件开关:操作已提交后停用插件不能让跟踪与 oplog 断档;只要求插件仍声明了写协议。
        record=True 时与模型调用一样落 records 步骤。
        """
        if role not in {"track", "reconcile", "impact"}:
            raise ActionDenied(f"不支持的写协议角色: {role}")
        service, plugin = self._write_plugin(plugin_id)
        tool = getattr(plugin.write, role)
        if not tool:
            raise ActionDenied(f"PLUGIN_WRITE_UNDECLARED: 插件 {plugin.title} 未声明 {role} 工具")
        try:
            headers = service.headers(plugin)
        except PluginCallError as exc:
            raise ActionDenied(f"{exc.code}: {exc.message}") from None
        result, _delivered = self._call_plugin(plugin, headers, tool, arguments)
        if record and run_id:
            self._record_plugin_step(run_id, plugin, tool, arguments, result)
        return result

    def execute_plugin_operation(
        self,
        run_id: str,
        *,
        plugin_id: str,
        plan_tool: str,
        content_hash: str,
        plan_id: str,
        plan_hash: str,
        approver: str,
    ) -> Dict[str, Any]:
        """人工批准后签发审批令牌并调用 execute;令牌只在本方法内存在,落档只留摘要。

        执行前按插件页现状复核:插件启用、计划工具仍启用且内容未变、已配审批密钥。
        返回值 maybe_delivered=True 表示请求可能已到插件(超时/断开/插件内部错误),须对账。
        """
        service, plugin = self._write_plugin(plugin_id)
        if not plugin.enabled or not plugin.tool_enabled(plan_tool, content_hash) \
                or write_role(plugin, plan_tool) != "plan":
            raise ActionDenied(f"PLUGIN_DISABLED: 插件 {plugin.title} 的写操作计划工具已在插件页停用或内容已变更")
        try:
            secret = service.credentials.plugin_approval_secret(plugin.id)
        except (CredentialUnavailable, CredentialStoreError):
            raise ActionDenied(f"APPROVAL_SECRET_MISSING: 插件 {plugin.title} 未配置审批密钥") from None
        try:
            headers = service.headers(plugin)
        except PluginCallError as exc:
            raise ActionDenied(f"{exc.code}: {exc.message}") from None
        token = approval_token.sign(secret, plan_id, plan_hash)
        token_digest = approval_token.digest(token)
        tool = plugin.write.execute
        result, delivered = self._call_plugin(plugin, headers, tool, {"plan_id": plan_id, "approval_token": token})
        del token, secret
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        code = str((result.get("error") or {}).get("code") or "")
        submitted = bool(result["ok"] and data.get("request_id"))
        # 插件明确拒绝(过期、状态变化、令牌不符、冲突)= 没有发生写动作;其余失败结果未知,按已执行落档
        maybe_delivered = not result["ok"] and (delivered or code in {"RESULT_UNKNOWN", "INTERNAL_ERROR"})
        self._record_plugin_step(
            run_id, plugin, tool, {"plan_id": plan_id}, result,
            approved=submitted or maybe_delivered,
            header=f"审批人: {approver} · 令牌摘要: {token_digest}\n",
        )
        return {**result, "token_digest": token_digest, "maybe_delivered": maybe_delivered}

    def record_plugin_result(
        self, run_id: str, *, plugin_id: str, tool: str, arguments: Dict[str, Any], result: Dict[str, Any],
    ) -> None:
        """把 Harness 已取得的插件结果(如跟踪到终态的那次查询)落成 records 步骤。"""
        _service, plugin = self._write_plugin(plugin_id)
        self._record_plugin_step(run_id, plugin, tool, arguments, result)

    def record_write_operation(self, run_id: str, entry: Dict[str, Any]) -> Path:
        """写操作结论进 session(`write_operations`,有 oplog 时并入 oplogs)并重渲染报告。"""
        session = self._session(run_id)
        session.record_write_operation(entry)
        return session.dir / "report.md"

    def _write_plugin(self, plugin_id: str):
        service = self._plugins()
        try:
            plugin = service.registry.get(plugin_id)
        except PluginNotFound:
            raise ActionDenied(f"PLUGIN_DISABLED: 插件 {plugin_id} 已不存在") from None
        if plugin.write is None:
            raise ActionDenied(f"PLUGIN_WRITE_UNDECLARED: 插件 {plugin.title} 未声明写协议")
        return service, plugin

    @staticmethod
    def _call_plugin(plugin, headers: Dict[str, str], tool: str, arguments: Dict[str, Any]):
        """调用并归一;失败也返回统一形态。第二个返回值:传输失败但请求可能已送达插件(拿到响应即 False)。"""
        try:
            raw = call_plugin_tool(plugin.url, headers, plugin.timeout_seconds, tool, arguments)
            return normalize_result(raw, plugin_id=plugin.id, tool=tool, trusted=plugin.trusted), False
        except PluginCallError as exc:
            return {"plugin": plugin.id, "tool": tool, "trust": "official" if plugin.trusted else "untrusted",
                    "ok": False, "data": None, "warnings": [], "meta": None,
                    "error": {"code": exc.code, "message": exc.message}}, exc.maybe_delivered

    def _record_plugin_step(self, run_id: str, plugin, tool: str, arguments: Dict[str, Any],
                            result: Dict[str, Any], *, approved: bool = False, header: str = "") -> bool:
        stdout, truncated = _bounded_text(header + json.dumps(result, ensure_ascii=False, indent=1))
        error = result.get("error") or {}
        self._session(run_id).record_deep_dive_step(
            # 与其它诊断同约定:cmd 自述执行位置;第三方插件单独标出
            cmd=f"[{'mcp' if plugin.trusted else 'mcp-3p'}:{plugin.id}] {tool} "
                f"{json.dumps(arguments, ensure_ascii=False, sort_keys=True)[:300]}",
            exit_code=0 if result["ok"] else 1,
            stdout=stdout,
            stderr="" if result["ok"] else f"{error.get('code', '')}: {error.get('message', '')}"[:2000],
            approved=approved,
        )
        return truncated

    def _run_ssh_diagnostic(
        self,
        run_id: str,
        platform: str,
        node: str,
        diagnostic_id: str,
        rendered: RenderedDiagnostic,
        *,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        action_started = time.monotonic()
        remote_exec_ms = 0
        session_write_ms = 0
        try:
            remote = self._remote_session(execution_id or run_id)
            pool = remote.pool(platform)
            run_kwargs = {
                "timeout": rendered.timeout_seconds,
                "approved": False,
            }
            if _accepts_max_output_bytes(pool.run):
                run_kwargs["max_output_bytes"] = DIAGNOSTIC_MAX_OUTPUT_BYTES
            remote_started = time.monotonic()
            result, connection = remote.run(
                platform, node, rendered.command, **run_kwargs,
            )
            remote_exec_ms = max(0, int((time.monotonic() - remote_started) * 1000))
        except CommandDenied as exc:
            # 红线拦下的是参数值渲染出的命令,属可自纠范畴;漏成致命类会让
            # 整个排查任务失败,与参数校验的错误分级原则相悖
            raise DiagnosticDenied(
                f"诊断动作 {diagnostic_id} 的参数渲染出的命令被红线拒绝,"
                f"请换用其他参数值: {exc}"
            ) from exc
        except SSHConnectFailed as exc:
            raise RemoteExecutionError(
                remote_error_result("run_diagnostic", platform, node, exc),
                exc,
            ) from exc
        stdout, gateway_truncated = _bounded_text(result.stdout)
        truncated = bool(getattr(result, "truncated", False)) or gateway_truncated
        stderr = result.stderr
        if truncated:
            stderr = _append_stderr(stderr, _truncation_note(diagnostic_id))
        session_started = time.monotonic()
        self._session(run_id).record_deep_dive_step(
            # 跨节点排查后 cmd 必须自述执行位置([node] 前缀与 kquery 同约定),
            # 否则报告里的证据无法回答「在哪台机看到的」
            cmd=f"[{node}] {rendered.command}",
            exit_code=result.exit_code if result.exit_code is not None else -1,
            stdout=stdout,
            stderr=stderr,
            approved=False,
        )
        session_write_ms = max(0, int((time.monotonic() - session_started) * 1000))
        total_ms = max(0, int((time.monotonic() - action_started) * 1000))
        return {
            "schema_version": 1,
            "ok": True,
            "operation": "run_diagnostic",
            "target": {"platform": platform, "node": node},
            "connection": connection.to_dict(),
            "execution": {
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "duration": result.duration,
                "timed_out": result.timed_out,
                "truncated": truncated,
            },
            # Backward-compatible fields used by the current Agent prompts.
            "diagnostic_id": diagnostic_id,
            "command": rendered.command,
            "node": node,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration": result.duration,
            "timed_out": result.timed_out,
            "truncated": truncated,
            "timing": {
                "dispatch_ms": max(0, total_ms - remote_exec_ms - session_write_ms),
                "remote_exec_ms": remote_exec_ms,
                "session_write_ms": session_write_ms,
                "total_ms": total_ms,
            },
        }

    def _run_http_diagnostic(
        self,
        run_id: str,
        platform: str,
        diagnostic_id: str,
        rendered: RenderedDiagnostic,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        timed_out = False
        truncated = False
        try:
            status, body = self._http_get(rendered.url, rendered.timeout_seconds)
            if not 200 <= status < 300:
                stdout = ""
                stderr = f"HTTP {status}: GET {rendered.url} failed"
                exit_code = 1
                truncated = len(body) > DIAGNOSTIC_MAX_OUTPUT_BYTES
            else:
                if len(body) > DIAGNOSTIC_MAX_OUTPUT_BYTES:
                    truncated = True
                    body = body[:DIAGNOSTIC_MAX_OUTPUT_BYTES]
                if truncated:
                    # 丢弃载荷而非回传截断片段:保险丝的立论就是「模型只看前 8000
                    # 字符,MB 级数据无价值」,再把 1MiB 塞进结果与 records 等于
                    # 付全部存储代价存无用数据。留 diagnostic_id 提示够模型改参数了
                    stdout = ""
                    stderr = "HTTP response exceeded diagnostic output limit"
                    exit_code = 1
                else:
                    payload = json.loads(body.decode("utf-8"))
                    extracted = (
                        _extract_json(payload, rendered.extract)
                        if rendered.extract
                        else payload
                    )
                    stdout = json.dumps(extracted, ensure_ascii=False, indent=2)
                    stdout, truncated = _bounded_text(stdout)
                    stderr = ""
                    exit_code = 0
        except (TimeoutError, urlerror.URLError) as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError
            )
            stdout = ""
            kind = "timeout" if timed_out else "connection failed"
            stderr = f"HTTP {kind}: GET {rendered.url}"
            exit_code = 1
        except (
            OSError,
            UnicodeError,
            ValueError,
            KeyError,
            TypeError,
            http.client.IncompleteRead,
        ) as exc:
            stdout = ""
            stderr = f"HTTP response invalid: {type(exc).__name__}"
            exit_code = 1
        stdout = redact_secrets(stdout)
        if truncated:
            stderr = _append_stderr(stderr, _truncation_note(diagnostic_id))
        duration = time.monotonic() - started
        self._session(run_id).record_deep_dive_step(
            cmd=f"[local] {rendered.command}",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            approved=False,
        )
        return {
            "diagnostic_id": diagnostic_id,
            "command": rendered.command,
            "node": "local",
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration": duration,
            "timed_out": timed_out,
            "truncated": truncated,
        }

    def _http_get(self, url: str, timeout: float) -> tuple[int, bytes]:
        # 部分 REST 服务对 Accept: application/json 回 406,*/* 与不带该头均正常;
        # 用 */* 而非省略,避免个别中间件对无 Accept 头的请求另作处理
        req = urlrequest.Request(url, headers={"Accept": "*/*"}, method="GET")
        try:
            # ProxyHandler({}) 显式禁代理:集群 endpoint 一律直连。不改 NO_PROXY
            # 环境变量——那是进程级永久副作用,常驻中台里每次请求都追加会把启动时
            # 设的 NO_PROXY=* 退化成具名列表,反而丢掉 urllib 对 "*" 的快速路径
            opener = urlrequest.build_opener(
                urlrequest.ProxyHandler({}), _NoRedirectHandler()
            )
            with opener.open(req, timeout=timeout) as response:
                return (
                    int(response.status),
                    response.read(DIAGNOSTIC_MAX_OUTPUT_BYTES + 1),
                )
        except urlerror.HTTPError as exc:
            return (
                int(exc.code),
                exc.read(DIAGNOSTIC_MAX_OUTPUT_BYTES + 1),
            )
    def finalize(
        self,
        run_id: str,
        *,
        verdict: str,
        severity: str,
        root_cause: str,
        evidence: List[str],
        remediation: List[str],
        oplogs: Optional[List[str]] = None,
        emit_worklog: bool = True,
        author: Optional[str] = None,
    ) -> Path:
        if verdict not in VERDICTS:
            raise ActionDenied(f"verdict 无效: {verdict}")
        if severity not in SEVERITIES:
            raise ActionDenied(f"severity 无效: {severity}")
        session = self._session(run_id)
        session.finalize(
            verdict=verdict,
            severity=severity,
            root_cause=root_cause,
            evidence=evidence,
            remediation=remediation,
            oplogs=oplogs,
        )
        # 人工任务的 worklog 改走确认闸(emit_worklog=False),自动任务保持直落
        if emit_worklog:
            append_from_troubleshoot(
                session.dir,
                datastore=self.datastore,
                author=author,
            )
        return session.dir / "report.md"

    def record_finish(
        self,
        run_id: str,
        *,
        verdict: str,
        severity: str,
        root_cause: str,
        evidence: List[str],
        remediation: List[str],
        finish: Dict[str, Any],
        oplogs: Optional[List[str]] = None,
    ) -> Path:
        """新运行时收口落报告;续查合并见 TroubleshootSession.record_finish。"""
        if verdict not in VERDICTS:
            raise ActionDenied(f"verdict 无效: {verdict}")
        if severity not in SEVERITIES:
            raise ActionDenied(f"severity 无效: {severity}")
        session = self._session(run_id)
        session.record_finish(
            verdict=verdict,
            severity=severity,
            root_cause=root_cause,
            evidence=evidence,
            remediation=remediation,
            finish=finish,
            oplogs=oplogs,
        )
        return session.dir / "report.md"

    def _record_author(self, author: Optional[str]) -> Optional[str]:
        """记录作者:任务创建人优先,缺省为数据根所有者;预览与落盘同源。"""
        return author or self.datastore.users.owner_id()

    def worklog_preview(
        self, run_id: str, *, kb_doc: Optional[str] = None, author: Optional[str] = None,
    ) -> Optional[str]:
        """渲染本次排查的 worklog 条目文本供人工确认;未收口返回 None。"""
        entry = entry_from_troubleshoot(
            self.datastore.troubleshoot.run_dir(run_id),
            kb_doc=kb_doc,
        )
        if entry is None:
            return None
        entry.author = self._record_author(author)
        return render_entry(entry)

    def worklog_commit(
        self, run_id: str, *, kb_doc: Optional[str] = None, author: Optional[str] = None,
    ) -> Optional[Path]:
        """人工确认后落 worklog;event_key 幂等,重复提交不重复写。"""
        return append_from_troubleshoot(
            self.datastore.troubleshoot.run_dir(run_id),
            kb_doc=kb_doc,
            datastore=self.datastore,
            author=author,
        )

    def render_worklog_entries(
        self,
        entries: List[Dict[str, Any]],
        *,
        status: str,
        source_prefix: str,
        author: Optional[str] = None,
    ) -> str:
        ts = datetime.now(env_timezone())
        author = self._record_author(author)
        rendered = []
        for index, item in enumerate(entries, start=1):
            entry = _worklog_entry(item, status, f"{source_prefix}:{index}", ts)
            entry.author = author
            rendered.append(render_entry(entry).rstrip())
        return "\n".join(rendered) + "\n"

    def commit_worklog_entries(
        self,
        entries: List[Dict[str, Any]],
        *,
        status: str,
        source_prefix: str,
        author: Optional[str] = None,
    ) -> List[str]:
        ts = datetime.now(env_timezone())
        written: List[str] = []
        for index, item in enumerate(entries, start=1):
            source = f"{source_prefix}:{index}"
            entry = _worklog_entry(item, status, source, ts)
            path = self.datastore.worklogs.append(
                entry,
                author=author,
                event_key=source,
            )
            if path is not None:
                written.append(self._display_path(path))
        return written

    def session_data(self, run_id: str) -> Dict[str, Any]:
        path = self.paths.records_root / run_id / "session.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ActionDenied(f"session 不可读: {run_id}") from exc
        if not isinstance(value, dict):
            raise ActionDenied(f"session 结构损坏: {run_id}")
        return value

    def record_turn_summary(self, run_id: str, summary: str) -> Path:
        session = self._session(run_id)
        session.record_turn_summary(summary)
        return session.dir / "report.md"

    def _routing(self) -> Dict[str, Any]:
        try:
            return load_routing(self.paths.routing_path)
        except (OSError, ValueError) as exc:
            raise ActionDenied("routing 索引不可读") from exc

    def _remote_session(self, execution_id: str) -> TaskRemoteSession:
        if not execution_id:
            raise ActionDenied("远程执行缺少 execution_id")
        session = self._remote_sessions.get(execution_id)
        if session is None:
            session = TaskRemoteSession(execution_id, self._pool_factory)
            self._remote_sessions[execution_id] = session
        return session

    def close_task_session(self, execution_id: str) -> None:
        session = self._remote_sessions.pop(execution_id, None)
        if session is not None:
            session.close()

    def close_all_task_sessions(self) -> None:
        sessions = list(self._remote_sessions.values())
        self._remote_sessions.clear()
        for session in sessions:
            session.close()

    def has_task_sessions(self) -> bool:
        return bool(self._remote_sessions)

    def close_task_sessions_except(self, execution_ids: set[str]) -> None:
        """Reap sessions whose persisted Task is no longer active."""
        stale = [
            execution_id
            for execution_id in self._remote_sessions
            if execution_id not in execution_ids
        ]
        for execution_id in stale:
            self.close_task_session(execution_id)

    def _pool(self, platform: str, execution_id: Optional[str] = None):
        """Compatibility accessor; new remote paths must use TaskRemoteSession."""
        if not is_registered_target(self.paths.repo_root, platform):
            raise ActionDenied(f"unsupported platform: {platform}")
        return self._remote_session(execution_id or "legacy").pool(platform)

    def _session(self, run_id: str) -> TroubleshootSession:
        directory = self.paths.records_root / run_id
        if not (directory / "session.json").is_file():
            raise ActionDenied(f"session 不存在: {run_id}")
        return TroubleshootSession(
            dir=directory, run_id=run_id, data_root=resolve_data_root(self.paths.repo_root),
        )


def _registered_playbooks(routing: Dict[str, Any]) -> set:
    registered = {
        item.get("playbook")
        for item in iter_alert_entries(routing)
        if item.get("playbook")
    }
    registered.update(
        item.get("playbook")
        for item in routing.get("docs") or []
        if item.get("playbook")
    )
    for item in [*iter_alert_entries(routing), *(routing.get("docs") or [])]:
        for ref in item.get("playbooks") or []:
            path = ref.get("path") if isinstance(ref, dict) else ref
            if path:
                registered.add(path)
        for path in _paths_from_diagnostic_paths(item.get("diagnostic_paths")):
            registered.add(path)
    return registered


def _paths_from_diagnostic_paths(paths: Any) -> set[str]:
    result: set[str] = set()
    for path in paths or []:
        if not isinstance(path, dict):
            continue
        refs = path.get("playbooks") or []
        for ref in refs:
            value = ref.get("path") if isinstance(ref, dict) else ref
            if value:
                result.add(str(value))
    return result


def _worklog_entry(
    item: Dict[str, Any],
    status: str,
    source: str,
    ts: datetime,
) -> WorklogEntry:
    category = str(item.get("category") or "OPS").upper()
    if category not in {"BUGFIX", "FEATURE", "RESEARCH", "OPS", "DOC", "MEETING"}:
        category = "OPS"
    process = [str(value).strip()[:20] for value in item.get("process") or [] if str(value).strip()]
    return WorklogEntry(
        status=status,
        category=category,
        title=str(item.get("title") or "Ops 任务进展").strip()[:160],
        source=source,
        output=str(item.get("output") or "-").strip()[:1000] or "-",
        related=str(item.get("related") or "-").strip()[:1000] or "-",
        process=process[:8] or ["完成本轮任务处理"],
        ts=ts,
    )


_ARTIFACT_FORBIDDEN = re.compile(
    r"(?im)(?:^|[;&|]\s*)"
    r"(?:rm\s+-|shutdown\b|reboot\b|poweroff\b|halt\b|"
    r"systemctl\s+(?:start|stop|restart|enable|disable)|"
    r"service\s+\S+\s+(?:start|stop|restart)|"
    r"sed\s+[^\n]*\s-i\b|mkfs\b|fdisk\b|iptables\b|user(?:add|del|mod)\b)"
)


def _validate_artifact_content(filename: str, content: str) -> None:
    """Conservative guard for generated scripts; artifacts are never executed here."""
    if "\x00" in content:
        raise ActionDenied("产物内容包含非法空字节")
    if Path(filename).suffix in {".sh", ".py"} and _ARTIFACT_FORBIDDEN.search(content):
        raise ActionDenied("脚本包含服务控制、删除或配置变更类命令，不能生成")


def _extract_json(payload: Any, path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("extract path must start with /")
    segments = path[1:].split("/")
    values = [payload]
    for segment in segments:
        if segment == "":
            continue
        if segment == "*":
            expanded = []
            for value in values:
                if isinstance(value, list):
                    expanded.extend(value)
                elif isinstance(value, dict):
                    expanded.extend(value.values())
            values = expanded
            continue
        if segment.startswith("type="):
            expected = unquote(segment.removeprefix("type="))
            matched = []
            for value in values:
                candidates = value if isinstance(value, list) else [value]
                matched.extend(
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and candidate.get("type") == expected
                )
            values = matched
            continue
        key = unquote(segment)
        values = [
            value[key]
            for value in values
            if isinstance(value, dict) and key in value
        ]
        if not values:
            raise KeyError(key)
    if not values:
        raise ValueError("extract path matched no values")
    extracted = values[0] if len(values) == 1 else values
    # 末段落在 properties 下时回包成 {key: value},让按配置项取值的 extract
    # 结果自带键名(否则模型只看到一个裸值,不知道是哪个参数的)。
    # 已知局限:这是按字面量段名匹配的特例,任何 extract 路径倒数第二段恰好叫
    # properties 的新诊断都会被同样改写形状。新增此类诊断时要么避开该段名,
    # 要么把包装挪到调用点。tests/triage_agent/test_http_diagnostics.py 有钉住用例。
    if (
        len(segments) >= 2
        and segments[-1]
        and segments[-2] == "properties"
    ):
        return {unquote(segments[-1]): extracted}
    return extracted


def _accepts_max_output_bytes(run: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(run).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "max_output_bytes"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _bounded_text(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= DIAGNOSTIC_MAX_OUTPUT_BYTES:
        return text, False
    bounded = encoded[:DIAGNOSTIC_MAX_OUTPUT_BYTES].decode("utf-8", "replace")
    return bounded + "\n[diagnostic output truncated at 1 MiB]\n", True


def _truncation_note(diagnostic_id: str) -> str:
    return (
        f"diagnostic {diagnostic_id} output truncated at 1 MiB; "
        "add a tighter head/tail limit to its definition"
    )


def _command_truncation_note() -> str:
    return (
        "command output truncated at 1 MiB; "
        "narrow it with grep/head/tail and run again"
    )


def _append_stderr(stderr: str, note: str) -> str:
    return f"{stderr.rstrip()}\n{note}".lstrip()


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"ts-{stamp}-{secrets.token_hex(2)}"
