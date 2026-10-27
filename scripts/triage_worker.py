#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.agent_runtime import AgentRuntime
from lib.data_root import resolve_data_root
from lib.envpack import load_env_pack
from lib.log_catalog import LogCatalog
from lib.triage_agent.actions import ActionGateway
from lib.triage_agent.command_policy import CommandPolicy
from lib.triage_agent.command_review import CommandReviewer
from lib.triage_agent.path_policy import load_path_policy
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.harness_tier import LEGACY_TASK_MESSAGE, uses_new_runtime
from lib.triage_agent.knowledge_sink import KnowledgeSink
from lib.triage_agent.model_client import (
    ModelConfig,
    OpenAICompatibleClient,
    configure_model_logging,
    load_reviewer_config,
)
from lib.triage_agent.model_registry import ModelRegistry
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.paths import AppPaths
from lib.triage_agent.task_store import TaskStore
from lib.triage_agent.worker import TriageWorker
from lib.triage_agent.write_operations import WriteOperations


class RetiredRuntime:
    """A3.7 之前由旧编排器执行的任务(审批兑现等)再次排队时:直接关闭并说明,不再执行。"""

    def __init__(self, store: TaskStore):
        self.store = store

    def run(self, task_id: str):
        task = self.store.get(task_id)
        return self.store.transition(
            task_id, task.status, TaskStatus.FAILED, error=LEGACY_TASK_MESSAGE,
            failure_kind="legacy_runtime_retired", lease_owner=None, lease_expires_at=None,
        )


def build_worker(
    paths: AppPaths,
    *,
    actions_factory=None,
    knowledge_sink=None,
    model_transport=None,
) -> TriageWorker:
    # 三个注入参数只供 Agent 回放评估替换「环境 / 知识写入 / 模型 HTTP 层」,
    # 缺省即生产组装;模型解析、命令策略与审核模型仍走同一套逻辑
    configure_model_logging(paths.state_dir)
    registry = ModelRegistry(paths.model_config)
    diagnostics = DiagnosticRegistry.load(
        paths.diagnostics_config,
        log_catalog=LogCatalog.load(paths.log_catalog_path),
        repo_root=paths.repo_root,
    )
    store = TaskStore(paths.tasks_root)
    actions = (actions_factory or ActionGateway)(paths, diagnostics)
    if knowledge_sink is None:
        knowledge_sink = KnowledgeSink(paths)
    # 日志目录以环境包采集的 catalog 为准(config/log-catalog.yaml),
    # 不传的话路径策略只会用粗粒度兜底根,catalog 等于白采
    try:
        catalog_doc = yaml.safe_load(
            paths.log_catalog_path.read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError):
        catalog_doc = None
    # fs-roots 是人工维护的可读根白名单(诊断的 fs.list_dir 已经在用),自由
    # 命令必须认同一份:catalog 只覆盖日志目录,流程定义 JSON 这类非日志文件
    # 全靠它
    try:
        fs_roots_doc = yaml.safe_load(
            load_env_pack(paths.repo_root).config_file("fs-roots.yaml").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, yaml.YAMLError):
        fs_roots_doc = None
    command_policy = CommandPolicy(
        paths.state_dir / "command-policy.json",
        path_policy=load_path_policy(fs_roots_doc, catalog=catalog_doc),
    )
    def orchestrator_factory(task):
        if not uses_new_runtime(task):
            return RetiredRuntime(store)
        if (
            not task.model_profile_id
            or not task.model_title
            or not task.model_name
            or task.reasoning_effort is None
        ):
            selection = registry.resolve(
                task.source.value,
                model_id=task.model_profile_id,
                reasoning_effort=task.reasoning_effort,
            )
            task = store.update(
                task.task_id,
                expected=task.status,
                **selection.model_dump(),
            )
        material = registry.client_material_for_task(task)
        config = ModelConfig(
            base_url=material.base_url,
            api_key=material.api_key,
            model=task.model_name,
            reasoning_effort=task.reasoning_effort,
            timeout_seconds=material.timeout_seconds,
            max_tool_turns=material.max_tool_turns,
        )
        # 审核模型设置可能在 worker 运行期间由 Web 修改;按任务读取最新配置,
        # 避免保存成功却要等重启才生效。缺省仍是 fail-safe 的人工裁决。
        reviewer_config = load_reviewer_config(paths.model_config)
        command_reviewer = CommandReviewer(
            OpenAICompatibleClient(
                reviewer_config,
                context={
                    "task_id": task.task_id,
                    "run_id": task.run_id or "",
                    "client_role": "reviewer",
                },
                transport=model_transport,
            ) if reviewer_config else None
        )
        model = OpenAICompatibleClient(
            config,
            context={
                "task_id": task.task_id,
                "run_id": task.run_id or "",
                "client_role": "agent",
            },
            transport=model_transport,
            lenient=True,
        )
        return AgentRuntime(
            store=store,
            model=model,
            actions=actions,
            knowledge_sink=knowledge_sink,
            command_policy=command_policy,
            command_reviewer=command_reviewer,
        )

    def reap_remote_sessions():
        reap = getattr(actions, "close_task_sessions_except", None)
        # 空闲时没有打开的远程会话,不必每秒全量读一遍任务列表
        if not callable(reap) or not actions.has_task_sessions():
            return
        active = {
            task.execution_id
            for task in store.list(limit=10000)
            if task.execution_id
            and task.status.value in {
                "queued", "running", "waiting_input", "waiting_approval", "waiting_operation",
            }
        }
        reap(active)

    close_all_sessions = getattr(actions, "close_all_task_sessions", None)
    # 写操作跟踪只用插件只读查询(get_operation 类),不经模型;收尾交回运行时
    operation_tracker = WriteOperations(store, None, actions)

    return TriageWorker(
        store=store,
        orchestrator_factory=orchestrator_factory,
        state_dir=paths.state_dir,
        worker_id=f"worker-{os.getpid()}",
        session_reaper=reap_remote_sessions,
        session_closer=close_all_sessions if callable(close_all_sessions) else None,
        operation_poller=operation_tracker.poll_due,
    )


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the local triage worker")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
    )
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        repo_root = Path(args.repo_root).resolve()
        state_dir = Path(args.state_dir)
        if not state_dir.is_absolute():
            state_dir = resolve_data_root(repo_root) / state_dir
        worker = build_worker(
            AppPaths.from_repo_root(repo_root, state_dir=state_dir)
        )
        if args.once:
            processed = worker.run_once()
            print(json.dumps({
                "ok": True,
                "processed": processed,
            }, ensure_ascii=False))
            return 0
        worker.run_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
