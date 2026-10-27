"""lib/kb/wiki.py:payload 构建与单文件渲染。"""
import json
from pathlib import Path

import pytest

from lib.kb.routing import build_routing
from lib.kb.wiki import build_payload, render_wiki


def _mk_doc(kb_root: Path, rel: str, front: str, body: str) -> Path:
    p = kb_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}\n---\n\n{body}", encoding="utf-8")
    return p


@pytest.fixture
def mini_kb(tmp_path):
    kb = tmp_path / "knowledge"
    _mk_doc(kb, "alpha/spark/alpha-spark-fgc-timeout.md",
            "title: alpha-spark-fgc-timeout\ntype: troubleshoot\nplatform: alpha\n"
            "service: spark\nproblem: fgc-timeout\nsymptoms: [beeline 挂起]\n"
            "alerts: [\"Spark2 端口告警\"]\nplaybook: \nstatus: draft",
            "## 症状\n\n端口超时。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    _mk_doc(kb, "beta/emqx/beta-emqx-mem-growth.md",
            "title: beta-emqx-mem-growth\ntype: troubleshoot\nplatform: beta\n"
            "service: emqx\nproblem: mem-growth\nsymptoms: []\nalerts: []\n"
            "playbook: \nstatus: verified\nverified_by: ts-20260701-0000-aaaa\n"
            "verified_at: 2026-07-01",
            "## 机理\n\n内存增长。\n")
    return kb


def test_build_payload_renders_md_and_meta(mini_kb):
    routing = build_routing(mini_kb)
    docs, warnings = build_payload(routing, mini_kb)
    assert warnings == []
    spark = docs["alpha/spark/alpha-spark-fgc-timeout.md"]
    assert "<table>" in spark["html"]            # tables 扩展生效
    assert spark["meta"]["alerts"] == ["Spark2 端口告警"]
    assert spark["meta"]["status"] == "draft"
    assert "端口超时" in spark["text"]            # 全文检索索引(去标签)
    assert "<" not in spark["text"]
    emqx = docs["beta/emqx/beta-emqx-mem-growth.md"]
    assert emqx["meta"]["verified_by"] == "ts-20260701-0000-aaaa"
    assert emqx["meta"]["verified_at"] == "2026-07-01"


def test_build_payload_extracts_doc_title(mini_kb):
    _mk_doc(mini_kb, "alpha/spark/alpha-spark-h1-title.md",
            "title: alpha-spark-h1-title\ntype: troubleshoot\nplatform: alpha\n"
            "service: spark\nproblem: h1\nsymptoms: []\nalerts: []\n"
            "playbook: \nstatus: draft",
            "# Spark `FGC` 标题排查\n\n## 现象\n\n```bash\n# fence 里的注释不是标题\n```\n")
    routing = build_routing(mini_kb)
    docs, warnings = build_payload(routing, mini_kb)
    assert warnings == []
    # 行内格式剥掉、fence 内 # 不误判
    assert docs["alpha/spark/alpha-spark-h1-title.md"]["meta"]["doc_title"] == "Spark FGC 标题排查"
    # 无 H1 的文档 doc_title 为 None(前端回退 slug)
    assert docs["beta/emqx/beta-emqx-mem-growth.md"]["meta"]["doc_title"] is None


def test_build_payload_includes_service_index_pages(mini_kb):
    _mk_doc(mini_kb, "alpha/spark/_index.md", "{title: _index, platform: alpha}",
            "# Spark (Alpha 平台)\n\n## 概述\n\n计算引擎。\n\n"
            "相关:[[alpha/spark/alpha-spark-fgc-timeout]]\n\n"
            "```bash\nif [[ -f /tmp/x ]]; then echo ok; fi\n```\n")
    _mk_doc(mini_kb, "_index.md", "{title: _index}", "# 根索引\n")
    routing = build_routing(mini_kb)
    docs, warnings = build_payload(routing, mini_kb)
    assert warnings == []
    idx = docs["alpha/spark/_index.md"]
    # platform/service 从路径推导,type=index,H1 进 doc_title
    assert idx["meta"]["platform"] == "alpha" and idx["meta"]["service"] == "spark"
    assert idx["meta"]["type"] == "index"
    assert idx["meta"]["doc_title"] == "Spark (Alpha 平台)"
    # wikilink 转成 data-wl 链接;fence 里的 bash [[ ]] 不被误转
    assert 'data-wl="alpha/spark/alpha-spark-fgc-timeout"' in idx["html"]
    assert "[[ -f /tmp/x ]]" in idx["html"]
    # 根 _index 不属于任何服务,跳过
    assert "_index.md" not in {k for k in docs if "/" not in k}


