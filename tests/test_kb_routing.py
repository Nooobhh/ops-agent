from pathlib import Path

from lib.kb.routing import build_routing


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


DOC_A = (
    "---\n"
    "title: alpha YARN vcore 拥塞\n"
    "type: troubleshoot\n"
    "platform: alpha\n"
    "service: yarn\n"
    "problem: vcore-exhaustion\n"
    "symptoms: [vcore 打满, PendingContainers 高]\n"
    "alerts: [YARN-vcore-critical]\n"
    "playbook: playbooks/alpha/yarn/check_vcore.sh\n"
    "status: draft\n"
    "---\n"
)

DOC_B = (
    "---\n"
    "title: alpha 系统内存压力\n"
    "type: troubleshoot\n"
    "platform: alpha\n"
    "service: system\n"
    "problem: memory-pressure\n"
    "symptoms: [内存告警, swap 高]\n"
    "alerts: [集群监控-内存告警]\n"
    "status: draft\n"
    "---\n"
)


def test_build_routing_basic(tmp_path):
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", DOC_A)
    _write(tmp_path, "alpha/system/alpha-system-memory-pressure.md", DOC_B)
    r = build_routing(tmp_path)
    # (platform, alertname) → 单一 doc(精确匹配)
    assert r["alerts"]["alpha"]["YARN-vcore-critical"]["doc"].endswith("alpha-yarn-vcore-exhaustion.md")
    assert r["alerts"]["alpha"]["YARN-vcore-critical"]["playbook"] == "playbooks/alpha/yarn/check_vcore.sh"
    assert r["alerts"]["alpha"]["集群监控-内存告警"]["playbook"] is None    # DOC_B 无 playbook
    # 症状候选:按 (platform, service) 分组
    key = ("alpha", "yarn")
    entries = [e for e in r["docs"] if (e["platform"], e["service"]) == key]
    assert len(entries) == 1
    assert entries[0]["problem"] == "vcore-exhaustion"
    assert "vcore 打满" in entries[0]["symptoms"]


def test_build_routing_alertname_normalization(tmp_path):
    """告警名 key 规范化:去首尾空白,大小写敏感保留。"""
    body = DOC_A.replace("alerts: [YARN-vcore-critical]",
                         "alerts: ['  YARN-vcore-critical  ']")
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", body)
    r = build_routing(tmp_path)
    assert "YARN-vcore-critical" in r["alerts"]["alpha"]
    assert "  YARN-vcore-critical  " not in r["alerts"]["alpha"]


def test_build_routing_allows_same_alertname_on_different_platforms(tmp_path):
    """同名告警可在 alpha/beta 各自注册并保留独立路由。"""
    _write(tmp_path, "alpha/yarn/alpha-yarn-vcore-exhaustion.md", DOC_A)
    beta = (DOC_A.replace("title: alpha YARN vcore 拥塞", "title: beta YARN vcore 拥塞")
                .replace("platform: alpha", "platform: beta")
                .replace("playbooks/alpha/", "playbooks/beta/"))
    _write(tmp_path, "beta/yarn/beta-yarn-vcore-exhaustion.md", beta)

    r = build_routing(tmp_path)

    assert r["alerts"]["alpha"]["YARN-vcore-critical"]["doc"].startswith("alpha/")
    assert r["alerts"]["beta"]["YARN-vcore-critical"]["doc"].startswith("beta/")


def test_build_routing_skips_index_and_non_troubleshoot(tmp_path):
    """index 与 knowledge/procedure/oplog 类不进 alerts 表;troubleshoot 独占。"""
    _write(tmp_path, "alpha/yarn/_index.md",
           "---\ntitle: alpha YARN\nplatform: alpha\nservice: yarn\n---\n")
    _write(tmp_path, "shared/hdfs/hdfs-primer.md",
           "---\ntitle: HDFS 入门\ntype: knowledge\nplatform: shared\nservice: hdfs\n"
           "alerts: [HDFS-primer-fake]\nstatus: draft\n---\n")
    r = build_routing(tmp_path)
    assert not any("HDFS-primer-fake" in entries for entries in r["alerts"].values())
    assert not any(e.get("is_index") for e in r["docs"])
