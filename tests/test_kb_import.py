import json
import shutil
from pathlib import Path

from lib.kb.frontmatter import load_doc
from tests.envpack_helpers import write_redlines
from scripts.kb_import import run


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "knowledge/shared/procedures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib/data").mkdir(parents=True, exist_ok=True)
    write_redlines(tmp_path / "knowledge/shared/procedures/redlines.md")
    shutil.copy2(
        PROJECT_ROOT / "lib/data/filter_rules.yaml",
        tmp_path / "lib/data/filter_rules.yaml",
    )
    return tmp_path


def _manifest(**updates):
    value = {
        "mode": "new",
        "title": "HDFS DataNode 磁盘故障排查",
        "type": "troubleshoot",
        "platform": "alpha",
        "service": "hdfs",
        "problem": "datanode-disk-failure-imported",
        "summary": "DataNode 数据盘异常时的确认方法和处置边界。",
        "symptoms": ["DataNode 磁盘目录不可访问"],
        "alerts": [],
        "playbook": None,
        "related": [],
        "sections": {
            "适用现象": "- 数据目录报 I/O error",
            "关键判据": "- 挂载点不可读且内核日志出现磁盘错误",
            "机理": "磁盘故障导致 DataNode 数据目录失效。",
            "排查步骤": "1. 只读检查挂载和内核日志。",
            "处置建议": "- 由人工安排磁盘更换。",
        },
    }
    value.update(updates)
    return value


def _run(tmp_path: Path, manifest: dict, capsys, extra=()):
    repo = _repo(tmp_path)
    source = tmp_path / "local-note.md"
    source.write_text("原始本地记录", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    rc = run([
        "--source", str(source), "--manifest", str(manifest_path),
        "--repo-root", str(repo), "--state-dir", str(tmp_path / "state"), *extra,
    ])
    return rc, json.loads(capsys.readouterr().out), repo


def test_manual_import_renders_canonical_document(tmp_path, capsys):
    rc, output, repo = _run(tmp_path, _manifest(), capsys)
    assert rc == 0 and output["ok"] is True
    path = repo / "knowledge" / output["doc"]
    doc = load_doc(path)
    assert doc.raw["schema_version"] == 3
    assert doc.raw["platform"] == "example/alpha" and doc.platform == "alpha"
    assert "author" not in doc.raw          # 未配置本机用户:不写,读时推断
    assert doc.raw["source"] == "manual_import"
    assert doc.raw["source_ref"] == "local-note.md"
    assert doc.raw["source_sha256"] == output["source_sha256"]
    assert doc.status == "draft"
    text = path.read_text(encoding="utf-8")
    assert text.count("# HDFS DataNode 磁盘故障排查") == 1
    assert "## 关键判据" in text
    assert (repo / "build/kb-recall.sqlite").is_file()
    service_index = repo / "knowledge/alpha/hdfs/_index.md"
    assert service_index.is_file()
    assert "HDFS DataNode 磁盘故障排查" in service_index.read_text(encoding="utf-8")


def test_new_import_refuses_to_overwrite(tmp_path, capsys):
    rc, output, _ = _run(tmp_path, _manifest(), capsys)
    assert rc == 0
    rc, output, _ = _run(tmp_path, _manifest(), capsys)
    assert rc == 1
    assert "不得覆盖" in output["message"]


def test_update_import_replaces_content_and_returns_to_draft(tmp_path, capsys):
    rc, output, repo = _run(tmp_path, _manifest(), capsys)
    assert rc == 0
    path = repo / "knowledge" / output["doc"]
    text = path.read_text(encoding="utf-8").replace(
        "status: draft", "status: verified\nverified_by: old-run",
    )
    path.write_text(text, encoding="utf-8")

    updated = _manifest(
        mode="update",
        summary="更新后的摘要。",
        sections={**_manifest()["sections"], "机理": "更新后的机理。"},
    )
    rc, output, _ = _run(tmp_path, updated, capsys)
    assert rc == 0
    changed = path.read_text(encoding="utf-8")
    assert "更新后的机理" in changed
    assert "status: draft" in changed
    assert "verified_by" not in changed


def test_update_with_target_rewrites_legacy_path(tmp_path, capsys):
    legacy = _repo(tmp_path) / "knowledge/alpha/hdfs/datanode/legacy-disk.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "---\ntitle: 旧文档\ntype: troubleshoot\nplatform: alpha\nservice: hdfs\n"
        "problem: datanode-disk-failure-imported\nstatus: draft\n---\n\n# 旧文档\n",
        encoding="utf-8",
    )
    updated = _manifest(mode="update", sections={**_manifest()["sections"], "机理": "改写后的机理。"})
    rc, output, repo = _run(tmp_path, updated, capsys, ("--target", "alpha/hdfs/datanode/legacy-disk.md"))
    assert rc == 0 and output["doc"] == "alpha/hdfs/datanode/legacy-disk.md"
    assert "改写后的机理" in legacy.read_text(encoding="utf-8")
    assert not (repo / "knowledge/alpha/hdfs/alpha-hdfs-datanode-disk-failure-imported.md").exists()