def test_render_wiki_doc_stat_excludes_index(mini_kb):
    _mk_doc(mini_kb, "alpha/spark/_index.md", "{title: _index, platform: alpha}",
            "# Spark (Alpha 平台)\n")
    routing = build_routing(mini_kb)
    html_out = render_wiki(routing, mini_kb)
    assert 'data-stat-docs="2"' in html_out     # index 页不计入文档数


def test_build_payload_degrades_on_unreadable_doc(mini_kb):
    routing = build_routing(mini_kb)
    (mini_kb / "alpha/spark/alpha-spark-fgc-timeout.md").unlink()   # routing 有、文件没了
    docs, warnings = build_payload(routing, mini_kb)
    assert len(warnings) == 1 and "alpha-spark-fgc-timeout" in warnings[0]
    assert "<pre>" in docs["alpha/spark/alpha-spark-fgc-timeout.md"]["html"]


def test_build_payload_degrades_on_broken_frontmatter(mini_kb):
    routing = build_routing(mini_kb)                          # 此时文档还合法
    broken = "---\ntitle: [unclosed\n---\n\n正文"              # YAML 语法错 → FrontmatterError
    (mini_kb / "alpha/spark/alpha-spark-fgc-timeout.md").write_text(broken, encoding="utf-8")
    docs, warnings = build_payload(routing, mini_kb)
    assert len(warnings) == 1 and "alpha-spark-fgc-timeout" in warnings[0]
    entry = docs["alpha/spark/alpha-spark-fgc-timeout.md"]
    assert "<pre>" in entry["html"]
    assert "正文" in entry["html"]                             # 降级保留转义后原文
    assert "title: [unclosed" in entry["html"]


def test_render_wiki_single_file(mini_kb):
    routing = build_routing(mini_kb)
    html_out = render_wiki(routing, mini_kb)
    # 自包含:统计、目录树数据、告警表、正文、样式、脚本全在一个字符串里
    assert "ops-agent 知识库" in html_out
    assert "alpha-spark-fgc-timeout" in html_out
    assert "Spark2 端口告警" in html_out
    assert "<style>" in html_out and "</script>" in html_out
    # 嵌入 JSON 已做 </ 转义,不会提前闭合 script
    payload_seg = html_out.split('id="kb-data"', 1)[1]
    assert "</pre>" not in payload_seg.split("</script>", 1)[0] or \
           "<\\/" in payload_seg.split("</script>", 1)[0]
    # 统计正确:2 篇、1 verified、1 告警
    assert 'data-stat-docs="2"' in html_out
    assert 'data-stat-verified="1"' in html_out
    assert 'data-stat-alerts="1"' in html_out


def test_render_wiki_shows_build_warnings(mini_kb):
    routing = build_routing(mini_kb)
    (mini_kb / "beta/emqx/beta-emqx-mem-growth.md").unlink()
    html_out = render_wiki(routing, mini_kb)
    assert "构建警告" in html_out and "beta-emqx-mem-growth" in html_out


def _extract_payload(html_out: str) -> str:
    return html_out.split('id="kb-data">', 1)[1].split("</script>", 1)[0]


