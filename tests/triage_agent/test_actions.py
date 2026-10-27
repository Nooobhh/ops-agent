import json
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

from lib.cmd_filter import CommandDenied, Level, Verdict
from lib.kb.recall import RecallIndexError, build_recall_index
from lib.ssh_pool import CmdResult
from lib.triage_agent.actions import ActionDenied, ActionGateway
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.paths import AppPaths
from scripts import _common
from tests.envpack_helpers import as_env_pack


class FakePool:
    def __init__(self):
        self.calls = []

    def run(self, node, cmd, timeout=30, approved=False):
        self.calls.append((node, cmd, timeout, approved))
        return CmdResult(node, cmd, 0, "memory ok", "", 0.1)


@dataclass
class FakePlaybookResult:
    verdict: str = "conclusive"
    severity: str = "P1"
    conclusion_hint: str = "root cause"

    def to_session_dict(self):
        return {
            "playbook": "playbooks/alpha/system/check.sh",
            "node": "node1",
            "verdict": self.verdict,
            "severity": self.severity,
            "conclusion_hint": self.conclusion_hint,
            "steps": [{
                "name": "memory",
                "cmd": "free -m",
                "expect": "healthy",
                "actual": "memory ok",
                "pass": True,
            }],
            "metrics": {},
        }


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "agent-diagnostics.yaml").write_text(
        """
version: 1
diagnostics:
  - id: system.memory_summary
    platforms: [alpha]
    params: {}
    command: "free -m"
    timeout_seconds: 15
approved_actions: []
""",
        encoding="utf-8",
    )
    knowledge = tmp_path / "knowledge" / "alpha" / "system"
    knowledge.mkdir(parents=True)
    document_content = (
        "---\ntitle: Memory\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: memory-pressure\n"
        "symptoms: [memory high]\nalerts: [MemoryHigh]\n"
        "playbook: playbooks/alpha/system/check.sh\nstatus: draft\n---\n\n"
        "# memory\n"
    )
    (knowledge / "memory.md").write_text(document_content, encoding="utf-8")
    playbook = tmp_path / "playbooks" / "alpha" / "system" / "check.sh"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("#!/bin/bash\n", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "routing.json").write_text(json.dumps({
        "alerts": {
            "alpha": {
                "MemoryHigh": {
                    "doc": "alpha/system/memory.md",
                    "playbook": "playbooks/alpha/system/check.sh",
                    "platform": "alpha",
                    "service": "system",
                },
            },
        },
        "docs": [{
            "path": "alpha/system/memory.md",
            "title": "Memory",
            "type": "troubleshoot",
            "platform": "alpha",
            "service": "system",
            "problem": "memory",
            "symptoms": ["memory high"],
            "playbook": "playbooks/alpha/system/check.sh",
            "status": "draft",
        }],
    }), encoding="utf-8")
    build_recall_index(tmp_path / "knowledge", build / "kb-recall.sqlite")
    fake_pool = FakePool()
    as_env_pack(tmp_path)
    paths = AppPaths.from_repo_root(tmp_path)
    registry = DiagnosticRegistry.load(paths.diagnostics_config)
    action_gateway = ActionGateway(
        paths,
        registry,
        pool_factory=lambda _platform: fake_pool,
    )
    monkeypatch.setattr(
        "lib.triage_agent.actions.run_playbook",
        lambda *_args, **_kwargs: FakePlaybookResult(),
    )
    return action_gateway, fake_pool


def test_diagnostic_records_step_through_pool(gateway):
    action_gateway, fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    result = action_gateway.run_diagnostic(
        session.run_id,
        platform="alpha",
        node="node1",
        diagnostic_id="system.memory_summary",
        params={},
    )

    assert fake_pool.calls == [("node1", "free -m", 15.0, False)]
    assert result["stdout"] == "memory ok"
    assert result["ok"] is True
    assert result["operation"] == "run_diagnostic"
    assert result["target"] == {"platform": "alpha", "node": "node1"}
    assert result["connection"]["status"] == "opened"
    assert result["execution"]["stdout"] == "memory ok"
    saved = json.loads((session.dir / "session.json").read_text())
    assert saved["steps"][0]["cmd"] == "[node1] free -m"
    report = (session.dir / "report.md").read_text(encoding="utf-8")
    assert "# 执行报告" in report
    assert "[node1] free -m" in report
    assert "memory ok" in report


