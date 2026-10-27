"""lib/kb/promote.py:draft→verified 行级手术、幂等、lint 回滚。"""
from pathlib import Path
import shutil

import pytest

from lib.kb.frontmatter import load_doc
from lib.kb.promote import AlreadyVerified, PromoteError, promote_manual
from tests.envpack_helpers import write_redlines

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mk_repo(tmp_path, status="draft", doc_type="troubleshoot",
             rel="alpha/system/alpha-system-demo-case.md", platform="alpha", service="system"):
    (tmp_path / "knowledge/shared/procedures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib/data").mkdir(parents=True, exist_ok=True)
    write_redlines(tmp_path / "knowledge/shared/procedures/redlines.md")
    shutil.copy2(PROJECT_ROOT / "lib/data/filter_rules.yaml",
                 tmp_path / "lib/data/filter_rules.yaml")
    p = tmp_path / "knowledge" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntitle: {Path(rel).stem}\ntype: {doc_type}\nplatform: {platform}\n"
        f"service: {service}\nproblem: demo-case\nsymptoms: []\nalerts: []\n"
        f"playbook: \nstatus: {status}\n---\n\n## 现象\n\n正文保持原样。\n",
        encoding="utf-8")
    return p


def test_promote_draft_to_verified(tmp_path):
    p = _mk_repo(tmp_path)
    info = promote_manual(
        "alpha/system/alpha-system-demo-case.md", reason="人工复核通过",
        run_id="ts-20260714-0001-abcd", repo_root=tmp_path,
        today="2026-07-14",
    )
    assert info == {"doc": "alpha/system/alpha-system-demo-case.md",
                    "verified_by": "manual:ts-20260714-0001-abcd",
                    "verified_at": "2026-07-14",
                    "verified_reason": "人工复核通过"}
    doc = load_doc(p)
    assert doc.status == "verified"
    assert doc.raw["verified_by"] == "manual:ts-20260714-0001-abcd"
    assert doc.raw["verified_at"] == "2026-07-14"
    # 正文字节级不变:闭合线后的空行必须还在
    assert p.read_text(encoding="utf-8").split("---\n", 2)[2] == "\n## 现象\n\n正文保持原样。\n"


def test_promote_already_verified_raises(tmp_path):
    _mk_repo(tmp_path, status="verified")
    with pytest.raises(AlreadyVerified):
        promote_manual("alpha/system/alpha-system-demo-case.md",
                       reason="人工确认", run_id="ts-x", repo_root=tmp_path)


def test_promote_missing_doc_raises(tmp_path):
    (tmp_path / "knowledge").mkdir()
    with pytest.raises(PromoteError):
        promote_manual("alpha/system/nope.md", reason="人工确认",
                       run_id="ts-x", repo_root=tmp_path)


def test_procedure_cannot_be_promoted(tmp_path):
    p = _mk_repo(tmp_path, doc_type="procedure",
                 rel="shared/procedures/demo-rule.md", platform="shared",
                 service="system")
    original = p.read_text(encoding="utf-8")
    with pytest.raises(PromoteError, match="禁止晋升"):
        promote_manual("shared/procedures/demo-rule.md", reason="人工确认",
                       run_id="ts-x", repo_root=tmp_path)
    assert p.read_text(encoding="utf-8") == original


def test_promote_duplicate_status_rejected(tmp_path):
    # frontmatter 有两行 status: → 拒绝晋升,且文件内容未被改动
    p = _mk_repo(tmp_path)
    text = p.read_text(encoding="utf-8")
    text = text.replace("status: draft\n", "status: draft\nstatus: draft\n")
    p.write_text(text, encoding="utf-8")
    with pytest.raises(PromoteError, match="重复 status"):
        promote_manual("alpha/system/alpha-system-demo-case.md", reason="人工确认",
                       run_id="ts-x", repo_root=tmp_path)
    assert p.read_text(encoding="utf-8") == text


def test_promote_path_escape_rejected(tmp_path):
    (tmp_path / "knowledge").mkdir()
    with pytest.raises(PromoteError, match="越界"):
        promote_manual("../secrets.md", reason="人工确认",
                       run_id="ts-x", repo_root=tmp_path)


def test_manual_promotion_requires_reason(tmp_path):
    _mk_repo(tmp_path)
    with pytest.raises(PromoteError, match="reason"):
        promote_manual("alpha/system/alpha-system-demo-case.md",
                       reason="", repo_root=tmp_path)


from lib.kb.__main__ import main as kb_main


def test_cli_promote(tmp_path, capsys):
    _mk_repo(tmp_path)
    rc = kb_main(["promote", "alpha/system/alpha-system-demo-case.md",
                  "--reason", "人工复核通过",
                  "--run-id", "ts-20260714-0002-ef01",
                  "--repo-root", str(tmp_path)])
    assert rc == 0
    assert "verified" in capsys.readouterr().out
    assert (tmp_path / "build" / "wiki.html").exists()   # 晋升后自动重建


def test_cli_promote_already_verified_is_ok(tmp_path, capsys):
    _mk_repo(tmp_path, status="verified")
    rc = kb_main(["promote", "alpha/system/alpha-system-demo-case.md",
                  "--reason", "人工复核通过",
                  "--run-id", "ts-x", "--repo-root", str(tmp_path)])
    assert rc == 0
    assert "跳过" in capsys.readouterr().out


def test_cli_lint_still_works(tmp_path, capsys):
    _mk_repo(tmp_path)
    rc = kb_main([str(tmp_path / "knowledge"), "--repo-root", str(tmp_path),
                  "--redlines", str(tmp_path / "knowledge/shared/procedures/redlines.md"),
                  "--filter-rules", "lib/data/filter_rules.yaml"])
    # 老入口不被 promote 分派破坏;红线文档用 _mk_repo 复制进 mini 库的那份,
    # --filter-rules 走真实仓库相对路径,mini 库单篇文档本身合法,lint 恒绿,故 rc 恒为 0
    assert rc == 0