def test_target_requires_update_and_same_document(tmp_path, capsys):
    legacy = _repo(tmp_path) / "knowledge/alpha/hdfs/datanode/legacy-disk.md"
    legacy.parent.mkdir(parents=True)
    original = (
        "---\ntitle: 旧文档\ntype: troubleshoot\nplatform: alpha\nservice: hdfs\n"
        "problem: another-problem\nstatus: draft\n---\n\n# 旧文档\n"
    )
    legacy.write_text(original, encoding="utf-8")
    target = ("--target", "alpha/hdfs/datanode/legacy-disk.md")
    rc, output, _ = _run(tmp_path, _manifest(mode="update"), capsys, target)
    assert rc == 1 and "problem 不一致" in output["message"]
    rc, output, _ = _run(tmp_path, _manifest(problem="another-problem"), capsys, target)
    assert rc == 1 and "只用于 mode=update" in output["message"]
    assert legacy.read_text(encoding="utf-8") == original


def test_invalid_manifest_never_creates_target(tmp_path, capsys):
    invalid = _manifest(sections={"机理": "缺少其他章节"})
    rc, output, repo = _run(tmp_path, invalid, capsys)
    assert rc == 1
    assert output["error"] == "ValidationError"
    assert not (repo / "knowledge/alpha/hdfs/alpha-hdfs-datanode-disk-failure-imported.md").exists()


def test_source_inside_knowledge_is_rejected(tmp_path, capsys):
    repo = _repo(tmp_path)
    source = repo / "knowledge/source.md"
    source.write_text("x", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(), ensure_ascii=False), encoding="utf-8")
    rc = run([
        "--source", str(source), "--manifest", str(manifest_path),
        "--repo-root", str(repo),
    ])
    output = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "knowledge/ 之外" in output["message"]


def test_import_records_local_user_as_author(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config/user.local.yaml").write_text("id: alice\ndisplay_name: alice\n", encoding="utf-8")
    rc, output, repo = _run(tmp_path, _manifest(), capsys)
    assert rc == 0
    assert load_doc(repo / "knowledge" / output["doc"]).raw["author"] == "alice"


def test_update_with_target_accepts_migrated_resource_ref(tmp_path, capsys):
    """迁移后的文档 platform 是资源引用,manifest 仍写短键:按归一值比较。"""
    legacy = _repo(tmp_path) / "knowledge/alpha/hdfs/datanode/legacy-disk.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "---\ntitle: 旧文档\ntype: troubleshoot\nplatform: example/alpha\nservice: hdfs\n"
        "problem: datanode-disk-failure-imported\nstatus: draft\n---\n\n# 旧文档\n",
        encoding="utf-8",
    )
    updated = _manifest(mode="update", sections={**_manifest()["sections"], "机理": "改写后的机理。"})
    rc, output, _ = _run(tmp_path, updated, capsys, ("--target", "alpha/hdfs/datanode/legacy-disk.md"))
    assert rc == 0 and "改写后的机理" in legacy.read_text(encoding="utf-8")