def test_task_artifact_is_versioned_and_never_executed(gateway):
    action_gateway, fake_pool = gateway
    workspace = action_gateway.start_workspace(
        task_id="tr-artifact", trigger="生成巡检说明",
    )

    first = action_gateway.create_task_artifact(
        workspace.run_id,
        filename="inventory.md",
        content="# 服务器现状\n",
        purpose="现状说明",
    )
    second = action_gateway.create_task_artifact(
        workspace.run_id,
        filename="inventory.md",
        content="# 更新后的服务器现状\n",
        purpose="现状说明更新",
    )

    assert first["filename"] == "inventory.md"
    assert second["filename"] == "inventory-v2.md"
    assert fake_pool.calls == []


def test_task_artifact_rejects_service_control_script(gateway):
    action_gateway, _fake_pool = gateway
    workspace = action_gateway.start_workspace(
        task_id="tr-unsafe", trigger="生成脚本",
    )

    with pytest.raises(ActionDenied, match="服务控制"):
        action_gateway.create_task_artifact(
            workspace.run_id,
            filename="restart.sh",
            content="#!/bin/bash\nsystemctl restart hadoop-hdfs-namenode\n",
            purpose="重启服务",
        )


def test_free_command_never_bypasses_the_redline(gateway):
    """自由命令入口已开放,安全改由红线担保:调 SSHPool 必须 approved=False,
    带 True 会让写命令直接放行——这是本入口唯一不可退让的契约。"""
    action_gateway, fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    action_gateway.run_command(
        session.run_id,
        platform="alpha",
        node="node1",
        pipeline=[["free", "-m"], ["head", "-3"]],
    )

    node, cmd, _timeout, approved = fake_pool.calls[-1]
    assert (node, cmd) == ("node1", "free -m | head -3")
    assert approved is False


def test_pipeline_arguments_cannot_reach_the_remote_shell_unquoted(gateway):
    """结构化 argv 的全部意义:参数里的 shell 构造必须以字面量落地。

    命令串形态下,过滤器看到 `${OPS:--o}`(不匹配 -o)而 shell 展开成 `-o`,
    `sort` 就写了文件——过滤的串与执行的串不是同一个。逐 token 转义后,
    这类构造只能作为普通参数传给工具。
    """
    action_gateway, fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    hostile = [
        "${OPS_UNSET:--o}",          # 展开后会变成 sort 的写文件选项
        "$(whoami)",                 # 命令替换
        "`id`",                      # 反引号命令替换
        "a; rm -rf /tmp/x",          # 命令串联
        "b & rm -f /tmp/y",          # 后台 + 第二条命令
        "*.log",                     # glob
    ]
    action_gateway.run_command(
        session.run_id,
        platform="alpha",
        node="node1",
        pipeline=[["grep", "-F", *hostile, "/var/log/app.log"]],
    )

    _node, rendered, _timeout, _approved = fake_pool.calls[-1]
    for token in hostile:
        assert shlex.quote(token) in rendered, f"{token!r} 未被转义"
    # 渲染串重新解析回 argv 后必须与原样一致:shell 再展开不出别的东西
    assert shlex.split(rendered) == [
        "grep", "-F", *hostile, "/var/log/app.log",
    ]
    # 管道只由 runner 在段之间拼,单段渲染里不该出现裸管道符
    assert "|" not in rendered.replace(shlex.quote("*.log"), "")


def test_pipeline_segments_are_joined_only_by_the_runner(gateway):
    """段与段之间的管道由 runner 拼接,模型无从在参数里自造管道。"""
    action_gateway, fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    action_gateway.run_command(
        session.run_id,
        platform="alpha",
        node="node1",
        pipeline=[["ps", "-eo", "args="], ["grep", "java"], ["head", "-20"]],
    )

    _node, rendered, _timeout, _approved = fake_pool.calls[-1]
    assert rendered == "ps -eo args= | grep java | head -20"


