"""单个用例的回放驱动:独立 state-dir 建任务 → worker 推进 → 代替人回复与审批。"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lib.agent_replay.shell_safety import check_shell
from lib.cmd_filter import CmdFilter
from lib.envpack import active_env_pack
from lib.triage_agent.model_client import ModelConfig, OpenAICompatibleClient
from lib.triage_agent.model_registry import ModelRegistry
from lib.triage_agent.models import TaskSource
from lib.triage_agent.paths import AppPaths
from lib.triage_agent.service import CreateTaskRequest, TriageTaskService
from lib.triage_agent.targets import infer_target_for_node, resolve_node, target_nodes
from lib.triage_agent.task_classifier import TaskClassifier
from lib.triage_agent.task_store import TaskStore
from scripts.triage_worker import build_worker

from .gateway import ReplayActionGateway, ReplayKnowledgeSink
from .metrics import TERMINAL, case_metrics, skeleton, skeleton_divergence
from .recordings import Recording, RecordingPool, SignatureMap
from .transport import RecordingTransport, ScriptedTransport, usage_totals

MAX_STEPS = 80
REPORT_GATE_FEEDBACK = "回放驱动:证据须为采证输出里的连续原文片段,请据此修正后重新收口"
_WS = re.compile(r"\s+")


def prepare_state(fixtures_dir: Path, state_dir: Path) -> None:
    source = Path(fixtures_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = state_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.parts[0] == "graphs":
            target.symlink_to(path.resolve())  # 几十 MB 的只读图谱,各用例共享一份
        else:
            shutil.copy2(path, target)


def _platform_reply(key: str) -> str:
    """代人回答平台澄清:托管集群照人工习惯答短键,其余答环境包里的显示名。"""
    cluster = active_env_pack().cluster(key) if key else None
    if cluster is None or "managed" in cluster.roles:
        return key
    return cluster.display_name


def _context_reply(case: Dict[str, Any]) -> str:
    explicit = case["interaction"].get("context_reply")
    if explicit:
        return str(explicit)
    source = (case.get("labels") or {}).get("source") or {}
    platform = _platform_reply(str(source.get("platform") or ""))
    if source.get("scope") == "cluster" or not source.get("node"):
        return f"{platform} 整个集群".strip()
    return f"{platform} {source['node']}".strip()


def _pending_request(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    request = None
    for event in events:
        if event["type"] == "user_message":
            request = None
        elif event["type"] in {
            "task_type_clarification_requested",
            "task_context_clarification_requested",
            "agent_handoff_requested",
        }:
            request = event
    return request


def _pending_approval(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    decided = {
        event["payload"].get("approval_id")
        for event in events if event["type"] == "approval_decided"
    }
    for event in reversed(events):
        if event["type"] == "approval_requested" and event["payload"].get("approval_id") not in decided:
            return event["payload"]
    return None


class ReplayDriver:
    def __init__(
        self,
        case: Dict[str, Any],
        *,
        repo_root: Path,
        out_dir: Path,
        fixtures_dir: Path,
        model_config: Path,
        signature_map: Optional[SignatureMap] = None,
        script: Optional[Dict[str, Any]] = None,
        max_seconds: float = 3600,
        model_inner_transport=None,
        live: bool = False,
        actions_factory=None,
    ):
        self.case = case
        self.repo_root = Path(repo_root).resolve()
        self.out_dir = Path(out_dir)
        self.script = script
        self.max_seconds = max_seconds
        state_dir = self.out_dir / "state"
        prepare_state(Path(fixtures_dir), state_dir)
        records = self.out_dir / "records"
        self.paths = dataclasses.replace(
            AppPaths.from_repo_root(self.repo_root, state_dir=state_dir),
            records_root=records / "troubleshoot",
            worklog_root=records / "worklog",
            model_config=Path(model_config),
        )
        self.paths.records_root.mkdir(parents=True, exist_ok=True)
        self.paths.worklog_root.mkdir(parents=True, exist_ok=True)
        self.pool = RecordingPool(
            [Recording.from_dict(item) for item in case.get("recordings") or []],
            signature_map=signature_map,
            snapshot_fallback=case["create"].get("polling_snapshot"),
        )
        if script is None:
            # model_inner_transport 仅供测试替换真实网络
            self.worker_transport = RecordingTransport(model_inner_transport)
            self.classifier_transport = RecordingTransport(model_inner_transport)
        else:
            self.worker_transport = ScriptedTransport(script.get("worker") or [])
            self.classifier_transport = ScriptedTransport(script.get("classifier") or [])
        self.sink = ReplayKnowledgeSink(self.paths)
        if actions_factory is None and not live:
            actions_factory = lambda paths, diagnostics: ReplayActionGateway(  # noqa: E731
                paths, diagnostics, recordings=self.pool,
            )
        self.worker = build_worker(
            self.paths,
            # live=True(A1 实况对比):不注入回放网关,走生产同款 ActionGateway/SSHPool;
            # actions_factory 显式给出时(测试注入假连接池)优先
            actions_factory=actions_factory,
            knowledge_sink=self.sink,
            model_transport=self.worker_transport,
        )
        self.store = TaskStore(self.paths.tasks_root)
        self.registry = ModelRegistry(self.paths.model_config)
        self.service = TriageTaskService(
            self.store,
            model_registry=self.registry,
            classifier=TaskClassifier(
                routing_path=self.paths.routing_path, repo_root=self.repo_root,
            ),
            classification_model_factory=self._classification_model,
            polling_snapshot_reader=self._polling_snapshot,
        )
        self._cmd_filter = CmdFilter.default()
        self.interactions: List[Dict[str, Any]] = []
        self.used: Counter = Counter()

    # ---- 与 create_app / 路由同款接线 -------------------------------------------------
    def _classification_model(self, selection):
        material = self.registry.client_material_for_selection(selection)
        return OpenAICompatibleClient(
            ModelConfig(
                base_url=material.base_url,
                api_key=material.api_key,
                model=str(selection["model_name"]),
                reasoning_effort=str(selection["reasoning_effort"]),
                timeout_seconds=min(material.timeout_seconds, 60),
                max_tool_turns=1,
            ),
            context={"client_role": "task_classifier"},
            transport=self.classifier_transport,
        )

    def _polling_snapshot(self, platform: str, node: Optional[str] = None):
        snapshot = self.case["create"].get("polling_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("platform") == platform:
            return snapshot
        return None

    def _target_kwargs(self) -> Dict[str, Any]:
        root = self.repo_root
        return {
            "resolve_node": lambda platform, node: resolve_node(root, platform, node),
            "infer_platform": lambda node: infer_target_for_node(root, node),
            "known_nodes": lambda platform: target_nodes(root, platform),
        }

    def _create(self) -> str:
        create = self.case["create"]
        if create["kind"] == "auto":
            detail, _created = self.service.enqueue_auto(
                f"replay-{self.case['case_id']}", dict(create["alert_event"]),
            )
            return detail.task.task_id
        request = CreateTaskRequest(
            message=create["message"],
            platform=create.get("platform"),
            node=create.get("node"),
            manual_artifacts=bool(create.get("manual_artifacts")),
            alert_context=create.get("alert_context"),
            task_type="investigation" if create["kind"] == "risk" else None,
            risk_seed=create.get("risk_seed") if create["kind"] == "risk" else None,
        )
        return self.service.create_manual(request, **self._target_kwargs()).task.task_id

    # ---- 代替人的决定 ----------------------------------------------------------------
    def _decide(self, approval: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
        kind = approval.get("kind")
        interaction = self.case["interaction"]
        if kind == "command":
            command = _WS.sub(" ", str(approval.get("command") or "")).strip()
            if command in (interaction.get("command_rejects") or []):
                return "reject", None, None
            # 只替人批准只读命令:写/凭据/外连按精简档同一安全边界判,一律拒
            if not check_shell(command, self._cmd_filter).allowed:
                return "reject", None, None
            return "approve", None, "once"
        if kind == "budget":
            if self.used["budget"] < int(interaction.get("budget_grants") or 0):
                self.used["budget"] += 1
                return "approve", None, None
            return "reject", None, None
        if kind == "report_gate" and self.used["report_gate"] == 0:
            self.used["report_gate"] += 1
            return "revise", REPORT_GATE_FEEDBACK, None
        # kb_proposal / kb_draft / worklog 及其余:一律不落档
        return "reject", None, None

    def _reply(self, events: List[Dict[str, Any]]) -> Optional[str]:
        request = _pending_request(events)
        interaction = self.case["interaction"]
        if request is not None:
            kind = request["type"]
            payload = request["payload"]
            self.used[kind] += 1
            if self.used[kind] > 3:
                return None  # 同类澄清反复出现说明卡住了,不再陪跑
            options = [str(item) for item in payload.get("options") or []]
            if kind == "task_type_clarification_requested":
                expected = str((self.case.get("expect") or {}).get("task_type") or "")
                chosen = next((item for item in options if expected and item.endswith(expected)), None)
                return chosen or (options[0] if options else "采用程序判断")
            if kind == "task_context_clarification_requested":
                return _context_reply(self.case)
            controls = interaction.get("control_replies") or []
            if self.used["control_scripted"] < len(controls):
                self.used["control_scripted"] += 1
                return controls[self.used["control_scripted"] - 1]
            preferred = next((item for item in options if "InvestigationAgent" in item), None)
            return preferred or (options[0] if options else None)
        followups = interaction.get("followups") or []
        if self.used["followup"] < len(followups):
            self.used["followup"] += 1
            return followups[self.used["followup"] - 1]
        return None

    def _events(self, task_id: str) -> List[Dict[str, Any]]:
        return [event.model_dump(mode="json") for event in self.store.events(task_id)]

    def run(self) -> Dict[str, Any]:
        started = time.monotonic()
        driver_error: Optional[str] = None
        task_id: Optional[str] = None
        try:
            task_id = self._create()
            for _step in range(MAX_STEPS):
                task = self.store.get(task_id)
                status = task.status.value
                if status in TERMINAL:
                    break
                if time.monotonic() - started > self.max_seconds:
                    driver_error = "timeout"
                    break
                if status == "queued":
                    if not self.worker.run_once():
                        driver_error = "worker_idle"
                        break
                    continue
                if status == "waiting_approval":
                    approval = _pending_approval(self._events(task_id))
                    if approval is None:
                        driver_error = "approval_missing"
                        break
                    decision, feedback, scope = self._decide(approval)
                    self.interactions.append({"approval": approval.get("kind"), "decision": decision})
                    self.service.decide_approval(
                        task_id, str(approval["approval_id"]),
                        decision=decision, feedback=feedback, scope=scope,
                    )
                    continue
                if status == "waiting_input":
                    reply = self._reply(self._events(task_id)) if task.source is TaskSource.MANUAL else None
                    if reply is None:
                        if task.source is TaskSource.MANUAL:
                            self.service.complete(task_id)
                            self.interactions.append({"finish": True})
                        break
                    self.interactions.append({"reply": reply})
                    self.service.add_message(task_id, reply, **self._target_kwargs())
                    continue
                driver_error = f"unexpected_status:{status}"
                break
            else:
                driver_error = "max_steps"
        except Exception as exc:  # noqa: BLE001 — 驱动自身故障也要落结果,不中断整轮评估
            driver_error = f"{type(exc).__name__}: {exc}"[:300]
        return self._collect(task_id, driver_error, time.monotonic() - started)

    def _collect(self, task_id: Optional[str], driver_error: Optional[str], wall: float) -> Dict[str, Any]:
        task: Dict[str, Any] = {}
        events: List[Dict[str, Any]] = []
        invocations: Dict[str, Any] = {}
        session = None
        if task_id:
            task = self.store.get(task_id).model_dump(mode="json")
            events = self._events(task_id)
            invocations_path = self.paths.tasks_root / task_id / "invocations.json"
            if invocations_path.is_file():
                invocations = json.loads(invocations_path.read_text(encoding="utf-8"))
            if task.get("run_id"):
                session_path = self.paths.records_root / task["run_id"] / "session.json"
                if session_path.is_file():
                    session = json.loads(session_path.read_text(encoding="utf-8"))
        responses = {"worker": [], "classifier": []}
        if isinstance(self.worker_transport, RecordingTransport):
            responses = {
                "worker": self.worker_transport.responses,
                "classifier": self.classifier_transport.responses,
            }
        metrics = case_metrics(
            self.case,
            task=task,
            events=events,
            invocations=invocations,
            session=session,
            lookups=self.pool.lookups,
            usage=usage_totals(responses["worker"] + responses["classifier"]),
            kb_attempts=len(self.sink.attempts),
            driver_error=driver_error,
            wall_seconds=wall,
        )
        events_skeleton = skeleton(events)
        a0 = None
        if self.script is not None:
            divergence = skeleton_divergence(list(self.script.get("skeleton") or []), events_skeleton)
            a0 = {
                "divergence": divergence,
                "script_exhausted": bool(
                    getattr(self.worker_transport, "exhausted", False)
                    or getattr(self.classifier_transport, "exhausted", False)
                ),
                "served": self.worker_transport.served + self.classifier_transport.served,
            }
            if divergence is not None:
                metrics["hard_failures"].append("a0_divergence")
        result = {
            "case_id": self.case["case_id"],
            "task_id": task_id,
            "metrics": metrics,
            "skeleton": events_skeleton,
            "interactions": self.interactions,
            "kb_attempts": self.sink.attempts,
            "a0": a0,
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        if self.script is None:
            (self.out_dir / "model-responses.json").write_text(
                json.dumps(responses, ensure_ascii=False), encoding="utf-8",
            )
        return result
