"""回放用 ActionGateway:前置校验照跑,只把环境 I/O 换成录制结果。"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.cmd_filter import CmdFilter, CommandDenied
from lib.envpack import EnvPackError, load_env_pack
from lib.triage_agent.actions import ActionDenied, ActionGateway, _registered_playbooks
from lib.triage_agent.command_policy import render_pipeline
from lib.triage_agent.knowledge_sink import KnowledgeResult, KnowledgeSink

from .recordings import RecordingPool

MISS_NOTE = "回放环境无此调用的录制结果(REPLAY_NO_RECORDING)"


class ReplayLiveAccessError(RuntimeError):
    """回放期间任何真实环境访问都是缺陷,不是模型可自纠的业务驳回。"""


def _deny_live(*_args, **_kwargs):
    raise ReplayLiveAccessError("回放模式禁止访问真实集群与数据库")


def _miss_command_result(operation: str, platform: str, node: str, command: str, **extra) -> Dict[str, Any]:
    # 未命中不抛错:抛错会进连续驳回计数甚至直接 fail 任务,把「轨迹偏离录制」
    # 误判成「harness 驳回」;也不写 session 步骤——没拿到数据就不该算采证
    return {
        "schema_version": 1,
        "ok": True,
        "operation": operation,
        "target": {"platform": platform, "node": node},
        "command": command,
        "node": node,
        "exit_code": -1,
        "stdout": "",
        "stderr": MISS_NOTE,
        "duration": 0.0,
        "timed_out": False,
        "truncated": False,
        **extra,
    }


def _exit_code(result: Dict[str, Any]) -> int:
    value = result.get("exit_code")
    if value is None and isinstance(result.get("execution"), dict):
        value = result["execution"].get("exit_code")
    return int(value) if value is not None else -1


def _text(result: Dict[str, Any], key: str) -> str:
    value = result.get(key)
    if value is None and isinstance(result.get("execution"), dict):
        value = result["execution"].get(key)
    return str(value or "")


class ReplayActionGateway(ActionGateway):
    def __init__(self, paths, diagnostics, *, recordings: RecordingPool):
        super().__init__(paths, diagnostics, pool_factory=_deny_live)
        self.recordings = recordings
        self._cmd_filter = CmdFilter.default()

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
        # 登记与参数校验照常走注册表:未登记诊断的驳回是被测行为
        rendered = self.diagnostics.render(diagnostic_id, params, platform=platform)
        recording, _ = self.recordings.match(
            "run_diagnostic",
            {"diagnostic_id": diagnostic_id, "params": params},
            platform=platform,
            node=node,
        )
        if recording is None:
            return _miss_command_result(
                "run_diagnostic", platform, node, rendered.command,
                diagnostic_id=diagnostic_id,
            )
        result = copy.deepcopy(recording.result)
        self._session(run_id).record_deep_dive_step(
            cmd=self._diagnostic_step_cmd(rendered, node, params),
            exit_code=_exit_code(result),
            stdout=_text(result, "stdout"),
            stderr=_text(result, "stderr"),
            approved=False,
        )
        return result

    def plugin_snapshot(self, platform: Optional[str], *, writable: bool = False) -> Dict[str, Any]:
        # 回放不接插件:工具清单与录制时一致,结果不依赖本机插件配置
        return {"version": 1, "plugins": [], "tools": [], "notices": []}

    def plugin_health(self, plugin_id: str) -> Dict[str, Any]:
        _deny_live()

    def run_plugin_tool(self, run_id: str, **_kwargs) -> Dict[str, Any]:
        _deny_live()

    def run_plugin_write_tool(self, run_id: Optional[str], **_kwargs) -> Dict[str, Any]:
        _deny_live()

    def execute_plugin_operation(self, run_id: str, **_kwargs) -> Dict[str, Any]:
        _deny_live()

    def record_plugin_result(self, run_id: str, **_kwargs) -> None:
        _deny_live()

    @staticmethod
    def _diagnostic_step_cmd(rendered, node: str, params: Dict[str, Any]) -> str:
        # 与 ActionGateway 各 transport 的步骤自述格式一致(仅影响报告可读性)
        if rendered.transport == "http":
            return f"[local] {rendered.command}"
        return f"[{node}] {rendered.command}"

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
        cmd = render_pipeline(pipeline)
        try:
            # 生产里红线在 SSHPool.run 入口;回放没有 pool,在这里用同一份规则判
            self._cmd_filter.check(cmd, approved=approved)
        except CommandDenied as exc:
            raise ActionDenied(
                f"命令被红线拒绝,只读排查请换等价的只读写法: {exc}"
            ) from exc
        recording, _ = self.recordings.match(
            "run_command", {"command": cmd}, platform=platform, node=node,
        )
        if recording is None:
            return _miss_command_result("run_command", platform, node, cmd)
        result = copy.deepcopy(recording.result)
        self._session(run_id).record_deep_dive_step(
            cmd=f"[{node}] {cmd}",
            exit_code=_exit_code(result),
            stdout=_text(result, "stdout"),
            stderr=_text(result, "stderr"),
            approved=approved,
        )
        return result

    def run_registered_playbook(
        self,
        *,
        run_id: str,
        platform: str,
        node: str,
        playbook: str,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if playbook not in _registered_playbooks(self._routing()):
            raise ActionDenied(f"playbook 未登记: {playbook}")
        try:
            path = load_env_pack(self.paths.repo_root).playbook_path(playbook)
        except EnvPackError as exc:
            raise ActionDenied(f"playbook 路径非法: {exc}") from exc
        if not path.is_file():
            raise ActionDenied(f"playbook 不存在: {playbook}")
        recording, _ = self.recordings.match(
            "run_registered_playbook", {"playbook": playbook},
            platform=platform, node=node,
        )
        if recording is None:
            # Alert 预跑 playbook 抛错会直接 fail 任务,未命中按「证据不足」返回
            return {
                "schema_version": 1,
                "ok": True,
                "operation": "run_registered_playbook",
                "target": {"platform": platform, "node": node},
                "playbook": Path(playbook).name,
                "node": node,
                "verdict": "insufficient",
                "severity": None,
                "conclusion_hint": MISS_NOTE,
                "steps": [],
                "metrics": {},
                "next_playbook": None,
                "raw_dir": None,
            }
        result = copy.deepcopy(recording.result)
        self._session(run_id).record_playbook_result(result)
        return result

    def read_resource_risk(
        self,
        *,
        risk_ref: str,
        platform: str,
        node: str,
        window_hours: int = 24,
        max_samples: int = 24,
    ) -> Dict[str, Any]:
        if (
            not isinstance(risk_ref, str)
            or not risk_ref.startswith("resource_forecast:")
            or len(risk_ref) > 256
            or any(part in risk_ref for part in ("/", "\\", ".."))
        ):
            raise ActionDenied("resource risk 引用格式不合法")
        try:
            hours = max(1, min(24 * 90, int(window_hours)))
            limit = max(1, min(256, int(max_samples)))
        except (TypeError, ValueError) as exc:
            raise ActionDenied("资源风险窗口参数不合法") from exc
        recording, _ = self.recordings.match(
            "read_resource_risk", {"risk_ref": risk_ref}, platform=platform, node=node,
        )
        if recording is None:
            return {
                "schema_version": 1,
                "kind": "resource_risk",
                "risk_ref": risk_ref,
                "finding": {},
                "samples": [],
                "window_hours": hours,
                "reference_only": True,
            }
        result = copy.deepcopy(recording.result)
        result["samples"] = list(result.get("samples") or [])[-limit:]
        result["window_hours"] = hours
        return result

    def latest_resource_snapshot(self, platform: str, node: Optional[str] = None) -> Dict[str, Any]:
        # harness 每轮也会读一次快照拼 AgentTurnInput;录制优先,
        # 否则退回任务创建时的快照,都没有才算不可用
        recording, _ = self.recordings.match(
            "read_resource_snapshot", {}, platform=platform, node=node,
            miss_kind="fallback",
        )
        if recording is not None:
            return {
                key: value for key, value in copy.deepcopy(recording.result).items()
                if key not in {"schema_version", "kind"}
            }
        if isinstance(self.recordings.snapshot_fallback, dict):
            return copy.deepcopy(self.recordings.snapshot_fallback)
        return {"available": False, "platform": platform, "node": node}

    def task_dependencies(self, *, process_id: int, task_id: Optional[int] = None) -> Dict[str, Any]:
        recording, _ = self.recordings.match(
            "read_task_dependencies",
            {"process_id": process_id, "task_id": task_id},
            platform=None, node=None, miss_kind="local",
        )
        if recording is not None:
            return {
                key: value for key, value in copy.deepcopy(recording.result).items()
                if key not in {"schema_version", "kind"}
            }
        # 未录制时读冻结 fixtures 里的本地任务拓扑:参考数据,不是证据
        return super().task_dependencies(process_id=process_id, task_id=task_id)


class ReplayKnowledgeSink(KnowledgeSink):
    """校验照跑,写入只记录不落盘:auto 任务按设计直接落档,回放不能写进知识库。"""

    def __init__(self, paths):
        super().__init__(paths)
        self.attempts: List[Dict[str, Any]] = []

    def persist(
        self,
        run_id: str,
        proposal: Dict,
        *,
        allow_incomplete_evidence: bool = False,
    ) -> KnowledgeResult:
        parsed, _session, target, issues = self._prepare(
            run_id,
            proposal,
            strict_evidence=not allow_incomplete_evidence,
        )
        self.attempts.append({
            "run_id": run_id,
            "mode": parsed.mode,
            "target": str(target),
            "evidence_issues": list(issues),
        })
        return KnowledgeResult(path=target, routing_rebuilt=False, promotion=None)