def test_render_wiki_literal_placeholder_token_survives(mini_kb):
    _mk_doc(mini_kb, "alpha/spark/alpha-spark-token-echo.md",
            "title: alpha-spark-token-echo\ntype: troubleshoot\nplatform: alpha\n"
            "service: spark\nproblem: token-echo\nsymptoms: []\nalerts: []\n"
            "playbook: \nstatus: draft",
            "正文含字面 `__N_DOCS__` 占位符,不应被统计替换污染。\n")
    routing = build_routing(mini_kb)
    html_out = render_wiki(routing, mini_kb)
    data = json.loads(_extract_payload(html_out).replace("<\\/", "</"))
    doc = data["docs"]["alpha/spark/alpha-spark-token-echo.md"]
    assert "__N_DOCS__" in doc["html"]            # 字面 token 原样保留
    assert 'data-stat-docs="3"' in html_out       # 统计占位符本身照常替换


def test_render_wiki_script_close_in_code_fence_escaped(mini_kb):
    _mk_doc(mini_kb, "alpha/spark/alpha-spark-script-fence.md",
            "title: alpha-spark-script-fence\ntype: troubleshoot\nplatform: alpha\n"
            "service: spark\nproblem: script-fence\nsymptoms: []\nalerts: []\n"
            "playbook: \nstatus: draft",
            "## 现象\n\n```html\n</script>\n```\n")
    routing = build_routing(mini_kb)
    html_out = render_wiki(routing, mini_kb)
    payload = _extract_payload(html_out)
    assert "</" not in payload                    # 无裸 </,不会提前闭合 script
    data = json.loads(payload.replace("<\\/", "</"))
    assert "</script>" in data["docs"]["alpha/spark/alpha-spark-script-fence.md"]["text"]


def test_home_route_table_consumes_nested_alerts(mini_kb):
    """首页「告警 → 文档路由」表必须遍历 platform 分区的两层 ALERTS。

    回归背景:routing 改为按 platform 分区后 ALERTS 变成
    {platform: {alertname: doc}},而 renderHome 仍按扁平结构遍历,
    把 platform 当告警名、把内层 dict 当文档路径渲染成 [object Object],
    真实路由条目全部不可见,且与顶部计数自相矛盾。
    """
    # 造出「某平台多个告警」的形态,使告警总数 > 平台数,两种遍历方式结果可区分
    _mk_doc(mini_kb, "alpha/hdfs/alpha-hdfs-datanode-down.md",
            "title: alpha-hdfs-datanode-down\ntype: troubleshoot\nplatform: alpha\n"
            "service: hdfs\nproblem: datanode-down\nsymptoms: []\n"
            "alerts: [\"DataNode 掉线\"]\nplaybook: \nstatus: draft",
            "## 症状\n\nDataNode 掉线。\n")
    _mk_doc(mini_kb, "beta/emqx/beta-emqx-port-down.md",
            "title: beta-emqx-port-down\ntype: troubleshoot\nplatform: beta\n"
            "service: emqx\nproblem: port-down\nsymptoms: []\n"
            "alerts: [\"EMQX 端口不通\"]\nplaybook: \nstatus: draft",
            "## 症状\n\n端口不通。\n")
    routing = build_routing(mini_kb)
    html_out = render_wiki(routing, mini_kb)

    # 注入给前端的 ALERTS 是 {platform: {alertname: docpath}} 两层结构
    injected = json.loads(_extract_payload(html_out).replace("<\\/", "</"))["alerts"]
    assert set(injected) == {"alpha", "beta"}
    assert injected["alpha"]["Spark2 端口告警"] == "alpha/spark/alpha-spark-fgc-timeout.md"
    assert injected["alpha"]["DataNode 掉线"] == "alpha/hdfs/alpha-hdfs-datanode-down.md"
    assert injected["beta"]["EMQX 端口不通"] == "beta/emqx/beta-emqx-port-down.md"

    # 顶部计数按嵌套求和;表格必须能渲染出同样多的条目,不能退化成"每平台一行"
    total_routes = sum(len(by_name) for by_name in injected.values())
    assert total_routes > len(injected)               # 告警数 > 平台数,两者可区分
    assert f'data-stat-alerts="{total_routes}"' in html_out

    # 消费侧契约:表头三列(平台/告警名/文档)。扁平遍历的旧实现只有两列,此断言会失败
    assert "<th>平台</th>" in html_out
    assert "<th>告警名</th>" in html_out
    assert "<th>文档</th>" in html_out
