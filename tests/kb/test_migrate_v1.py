"""知识库 1.0 格式迁移:platform 改资源引用、正文 v2 → v3,只动 frontmatter,幂等。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.kb.bases import discover_bases
from lib.kb.frontmatter import load_doc
from lib.kb.migrate_v1 import MigrationError, apply_migration, plan_migration
from scripts.migrate_kb_v1 import main

V2_DOC = (
    "---\n"
    "schema_version: 2\n"
    "source: manual_import\n"
    "source_ref: note.md\n"
    "title: NameNode Heap\n"
    "type: troubleshoot\n"
    "platform: alpha\n"
    "service: hdfs\n"
    "problem: nn-heap\n"
    "summary: 堆使用率高。\n"
    "related: []\n"
    "status: draft\n"
    "---\n"
    "\n# NameNode Heap\n\n正文里的 platform: alpha 不能被改。\n"
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    _write(root, "cluster/kb.yaml", "id: cluster\nname: 集群运维\nlayout: platform_service\n")
    _write(root, "jobs/kb.yaml",
           "id: jobs\nname: JobScheduler 任务\nlayout: flat\n"
           "platform: alpha\nservice: jobs\ndescription: 任务告警\n")
    _write(root, "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md", V2_DOC)
    _write(root, "cluster/shared/system/shared-system-tips.md",
           V2_DOC.replace("platform: alpha", "platform: shared", 1)
                 .replace("service: hdfs", "service: system").replace("problem: nn-heap", "problem: tips"))
    _write(root, "cluster/alpha/hdfs/_index.md",
           "---\n{title: _index, platform: alpha, service: hdfs}\n---\n# HDFS\n\n## 概述\n\n人工区。\n")
    _write(root, "cluster/beta/_index.md", "---\ntitle: _index\nplatform: beta\n---\n# Beta\n")
    _write(root, "_templates/troubleshoot.md", "---\nplatform: <alpha|beta|shared>\n---\n")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_dry_run_lists_changes_without_writing(knowledge: Path):
    before = _snapshot(knowledge)
    plan = plan_migration(knowledge)
    assert _snapshot(knowledge) == before
    assert not plan.errors and not plan.skipped
    fields = {item.path: item.fields for item in plan.changes}
    assert fields["cluster/alpha/hdfs/alpha-hdfs-nn-heap.md"] == [
        ("platform", "alpha", "example/alpha"), ("schema_version", "2", "3"),
    ]
    assert fields["cluster/shared/system/shared-system-tips.md"][0] == ("platform", "shared", "example")
    assert fields["cluster/alpha/hdfs/_index.md"] == [("platform", "alpha", "example/alpha")]
    assert fields["jobs/kb.yaml"] == [("platform", "alpha", "example/alpha")]
    assert "cluster/kb.yaml" not in fields
    assert not any(path.startswith("_templates/") for path in fields)


def test_apply_only_touches_frontmatter_and_is_idempotent(knowledge: Path):
    apply_migration(knowledge, plan_migration(knowledge))
    doc_path = knowledge / "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md"
    text = doc_path.read_text(encoding="utf-8")
    assert text == (V2_DOC.replace("schema_version: 2", "schema_version: 3")
                    .replace("platform: alpha\nservice", "platform: example/alpha\nservice"))
    doc = load_doc(doc_path)
    assert (doc.platform, doc.platform_ref, doc.raw["schema_version"]) == ("alpha", "example/alpha", 3)
    # _index:流式写法统一成块写法,人工区不动
    index = (knowledge / "cluster/alpha/hdfs/_index.md").read_text(encoding="utf-8")
    assert index == ("---\ntitle: _index\nplatform: example/alpha\nservice: hdfs\n---\n"
                     "# HDFS\n\n## 概述\n\n人工区。\n")
    assert (knowledge / "cluster/beta/_index.md").read_text(encoding="utf-8").startswith(
        "---\ntitle: _index\nplatform: example/beta\n---\n")
    ts = next(base for base in discover_bases(knowledge) if base.id == "jobs")
    assert ts.platform == "alpha"
    assert "platform: example/alpha\n" in (knowledge / "jobs/kb.yaml").read_text(encoding="utf-8")
    after = _snapshot(knowledge)
    assert plan_migration(knowledge).changes == []
    apply_migration(knowledge, plan_migration(knowledge))
    assert _snapshot(knowledge) == after


def test_any_error_blocks_the_whole_write(knowledge: Path):
    _write(knowledge, "cluster/alpha/hdfs/alpha-hdfs-bad.md",
           V2_DOC.replace("problem: nn-heap", "problem: bad").replace("platform: alpha", "platform: 'alpha'\nplatform: alpha"))
    before = _snapshot(knowledge)
    plan = plan_migration(knowledge)
    assert [path for path, _ in plan.errors] == ["cluster/alpha/hdfs/alpha-hdfs-bad.md"]
    with pytest.raises(MigrationError):
        apply_migration(knowledge, plan)
    assert _snapshot(knowledge) == before


def test_index_with_unknown_keys_is_not_rewritten(knowledge: Path):
    _write(knowledge, "cluster/beta/kafka/_index.md",
           "---\ntitle: _index\nplatform: beta\nservice: kafka\nowner: someone\n---\n# Kafka\n")
    plan = plan_migration(knowledge)
    assert plan.errors and plan.errors[0][0] == "cluster/beta/kafka/_index.md"
    assert "未知键" in plan.errors[0][1]


def test_non_v2_documents_are_skipped(knowledge: Path):
    _write(knowledge, "cluster/alpha/hdfs/alpha-hdfs-legacy.md",
           "---\ntitle: 旧文档\ntype: troubleshoot\nplatform: alpha\nservice: hdfs\n"
           "problem: legacy\nstatus: draft\n---\n# 旧文档\n")
    plan = plan_migration(knowledge)
    assert [path for path, _ in plan.skipped] == ["cluster/alpha/hdfs/alpha-hdfs-legacy.md"]


def test_cli_defaults_to_dry_run(knowledge: Path, capsys):
    before = _snapshot(knowledge)
    assert main(["--knowledge-root", str(knowledge), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary == {"changed": 5, "skipped": 0, "errors": 0}
    assert _snapshot(knowledge) == before
    assert main(["--knowledge-root", str(knowledge), "--apply"]) == 0
    assert "dry-run" not in capsys.readouterr().out
    assert _snapshot(knowledge) != before
    assert main(["--knowledge-root", str(knowledge)]) == 0
    assert "变更 0 个文件" in capsys.readouterr().out
