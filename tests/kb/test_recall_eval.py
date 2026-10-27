import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib.kb.bases import resolve_knowledge_root
from lib.kb.recall_eval import (
    Corpus,
    CorpusError,
    EvalCase,
    expand_related,
    evaluate,
    load_corpus,
    load_search_documents,
    render_markdown,
    score_case,
    search_fts_documents,
    search_fts_sections,
    search_fts_sections_reranked,
)
from tests.envpack_helpers import example_data_root


ROOT = Path(__file__).resolve().parents[2]


def _write_doc(root: Path, relative: str, frontmatter: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


@pytest.fixture
def synthetic_wiki(tmp_path):
    root = tmp_path / "knowledge"
    _write_doc(
        root,
        "alpha/yarn/alpha-yarn-pressure.md",
        "title: YARN 资源压力\n"
        "type: troubleshoot\nplatform: alpha\nservice: yarn\n"
        "problem: yarn-resource-pressure\nsymptoms: [CPU 占满, 容器内存不足]\n"
        "alerts: []\nstatus: draft\n"
        "related: ['[[beta/hdfs/beta-datanode-heartbeat]]', '[[shared/procedures/resource-check]]']",
        "# YARN 应用排队\n\n## 等待应用\n\n"
        "vCore 与 CPU 饱和，容器内存不足，Pending 应用持续等待。\n",
    )
    _write_doc(
        root,
        "alpha/hdfs/alpha-datanode-heartbeat.md",
        "title: DataNode 心跳超时\n"
        "type: troubleshoot\nplatform: alpha\nservice: hdfs\n"
        "problem: datanode-heartbeat-io\nsymptoms: [心跳超时, IO 队列高]\n"
        "alerts: []\nstatus: draft\nrelated: []",
        "# DataNode 心跳\n\n## 节点资源\n\n磁盘 IO 队列导致心跳超时。\n",
    )
    _write_doc(
        root,
        "alpha/spark/alpha-spark-heartbeat.md",
        "title: Spark Executor 心跳超时\n"
        "type: troubleshoot\nplatform: alpha\nservice: spark\n"
        "problem: executor-heartbeat\nsymptoms: [Spark Executor 心跳超时]\n"
        "alerts: []\nstatus: draft\nrelated: []",
        "# Spark Executor 心跳\n\nDataNode IO 慢时 Executor 也可能心跳超时。\n",
    )
    _write_doc(
        root,
        "alpha/yarn/alpha-yarn-cpu-memory.md",
        "title: YARN CPU 与内存检查\n"
        "type: troubleshoot\nplatform: alpha\nservice: yarn\n"
        "problem: yarn-cpu-memory\nsymptoms: [CPU 占用高, 容器内存不足]\n"
        "alerts: []\nstatus: draft\nrelated: []",
        "# YARN 资源\n\nCPU CPU CPU，容器内存 容器内存。\n",
    )
    _write_doc(
        root,
        "beta/hdfs/beta-datanode-heartbeat.md",
        "title: Beta DataNode 心跳超时\n"
        "type: troubleshoot\nplatform: beta\nservice: hdfs\n"
        "problem: datanode-heartbeat\nsymptoms: [心跳超时]\n"
        "alerts: []\nstatus: draft\nrelated: []",
        "# Beta DataNode\n\nIO 问题。\n",
    )
    _write_doc(
        root,
        "shared/procedures/resource-check.md",
        "title: 通用资源检查\n"
        "type: procedure\nplatform: shared\nservice: system\n"
        "problem: resource-check\nsymptoms: []\nalerts: []\n"
        "status: verified\nrelated: []",
        "# 通用检查\n\n核对 CPU、内存与 IO。\n",
    )
    _write_doc(
        root,
        "alpha/yarn/_index.md",
        "title: YARN 目录\nplatform: alpha\nservice: yarn",
        "# YARN 索引\n\nCPU 容器内存等待应用。\n",
    )
    _write_doc(
        root,
        "alpha/yarn/alpha-yarn-oplog.md",
        "title: YARN 操作记录\n"
        "type: oplog\nplatform: alpha\nservice: yarn\n"
        "problem: yarn-oplog\nsymptoms: []\nalerts: []\n"
        "status: draft\nrelated: []",
        "# Oplog\n\nCPU 容器内存等待应用。\n",
    )
    return root


def test_production_schemes_use_shipped_recall_index(synthetic_wiki, tmp_path):
    from lib.kb.recall import build_recall_index
    from lib.kb.recall_eval import production_schemes

    repo = synthetic_wiki.parent
    assert production_schemes(repo) == ()  # 索引未构建时不伪造方案
    build_recall_index(synthetic_wiki, repo / "build" / "kb-recall.sqlite")
    schemes = dict((name, retriever) for name, retriever, _ in production_schemes(repo))
    assert list(schemes) == ["prod_recall_top5"]
    docs = load_search_documents(synthetic_wiki)
    hits = schemes["prod_recall_top5"](docs, "yarn CPU 容器内存等待应用", platform="alpha", limit=5)
    assert hits and hits[0].path == "alpha/yarn/alpha-yarn-pressure.md"
    assert all(hit.platform in {"alpha", "shared"} for hit in hits)


def test_corpus_rejects_missing_gold_path(tmp_path):
    corpus_path = tmp_path / "corpus.yaml"
    corpus_path.write_text(
        "version: 1\ncases:\n"
        "  - id: bad\n    query: q\n    platform: alpha\n"
        "    expected_primary: [alpha/hdfs/missing.md]\n"
        "    acceptable_support: []\n    must_not_match: []\n"
        "    source: real_case\n    split: dev\n    notes: bad\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="不存在"):
        load_corpus(corpus_path, resolve_knowledge_root(ROOT))


def test_fts_section_ranks_yarn_resource_document_first(synthetic_wiki):
    docs = load_search_documents(synthetic_wiki)
    hits = search_fts_sections(
        docs,
        "调查 yarn cpu 容器内存和等待应用",
        platform="alpha",
        limit=5,
    )
    assert hits[0].path == "alpha/yarn/alpha-yarn-pressure.md"
    assert hits[0].matched_section


def test_retrievers_exclude_wrong_platform_indexes_and_oplogs(synthetic_wiki):
    docs = load_search_documents(synthetic_wiki)
    hits = search_fts_documents(docs, "datanode IO 心跳超时", platform="alpha")
    assert all(hit.platform in {"alpha", "shared"} for hit in hits)
    assert all(not hit.path.endswith("_index.md") for hit in hits)
    assert all(hit.doc_type != "oplog" for hit in hits)


def test_link_expansion_is_support_only(synthetic_wiki):
    docs = load_search_documents(synthetic_wiki)
    primary_path = "alpha/yarn/alpha-yarn-pressure.md"
    supports = expand_related(docs, primary_path, limit=2)
    assert [item.path for item in supports] == ["shared/procedures/resource-check.md"]
    assert primary_path not in [item.path for item in supports]


def test_reranker_prefers_matching_component_but_keeps_cross_service_candidate(
    synthetic_wiki,
):
    docs = load_search_documents(synthetic_wiki)
    hits = search_fts_sections_reranked(
        docs, "datanode 心跳超时与节点 IO", platform="alpha", limit=5
    )
    paths = [hit.path for hit in hits]
    assert paths[0] == "alpha/hdfs/alpha-datanode-heartbeat.md"
    assert "alpha/spark/alpha-spark-heartbeat.md" in paths


def test_reranker_prefers_distinct_query_concept_coverage(synthetic_wiki):
    docs = load_search_documents(synthetic_wiki)
    hits = search_fts_sections_reranked(
        docs, "yarn CPU 容器内存 等待应用", platform="alpha", limit=5
    )
    assert hits[0].path == "alpha/yarn/alpha-yarn-pressure.md"


def test_metrics_score_primary_and_support_separately():
    result = score_case(
        expected_primary=("primary.md",),
        acceptable_support=("support.md",),
        must_not_match=("wrong.md",),
        ranked_paths=["support.md", "primary.md", "wrong.md"],
        support_paths=["support.md"],
    )
    assert result.reciprocal_rank == 0.5
    assert result.recall_at_1 is False
    assert result.recall_at_3 is True
    assert result.recall_at_5 is True
    assert result.support_hits == ("support.md",)
    assert result.must_not_violations == ("wrong.md",)


def test_preliminary_report_refuses_pass_claim(synthetic_wiki):
    corpus = Corpus(
        version=1,
        cases=(
            EvalCase(
                case_id="synthetic-yarn",
                query="yarn CPU 容器内存等待应用",
                platform="alpha",
                expected_primary=("alpha/yarn/alpha-yarn-pressure.md",),
                acceptable_support=("shared/procedures/resource-check.md",),
                must_not_match=(),
                source="real_case",
                split="dev",
                notes="synthetic",
            ),
        ),
        preliminary=True,
    )
    report = evaluate(corpus, load_search_documents(synthetic_wiki), synthetic_wiki)
    markdown = render_markdown(report)
    assert "预评测" in markdown
    assert "不能判定召回框架达标" in markdown


def test_cli_writes_machine_and_human_reports(synthetic_wiki, tmp_path):
    corpus_path = tmp_path / "corpus.yaml"
    corpus_path.write_text(
        "version: 1\ncases:\n"
        "  - id: synthetic-yarn\n"
        "    query: yarn CPU 容器内存等待应用\n"
        "    platform: alpha\n"
        "    expected_primary: [alpha/yarn/alpha-yarn-pressure.md]\n"
        "    acceptable_support: [shared/procedures/resource-check.md]\n"
        "    must_not_match: []\n"
        "    source: real_case\n"
        "    split: dev\n"
        "    notes: synthetic\n",
        encoding="utf-8",
    )
    json_report = tmp_path / "report.json"
    markdown_report = tmp_path / "report.md"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/eval_kb_recall.py"),
            "--corpus",
            str(corpus_path),
            "--knowledge-root",
            str(synthetic_wiki),
            "--repo-root",
            str(tmp_path),
            "--json-report",
            str(json_report),
            "--markdown-report",
            str(markdown_report),
        ],
        cwd=ROOT,
        env=example_data_root(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "preliminary": True,
        "cases": 1,
        "json_report": str(json_report),
        "markdown_report": str(markdown_report),
    }
    assert json.loads(json_report.read_text(encoding="utf-8"))["preliminary"] is True
    assert "预评测" in markdown_report.read_text(encoding="utf-8")
