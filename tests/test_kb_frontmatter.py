from pathlib import Path

import pytest

from lib.envpack import active_env_pack
from lib.kb.frontmatter import (
    DOC_TYPES, SERVICES, STATUSES,
    FrontmatterError, load_doc,
)


def _doc(tmp_path: Path, body: str, name: str = "alpha-yarn-vcore-exhaustion.md") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_load_doc_full(tmp_path):
    p = _doc(tmp_path, (
        "---\n"
        "title: alpha YARN vcore 拥塞\n"
        "type: troubleshoot\n"
        "platform: alpha\n"
        "service: yarn\n"
        "problem: vcore-exhaustion\n"
        "symptoms: [vcore 打满, PendingContainers 高]\n"
        "alerts: [YARN-vcore-critical]\n"
        "playbook: playbooks/alpha/yarn/check_vcore_pressure.sh\n"
        "status: draft\n"
        "---\n"
        "# 正文\n"
    ))
    d = load_doc(p)
    assert d.type == "troubleshoot"
    assert d.platform == "alpha" and d.service == "yarn"
    assert d.problem == "vcore-exhaustion"
    assert "vcore 打满" in d.symptoms and "YARN-vcore-critical" in d.alerts
    assert d.playbook == "playbooks/alpha/yarn/check_vcore_pressure.sh"
    assert d.status == "draft"


def test_load_doc_index_special_name(tmp_path):
    """_index.md 走文件名约定,不走 4 类型封闭集。"""
    p = _doc(tmp_path, (
        "---\n"
        "title: alpha YARN 索引\n"
        "platform: alpha\n"
        "service: yarn\n"
        "---\n"
    ), name="_index.md")
    d = load_doc(p)
    assert d.is_index is True
    assert d.type is None


def test_load_doc_missing_frontmatter_rejected(tmp_path):
    p = _doc(tmp_path, "# 正文\n无 frontmatter\n")
    with pytest.raises(FrontmatterError, match="frontmatter"):
        load_doc(p)


def test_load_doc_unclosed_frontmatter_rejected(tmp_path):
    p = _doc(tmp_path, "---\ntype: troubleshoot\n\n# 正文\n")
    with pytest.raises(FrontmatterError, match="未闭合"):
        load_doc(p)


def test_load_doc_invalid_type_rejected(tmp_path):
    p = _doc(tmp_path, (
        "---\ntype: concern\nplatform: alpha\nservice: yarn\nproblem: x\nstatus: draft\n---\n"
    ))
    with pytest.raises(FrontmatterError, match="type"):
        load_doc(p)


def test_load_doc_invalid_platform_rejected(tmp_path):
    p = _doc(tmp_path, (
        "---\ntype: troubleshoot\nplatform: mars\nservice: yarn\nproblem: x\nstatus: draft\n---\n"
    ))
    with pytest.raises(FrontmatterError, match="platform"):
        load_doc(p)


def test_load_doc_invalid_service_rejected(tmp_path):
    p = _doc(tmp_path, (
        "---\ntype: troubleshoot\nplatform: alpha\nservice: unknown_svc\nproblem: x\nstatus: draft\n---\n"
    ))
    with pytest.raises(FrontmatterError, match="service"):
        load_doc(p)


def test_closed_sets_populated():
    assert DOC_TYPES == {"troubleshoot", "knowledge", "procedure", "oplog"}
    assert active_env_pack().knowledge_platforms() == ("alpha", "beta", "db", "misc", "cache", "edge", "shared")
    assert {"yarn", "hdfs", "hive", "hbase", "spark", "kafka", "kudu", "system",
            "jobs", "emqx", "moosefs", "zookeeper"} <= SERVICES
    assert STATUSES == {"draft", "verified"}