def test_free_command_redline_rejection_is_self_correctable(gateway):
    """红线拒绝须转成 ActionDenied(在 _is_recoverable 白名单里,模型可改写),
    漏成裸 CommandDenied 会让整个排查任务失败。"""
    action_gateway, fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    def deny(node, cmd, timeout=30, approved=False):
        raise CommandDenied(cmd, Verdict(Level.WRITE, "write command: rm"))

    fake_pool.run = deny

    with pytest.raises(ActionDenied, match="红线"):
        action_gateway.run_command(
            session.run_id,
            platform="alpha",
            node="node1",
            pipeline=[["rm", "-rf", "/tmp/whatever"]],
        )


def test_free_command_records_step_with_node_prefix(gateway):
    """自由命令与 run_diagnostic 同约定:落档 cmd 自述执行位置,
    否则报告里的证据无法回答「在哪台机看到的」。"""
    action_gateway, _fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    action_gateway.run_command(
        session.run_id,
        platform="alpha",
        node="node2",
        pipeline=[["grep", "-c", "ERROR", "/var/log/app.log"]],
    )

    saved = json.loads((session.dir / "session.json").read_text())
    assert saved["steps"][-1]["cmd"] == (
        "[node2] grep -c ERROR /var/log/app.log"
    )
    assert saved["steps"][-1]["approved"] is False


def test_playbook_must_be_registered(gateway):
    action_gateway, _fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="MemoryHigh",
        service="system",
    )

    with pytest.raises(ActionDenied, match="未登记"):
        action_gateway.run_registered_playbook(
            run_id=session.run_id,
            platform="alpha",
            node="node1",
            playbook="playbooks/unlisted.sh",
        )


def test_registered_playbook_result_is_recorded(gateway):
    action_gateway, _fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="MemoryHigh",
        service="system",
    )

    result = action_gateway.run_registered_playbook(
        run_id=session.run_id,
        platform="alpha",
        node="node1",
        playbook="playbooks/alpha/system/check.sh",
    )

    assert result["verdict"] == "conclusive"
    saved = json.loads((session.dir / "session.json").read_text())
    assert saved["playbook_results"][0]["conclusion_hint"] == "root cause"


def test_route_candidates_and_document_reads_are_registry_bounded(gateway):
    action_gateway, _fake_pool = gateway

    hit = action_gateway.lookup_route("MemoryHigh", "alpha")
    candidates = action_gateway.search_candidates("memory high", "alpha", "system")
    content = action_gateway.read_document("alpha/system/memory.md")

    assert hit["playbook"] == "playbooks/alpha/system/check.sh"
    assert candidates[0]["path"] == "alpha/system/memory.md"
    assert candidates[0]["score"] > 0
    assert candidates[0]["snippet"]
    assert content.endswith("# memory\n")
    with pytest.raises(ActionDenied):
        action_gateway.read_document("../../config/model.local.yaml")


