from pathlib import Path

import pytest

from lib.kb.lint import lint_doc, lint_kb
from tests.envpack_helpers import as_env_pack


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


TROUBLESHOOT = (
    "---\n"
    "title: alpha YARN vcore 拥塞\n"
    "type: troubleshoot\n"
    "platform: alpha\n"
    "service: yarn\n"
    "problem: vcore-exhaustion\n"
    "symptoms: [vcore 打满]\n"
    "alerts: []\n"
    "playbook: \n"
    "related: []\n"
    "status: draft\n"
    "---\n"
    "# 正文\n"
)


def test_lint_clean_troubleshoot(tmp_path):
    p = _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", TROUBLESHOOT)
    assert lint_doc(p, kb_root=tmp_path) == []


def test_slug_naming_enforced(tmp_path):
    """文件名 slug 必须匹配 <platform>-<service>-<problem>.md 或 _index.md。"""
    p = _write(tmp_path, "alpha/yarn/BadFileName.md", TROUBLESHOOT)
    errs = lint_doc(p, kb_root=tmp_path)
    assert any("文件名" in e for e in errs)


def test_directory_matches_platform_service(tmp_path):
    """文档所在目录必须是 knowledge/<platform>/<service>/。"""
    p = _write(tmp_path, "shared/yarn/alpha-yarn-vcore-exhaustion.md", TROUBLESHOOT)  # platform=alpha 但目录 shared
    errs = lint_doc(p, kb_root=tmp_path)
    assert any("目录" in e for e in errs)


def test_title_matches_filename_stem(tmp_path):
    """title 必须与文件名 stem 语义对齐(此处只校验 title 非空,允许中文)。"""
    body = TROUBLESHOOT.replace("title: alpha YARN vcore 拥塞\n", "title: \n")
    p = _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", body)
    errs = lint_doc(p, kb_root=tmp_path)
    assert any("title" in e for e in errs)


def test_procedure_verified_rejected(tmp_path):
    """procedure 类型禁止 verified 状态(spec §7.1)。"""
    body = (
        "---\n"
        "title: 重启 HDFS NameNode 规程\n"
        "type: procedure\n"
        "platform: alpha\n"
        "service: hdfs\n"
        "status: verified\n"
        "---\n"
        "# 正文\n"
    )
    p = _write(tmp_path, "alpha/hdfs/alpha-hdfs-restart-nn.md", body)
    errs = lint_doc(p, kb_root=tmp_path)
    assert any("procedure" in e and "verified" in e for e in errs)


def test_lint_kb_scans_all(tmp_path):
    """lint_kb 扫描全库,收集所有错误。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", TROUBLESHOOT)  # OK
    _write(tmp_path, "alpha/yarn/BadFileName.md", TROUBLESHOOT)              # 命名错
    errs = lint_kb(tmp_path)
    assert len(errs) >= 1
    assert any("BadFileName" in str(e) for e in errs)


DOC_A = TROUBLESHOOT.replace("alerts: []", "alerts: [YARN-vcore-critical]")


def test_alertname_unique_within_platform(tmp_path):
    """同一平台内 alertname 不得在两篇 troubleshoot 里同时注册。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", DOC_A)
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-duplicate.md",
           DOC_A.replace("vcore-exhaustion", "vcore-duplicate")
                .replace("alpha YARN vcore 拥塞", "alpha YARN vcore 重复注册"))
    errs = lint_kb(tmp_path)
    flat = [e for _, es in errs for e in es]
    assert any("YARN-vcore-critical" in e and ("重复" in e or "unique" in e.lower())
               for e in flat)


def test_same_alertname_allowed_across_platforms(tmp_path):
    """alpha/beta 可以为同名告警分别登记平台专属文档。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", DOC_A)
    beta = (DOC_A.replace("title: alpha YARN vcore 拥塞", "title: beta YARN vcore 拥塞")
                .replace("platform: alpha", "platform: beta")
                .replace("playbooks/alpha/", "playbooks/beta/"))
    _write(tmp_path, "beta/yarn/beta-yarn-vcore-exhaustion.md", beta)

    flat = [e for _, es in lint_kb(tmp_path) for e in es]

    assert not any("YARN-vcore-critical" in e and "重复" in e for e in flat)


def test_wikilink_broken_reported(tmp_path):
    """[[相对路径|slug]] 或 [[slug]] 断链必须报错。"""
    body = TROUBLESHOOT.replace(
        "# 正文\n",
        "# 正文\n相关: [[alpha-yarn-nonexistent]] 和 [[shared/system/nowhere]]\n"
    )
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", body)
    errs = lint_kb(tmp_path)
    flat = [e for _, es in errs for e in es]
    assert any("alpha-yarn-nonexistent" in e and "断链" in e for e in flat)
    assert any("shared/system/nowhere" in e and "断链" in e for e in flat)


def test_wikilink_valid_ok(tmp_path):
    """指向真实存在的 slug 不报错。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md",
           TROUBLESHOOT + "参见 [[alpha-yarn-nm-swap-gc]]\n")
    _write(tmp_path, "alpha/yarn/alpha-yarn-nm-swap-gc.md",
           TROUBLESHOOT.replace("vcore-exhaustion", "nm-swap-gc")
                       .replace("alpha YARN vcore 拥塞", "alpha YARN NM swap"))
    errs = lint_kb(tmp_path)
    flat = [e for _, es in errs for e in es]
    assert not any("断链" in e for e in flat)