def test_load_doc_symptoms_bare_string_rejected(tmp_path):
    """symptoms/alerts 必须是 list,裸字符串必须显式拒(否则 list('str') 拆单字符污染路由)。"""
    body = (
        "---\ntype: troubleshoot\ntitle: t\nplatform: alpha\nservice: yarn\n"
        "problem: x\nstatus: draft\nsymptoms: 内存告警\n---\n"
    )
    p = _doc(tmp_path, body)
    with pytest.raises(FrontmatterError, match="symptoms"):
        load_doc(p)


def test_load_doc_alerts_bare_string_rejected(tmp_path):
    body = (
        "---\ntype: troubleshoot\ntitle: t\nplatform: alpha\nservice: yarn\n"
        "problem: x\nstatus: draft\nalerts: YARN-crit\n---\n"
    )
    p = _doc(tmp_path, body)
    with pytest.raises(FrontmatterError, match="alerts"):
        load_doc(p)


def test_load_doc_unhashable_platform_raises_frontmatter_error(tmp_path):
    """非法 YAML 值(list/dict)必须 FrontmatterError 而非 TypeError,不然 batch 崩。"""
    body = (
        "---\ntype: troubleshoot\ntitle: t\nplatform: [alpha]\nservice: yarn\n"
        "problem: x\nstatus: draft\n---\n"
    )
    p = _doc(tmp_path, body)
    with pytest.raises(FrontmatterError, match="platform"):
        load_doc(p)


def test_load_doc_unhashable_type_raises_frontmatter_error(tmp_path):
    body = (
        "---\ntype: [troubleshoot]\ntitle: t\nplatform: alpha\nservice: yarn\n"
        "problem: x\nstatus: draft\n---\n"
    )
    p = _doc(tmp_path, body)
    with pytest.raises(FrontmatterError, match="type"):
        load_doc(p)


def test_load_doc_utf8_bom_tolerated(tmp_path):
    """Windows 编辑器常在 UTF-8 文件头插 BOM,frontmatter 检测必须容忍。"""
    body = (
        "﻿---\ntype: troubleshoot\ntitle: t\nplatform: alpha\nservice: yarn\n"
        "problem: x\nsymptoms: []\nalerts: []\nstatus: draft\n---\n# 正文\n"
    )
    p = tmp_path / "bom.md"
    p.write_text(body, encoding="utf-8")
    doc = load_doc(p)
    assert doc.type == "troubleshoot" and doc.platform == "alpha"


def test_closed_set_master_removed():
    """service 'master' 从封闭集里删除——防和 hbasemaster/kudumaster 语义塌陷。"""
    assert "master" not in SERVICES


@pytest.mark.parametrize("value, platform, ref", [
    ("alpha", "alpha", "example/alpha"),
    ("example/alpha", "alpha", "example/alpha"),
    ("example/beta", "beta", "example/beta"),
    ("shared", "shared", "example"),
    ("example", "shared", "example"),
    ("example/db", "db", "example/db"),
    # 零散服务器集群的旧短键是 shared,知识库键另为 misc,不能与环境级混淆
    ("example/misc", "misc", "example/misc"),
    ("misc", "misc", "example/misc"),
])
def test_platform_accepts_legacy_key_and_resource_ref(tmp_path, value, platform, ref):
    p = _doc(tmp_path, (
        "---\ntitle: x\ntype: knowledge\n"
        f"platform: {value}\nservice: yarn\n---\n# x\n"
    ))
    d = load_doc(p)
    assert d.platform == platform and d.platform_ref == ref
    assert d.raw["platform"] == value          # 原文不改写,M7 才批量迁移


@pytest.mark.parametrize("value", ["example/nosuch", "other/alpha", "example/alpha/host/node1", "example/Bad"])
def test_platform_resource_ref_must_be_env_or_knowledge_cluster(tmp_path, value):
    p = _doc(tmp_path, (
        "---\ntitle: x\ntype: knowledge\n"
        f"platform: {value}\nservice: yarn\n---\n# x\n"
    ))
    with pytest.raises(FrontmatterError):
        load_doc(p)
