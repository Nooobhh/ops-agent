import json

from web.server.data.routing_reader import lookup


def _routing(tmp_path, *, doc_exists=True):
    build = tmp_path / "build"
    build.mkdir()
    path = build / "routing.json"
    path.write_text(json.dumps({
        "alerts": {
            "alpha": {
                "disk_full": {
                    "doc": "alpha/hdfs/disk-full.md",
                    "playbook": "playbooks/alpha/check_disk.sh",
                    "platform": "alpha",
                    "service": "hdfs",
                },
            },
            "shared": {
                "host_clock_skew": {
                    "doc": "shared/system/clock-skew.md",
                    "playbook": None,
                    "platform": "shared",
                    "service": "system",
                },
            },
        },
        "docs": [
            {"path": "alpha/hdfs/disk-full.md", "title": "磁盘空间不足"},
            {"path": "shared/system/clock-skew.md", "title": "主机时钟偏移"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    if doc_exists:
        doc = tmp_path / "knowledge" / "alpha" / "hdfs" / "disk-full.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# 磁盘空间不足\n", encoding="utf-8")
    return path


def test_lookup_marks_direct_platform_match_and_links_into_the_console(tmp_path):
    result = lookup(_routing(tmp_path), "disk_full", "alpha")

    assert result == {
        "doc": "alpha/hdfs/disk-full.md",
        "doc_title": "磁盘空间不足",
        # 中台内的知识页,不再跳出到离线 wiki 快照
        "doc_url": "/knowledge/alpha/hdfs/disk-full.md",
        "playbook": "playbooks/alpha/check_disk.sh",
        "matched_via": "platform",
    }


def test_lookup_marks_shared_fallback(tmp_path):
    result = lookup(_routing(tmp_path), "host_clock_skew", "beta")

    assert result["matched_via"] == "shared"
    assert result["doc"] == "shared/system/clock-skew.md"


def test_lookup_returns_none_for_no_match_or_missing_routing(tmp_path):
    assert lookup(_routing(tmp_path), "does_not_exist", "alpha") is None
    assert lookup(tmp_path / "missing" / "routing.json", "disk_full", "alpha") is None


def test_lookup_keeps_route_when_the_document_is_gone(tmp_path):
    """routing 里登记了但文件已删:仍展示命中,只是没得可跳。"""
    result = lookup(_routing(tmp_path, doc_exists=False), "disk_full", "alpha")

    assert result["doc_url"] is None
    assert result["doc_title"] == "磁盘空间不足"
    assert result["playbook"] == "playbooks/alpha/check_disk.sh"