def test_playbook_pointer_must_exist(tmp_path):
    """frontmatter.playbook 指针指向的文件必须存在(相对于 repo 根)。"""
    body = TROUBLESHOOT.replace(
        "playbook: \n",
        "playbook: playbooks/alpha/yarn/does_not_exist.sh\n"
    )
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", body)
    errs = lint_kb(tmp_path)
    flat = [e for _, es in errs for e in es]
    assert any("playbook" in e and ("不存在" in e or "缺失" in e) for e in flat)


def test_playbook_pointer_exists_ok(tmp_path):
    as_env_pack(tmp_path)  # playbook 引用解析到环境包:临时仓兼作环境包
    (tmp_path / "playbooks" / "alpha" / "yarn").mkdir(parents=True)
    (tmp_path / "playbooks" / "alpha" / "yarn" / "check_vcore.sh").write_text(
        "#!/bin/bash\npb_init 'alpha/yarn/check_vcore'\nemit_verdict conclusive ok 'x'\n",
        encoding="utf-8",
    )
    body = TROUBLESHOOT.replace(
        "playbook: \n",
        "playbook: playbooks/alpha/yarn/check_vcore.sh\n"
    )
    _write(tmp_path, "knowledge/alpha/yarn/alpha-yarn-vcore-exhaustion.md", body)
    errs = lint_kb(tmp_path / "knowledge", repo_root=tmp_path)
    flat = [e for _, es in errs for e in es]
    assert not any("playbook" in e and ("不存在" in e or "缺失" in e) for e in flat)