def test_read_baseline_appends_local_resource_overlay(gateway):
    action_gateway, _fake_pool = gateway
    baseline = action_gateway.paths.knowledge_root / "alpha" / "system" / "alpha-system-baseline.md"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        "---\n"
        "schema_version: 2\nsource: manual_import\nsource_ref: baseline.md\n"
        "source_sha256: " + "a" * 64 + "\n"
        "title: Alpha 系统基线\ntype: knowledge\nplatform: alpha\nservice: system\n"
        "problem: baseline\nsummary: Alpha 系统基线。\n"
        "knowledge_layer: basic_info\nentrypoints: [basic_info, manual_task]\n"
        "profile: baseline\nexecution_scope: agent_readonly\nconfidence: observed\n"
        "related: []\nstatus: draft\n---\n\n"
        "# Alpha 系统基线\n\n## 摘要\n\nHD 系统基线。\n\n"
        "## 适用范围\n\n用于测试。\n\n## 组件与节点\n\n事实。\n\n"
        "## 关键配置\n\n配置。\n\n## 最新观测\n\n观测。\n\n"
        "## 边界与时效\n\n快照。\n\n## 参考\n\n来源。\n",
        encoding="utf-8",
    )
    resource_root = action_gateway.paths.repo_root / "records" / "telemetry" / "resources"
    resource_root.mkdir(parents=True, exist_ok=True)
    (resource_root / "2026-09-01.jsonl").write_text(
        json.dumps({
            "platform": "alpha",
            "collected_at": "2026-09-01T10:00:00+08:00",
            "status": "ok",
            "coverage": {"expected_nodes": 1, "observed_nodes": 1,
                         "expected_services": 1, "observed_services": 1},
            "services": [{
                "node": "node1", "component": "SYSTEM", "service": "system",
                "job": "system_jmx", "status": "ok", "sample_at": "2026-09-01T10:00:00+08:00",
                "metrics": {"heap_used_pct": 12.5},
            }],
            "nodes": [{
                "node": "node1", "status": "ok", "sample_at": "2026-09-01T10:00:00+08:00",
                "metrics": {"memory_used_pct": 42.0}, "filesystems": [],
            }],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    routing_path = action_gateway.paths.routing_path
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing.setdefault("docs", []).append({
        "path": "alpha/system/alpha-system-baseline.md", "title": "Alpha 系统基线",
        "type": "knowledge", "platform": "alpha", "service": "system",
        "problem": "baseline", "symptoms": [], "playbook": None,
        "status": "draft",
    })
    routing_path.write_text(json.dumps(routing), encoding="utf-8")

    content = action_gateway.read_document("alpha/system/alpha-system-baseline.md")

    assert "## 当前资源观测" in content
    assert "2026-09-01T10:00:00+08:00" in content
    assert "heap_used_pct" in content
    assert "memory_used_pct" in content


def test_recall_index_error_is_mapped_to_action_denied(gateway, monkeypatch):
    action_gateway, _fake_pool = gateway
    monkeypatch.setattr(
        "lib.triage_agent.actions.search_recall_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecallIndexError("请运行 scripts/build_wiki.py")
        ),
        raising=False,
    )

    with pytest.raises(ActionDenied, match="scripts/build_wiki.py"):
        action_gateway.search_candidates("memory high", "alpha")


def test_finalize_writes_report_and_injected_worklog(gateway):
    action_gateway, _fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha",
        node="node1",
        trigger="manual:test",
        service="system",
    )

    report = action_gateway.finalize(
        session.run_id,
        verdict="conclusive",
        severity="P1",
        root_cause="memory pressure",
        evidence=["memory ok"],
        remediation=["observe"],
    )

    assert report == session.dir / "report.md"
    assert report.exists()
    worklogs = list(action_gateway.paths.worklog_root.glob("*.md"))
    assert len(worklogs) == 1
    assert session.run_id in worklogs[0].read_text(encoding="utf-8")


def test_platform_pool_factory_uses_injected_repo_paths(tmp_path, monkeypatch):
    as_env_pack(tmp_path)  # 主机清单取环境包:临时仓兼作环境包
    seen = {}
    monkeypatch.setattr(
        _common,
        "build_pool",
        lambda platform_path, local_dev_path, credentials_path: seen.update({
            "platform": platform_path,
            "local_dev": local_dev_path,
            "credentials": credentials_path,
        }) or "pool",
    )

    assert _common.build_pool_for_platform("beta", tmp_path) == "pool"
    assert seen == {
        "platform": str(tmp_path / "config" / "platform-beta.yaml"),
        "local_dev": str(tmp_path / "config" / "local-dev.yaml"),
        "credentials": str(tmp_path / "config" / "credentials.json"),
    }
    with pytest.raises(ValueError):
        _common.build_pool_for_platform("unknown", tmp_path)


def test_redline_denial_becomes_recoverable_diagnostic_denied(gateway, monkeypatch):
    """红线拦下参数渲染出的命令时,必须回可自纠的 DiagnosticDenied。

    漏成 CommandDenied 会越过 orchestrator 的可恢复白名单,把「搜索词选得不好」
    升级成整个排查任务失败 —— 与参数校验的错误分级原则相悖。
    """
    from lib.cmd_filter import CommandDenied, Level, Verdict
    from lib.triage_agent.diagnostics import DiagnosticDenied

    action_gateway, fake_pool = gateway
    session = action_gateway.start_session(
        platform="alpha", node="node1", trigger="test",
    )

    def denied(node, cmd, timeout=30, approved=False):
        raise CommandDenied(cmd, Verdict(Level.WRITE, "write pattern: systemctl"))

    monkeypatch.setattr(fake_pool, "run", denied)

    with pytest.raises(DiagnosticDenied) as excinfo:
        action_gateway.run_diagnostic(
            session.run_id,
            platform="alpha",
            node="node1",
            diagnostic_id="system.memory_summary",
            params={},
        )
    assert "换用其他参数值" in str(excinfo.value)
