import json

import pytest
from tests.envpack_helpers import as_env_pack

PLAYBOOK = "playbooks/alpha/system/check.sh"


@pytest.fixture
def replay_repo(tmp_path):
    """最小合成仓库:一条诊断、一条登记 playbook 的告警路由、日志目录清单。"""
    repo = tmp_path / "repo"
    config = repo / "config"
    config.mkdir(parents=True)
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
    (config / "log-catalog.yaml").write_text(
        """
version: 1
platforms:
  alpha:
    services:
      system:
      - path: /var/log/messages
        nodes: all
        source: probe
""",
        encoding="utf-8",
    )
    playbook = repo / PLAYBOOK
    playbook.parent.mkdir(parents=True)
    playbook.write_text("#!/bin/bash\n", encoding="utf-8")
    build = repo / "build"
    build.mkdir()
    (build / "routing.json").write_text(json.dumps({
        "alerts": {"alpha": {"ReplayTestAlert": {
            "doc": "alpha/system/memory.md",
            "playbook": PLAYBOOK,
            "platform": "alpha",
            "service": "system",
        }}},
        "docs": [],
    }), encoding="utf-8")
    (config / "platform-alpha.yaml").write_text(
        "platform: alpha\ncollected_at: '2026-10-01'\nnodes:\n"
        "  node1: {host: 198.51.100.101, components: [NAMENODE]}\n",
        encoding="utf-8",
    )
    (config / "service-baseline.yaml").write_text(
        "service_map: {}\nconfig_whitelist: {}\nprobes: {}\n", encoding="utf-8",
    )
    (repo / "knowledge").mkdir()
    as_env_pack(repo)
    return repo


@pytest.fixture
def indexed_replay_repo(replay_repo):
    """带召回/检索索引的合成仓库,manual 任务的 ContextPack 才能构建。"""
    from lib.kb.recall import build_recall_index
    from lib.kb.retrieval import build_retrieval_index

    build_recall_index(replay_repo / "knowledge", replay_repo / "build" / "kb-recall.sqlite")
    build_retrieval_index(replay_repo)
    return replay_repo