def test_playbook_pointer_empty_ok(tmp_path):
    """playbook 字段为空(暂无脚本)不报错——过渡态允许。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", TROUBLESHOOT)
    errs = lint_kb(tmp_path)
    flat = [e for _, es in errs for e in es]
    assert not any("playbook" in e and ("不存在" in e or "缺失" in e) for e in flat)


def test_procedure_in_shared_procedures_dir_ok(tmp_path):
    """procedure 类允许 knowledge/shared/procedures/ 目录,platform 必须 shared。"""
    body = (
        "---\n"
        "title: 命令红线人可读镜像\n"
        "type: procedure\n"
        "platform: shared\n"
        "service: system\n"
        "filter_rules_sha256: fakehashabc\n"
        "status: draft\n"
        "---\n"
    )
    p = _write(tmp_path, "shared/procedures/redlines.md", body)
    errs = lint_doc(p, kb_root=tmp_path)
    # 目录/命名不该报错;filter_rules_sha256 是自由 frontmatter 字段
    assert not any("目录" in e or "文件名" in e for e in errs), errs


def test_procedure_in_shared_procedures_wrong_platform_rejected(tmp_path):
    """shared/procedures/ 下 platform 必须是 shared。"""
    body = (
        "---\n"
        "title: t\n"
        "type: procedure\n"
        "platform: alpha\n"      # 错:目录 shared/procedures 要求 platform=shared
        "service: system\n"
        "status: draft\n"
        "---\n"
    )
    p = _write(tmp_path, "shared/procedures/redlines.md", body)
    errs = lint_doc(p, kb_root=tmp_path)
    assert any("shared" in e and "platform" in e for e in errs)


def test_oplog_directory_layout_ok(tmp_path):
    """oplog 类走 knowledge/oplog/<platform>/*.md 布局,platform 从第二段读。"""
    body = (
        "---\n"
        "title: 2026-07-08 YARN spark 队列改造\n"
        "type: oplog\n"
        "platform: alpha\n"
        "service: yarn\n"
        "status: draft\n"
        "---\n"
    )
    p = _write(tmp_path, "oplog/alpha/2026-07-08-yarn-spark-queue.md", body)
    errs = lint_doc(p, kb_root=tmp_path)
    assert not any("目录" in e for e in errs), errs


def test_oplog_directory_platform_mismatch_rejected(tmp_path):
    """oplog/<second>/ 的 <second> 必须匹配 frontmatter.platform。"""
    body = (
        "---\n"
        "title: t\n"
        "type: oplog\n"
        "platform: beta\n"      # 错:目录 oplog/alpha 要求 platform=alpha
        "service: yarn\n"
        "status: draft\n"
        "---\n"
    )
    p = _write(tmp_path, "oplog/alpha/2026-07-08-yarn-spark-queue.md", body)
    errs = lint_doc(p, kb_root=tmp_path)
    assert any("oplog" in e and "platform" in e for e in errs)


def test_wikilink_windows_style_normalized(tmp_path):
    """wikilink 目标 `alpha\\yarn\\xxx` 归一化后应匹配 `alpha/yarn/xxx.md`。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-nm-swap-gc.md",
           TROUBLESHOOT.replace("vcore-exhaustion", "nm-swap-gc")
                       .replace("alpha YARN vcore 拥塞", "alpha YARN NM swap"))
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md",
           TROUBLESHOOT + r"参见 [[alpha\yarn\alpha-yarn-nm-swap-gc]]" + "\n")
    errs = lint_kb(tmp_path)
    flat = [e for _, es in errs for e in es]
    assert not any("断链" in e for e in flat), flat


def test_index_manual_region_rejects_node_column(tmp_path):
    """节点分布只由生成区维护;人工区表格带「节点」列即拒,生成区不受限。"""
    header = "---\ntitle: _index\nplatform: alpha\nservice: hdfs\n---\n# HDFS\n\n"
    generated = (
        "<!-- GENERATED:KB-INDEX:START -->\n\n## 组件分布\n\n"
        "| 组件 | 节点数 | 节点 |\n|---|---:|---|\n| NAMENODE | 2 | node1, node3 |\n\n"
        "<!-- GENERATED:KB-INDEX:END -->\n"
    )
    clean = _write(tmp_path, "alpha/hdfs/_index.md", header
                   + "## 组件\n\n| 组件 | 进程关键字 |\n|---|---|\n| NameNode | `NameNode` |\n\n" + generated)
    assert lint_doc(clean, kb_root=tmp_path) == []
    dirty = _write(tmp_path, "alpha/yarn/_index.md", header
                   + "## 组件\n\n| 组件 | 节点 | 进程关键字 |\n|---|---|---|\n| RM | node1 | `ResourceManager` |\n\n" + generated)
    errs = lint_doc(dirty, kb_root=tmp_path)
    assert len(errs) == 1 and "节点" in errs[0]


def test_index_manual_region_rejects_ports_and_dependency_sections(tmp_path):
    header = "---\ntitle: _index\nplatform: alpha\nservice: hdfs\n---\n# HDFS\n\n"
    tail = "<!-- GENERATED:KB-INDEX:START -->\n\n## 服务依赖\n\n<!-- GENERATED:KB-INDEX:END -->\n"
    ports = _write(tmp_path, "alpha/hdfs/_index.md", header
                   + "## 组件\n\n| 组件 | 端口 |\n|---|---|\n| NameNode | 8020 |\n\n" + tail)
    assert any("「端口」" in e for e in lint_doc(ports, kb_root=tmp_path))
    deps = _write(tmp_path, "alpha/yarn/_index.md", header + "## 依赖\n\n- HDFS\n\n" + tail)
    errors = lint_doc(deps, kb_root=tmp_path)
    assert len(errors) == 1 and "manual-dependencies.yaml" in errors[0]


def _canonical(tmp_path, **render):
    from lib.kb.contract import ImportManifest, render_document, target_relative

    manifest = ImportManifest.model_validate({
        "title": "系统知识", "type": "knowledge", "platform": "alpha",
        "service": "system", "problem": "system-knowledge", "summary": "系统知识摘要。",
        "sections": {"适用范围": "Alpha", "关键知识": "关键事实", "边界与注意事项": "边界", "参考": "内部资料"},
    })
    text = render_document(manifest, source="manual_import", source_ref="source.md",
                           source_sha256="a" * 64, **render)
    return _write(tmp_path, target_relative(manifest).as_posix(), text), text


def test_v3_document_writes_resource_ref_and_passes(tmp_path):
    p, text = _canonical(tmp_path, author="alice")
    assert "schema_version: 3\n" in text and "platform: example/alpha\n" in text and "author: alice\n" in text
    assert lint_doc(p, kb_root=tmp_path) == []


def test_v3_document_rejects_legacy_short_key(tmp_path):
    p, text = _canonical(tmp_path)
    p.write_text(text.replace("platform: example/alpha\n", "platform: alpha\n"), encoding="utf-8")
    assert any("须写资源引用" in e and "example/alpha" in e for e in lint_doc(p, kb_root=tmp_path))
    # v2 仍按旧规则:短键照收
    p.write_text(text.replace("platform: example/alpha\n", "platform: alpha\n")
                     .replace("schema_version: 3\n", "schema_version: 2\n"), encoding="utf-8")
    assert lint_doc(p, kb_root=tmp_path) == []


def test_author_must_be_valid_user_id(tmp_path):
    p, text = _canonical(tmp_path, author="alice")
    p.write_text(text.replace("author: alice\n", "author: Haze Wang\n"), encoding="utf-8")
    assert any("author" in e for e in lint_doc(p, kb_root=tmp_path))
