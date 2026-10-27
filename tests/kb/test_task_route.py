"""jobs 库按 task_type 分组:目录约束、索引标签与确定性组装(通用 → 类型通用 → pid/tid 特化)。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

import pytest

from lib.kb.bases import (
    KnowledgeBaseError, discover_bases, doc_id_aliases, group_role, place_document, resolve_doc_id,
)
from lib.kb.recall import build_recall_index
from lib.kb.retrieval import RetrievalRequest, RetrievalService, build_context, build_retrieval_index
from lib.kb.retrieval.task_route import extract_task_ids, resolve_task_route

TS_YAML = (
    "id: jobs\nname: JobScheduler 任务\nlayout: flat\nplatform: alpha\nservice: jobs\n"
    "group_field: task_type\ncommon_dir: common\nresolve:\n  common:\n"
    "    - alpha-jobs-failure-diagnosis\n    - alpha-jobs-task-drilldown\n"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _doc(root: Path, relative: str, title: str, problem: str, *, task_type: str | None = None,
         pid: int | None = None, tid: int | None = None, body: str = "任务失败排查。") -> Path:
    extra = "".join(
        f"{key}: {value}\n" for key, value in
        (("task_type", task_type), ("process_id", pid), ("task_id", tid)) if value is not None
    )
    return _write(root / relative, (
        "---\nschema_version: 2\nsource: manual_import\nsource_ref: test\n"
        f"title: {title}\ntype: troubleshoot\nplatform: alpha\nservice: jobs\n{extra}"
        f"problem: {problem}\nsummary: {title}\nknowledge_layer: alert\nentrypoints: [manual_task]\n"
        "profile: diagnosis\nexecution_scope: agent_readonly\nconfidence: observed\nrelated: []\n"
        f"symptoms: [{title}]\nalerts: []\nplaybooks: []\ndiagnostic_paths: []\n---\n\n"
        f"# {title}\n\n## 摘要\n\n{title}\n\n## 排查步骤\n\n{body}\n"
    ))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("OPS_AGENT_KNOWLEDGE_ROOT", raising=False)
    knowledge = tmp_path / "knowledge"
    _write(knowledge / "cluster/kb.yaml", "id: cluster\nname: 集群运维\nlayout: platform_service\n")
    _write(knowledge / "jobs/kb.yaml", TS_YAML)
    _doc(knowledge, "jobs/common/alpha-jobs-failure-diagnosis.md", "任务失败下钻", "failure-diagnosis")
    _doc(knowledge, "jobs/common/alpha-jobs-task-drilldown.md", "单任务下钻", "task-drilldown")
    _doc(knowledge, "jobs/common/alpha-jobs-task-latency.md", "任务耗时基线", "task-latency")
    _doc(knowledge, "jobs/sparksql/alpha-jobs-sparksql-general.md", "SparkSQL 任务通用排查",
         "sparksql-general", task_type="SPARKSQL")
    _doc(knowledge, "jobs/sparksql/alpha-jobs-sparksql-slow-query.md", "SparkSQL 慢查询",
         "sparksql-slow-query", task_type="SPARKSQL")
    _doc(knowledge, "jobs/sparksql/alpha-jobs-p1016-t13373-quotation.md", "报价单任务特化",
         "p1016-t13373-quotation", task_type="SPARKSQL", pid=1016, tid=13373)
    _doc(knowledge, "jobs/sparksql/alpha-jobs-p1016-flow.md", "1016 流程特化",
         "p1016-flow", task_type="SPARKSQL", pid=1016)
    _doc(knowledge, "jobs/sparksql/alpha-jobs-p1016-t99-other.md", "同流程其他任务",
         "p1016-t99-other", task_type="SPARKSQL", pid=1016, tid=99)
    _doc(knowledge, "jobs/transfer/alpha-jobs-sqoop-codegen.md", "Sqoop codegen",
         "sqoop-codegen", task_type="TRANSFER")
    nodes = [
        {"id": "process:alpha:1016", "kind": "process", "name": "报价单导数", "platform": "alpha", "properties": {}},
        {"id": "task:alpha:1016:13373", "kind": "task", "name": "成本报价单", "platform": "alpha",
         "properties": {"task_type": "SPARKSQL"}},
        {"id": "task:alpha:1030:13606", "kind": "task", "name": "园区停车场", "platform": "alpha",
         "properties": {"task_type": "TRANSFER"}},
    ]
    nodes += [
        {"id": "task:alpha:995:13076", "kind": "task", "name": "导入实时数据", "platform": "alpha",
         "properties": {"task_type": "SPARKSQL"}},
        {"id": "task:alpha:996:1", "kind": "task", "name": "停用写入", "platform": "alpha",
         "properties": {"task_type": "SPARKSQL", "disabled": "true"}},
        {"id": "process:alpha:995", "kind": "process", "name": "导入实时", "platform": "alpha",
         "properties": {"freq_type": "I"}},
        {"id": "table:alpha:a", "kind": "table", "name": "ods.xsbjdb", "platform": "alpha", "properties": {}},
        {"id": "table:alpha:b", "kind": "table", "name": "t1", "platform": "alpha", "properties": {}},
        {"id": "table:alpha:c", "kind": "table", "name": "dws.jt_sale_quotation", "platform": "alpha", "properties": {}},
    ]
    edge = lambda s, t, r: {"id": f"{s}|{r}|{t}", "source": s, "target": t, "relation": r}  # noqa: E731
    edges = [
        edge("process:alpha:1016", "task:alpha:1016:13373", "contains"),
        edge("task:alpha:1016:13373", "table:alpha:a", "reads_from"),
        edge("task:alpha:1016:13373", "table:alpha:b", "reads_from"),         # WITH 别名,应被滤掉
        edge("task:alpha:1016:13373", "table:alpha:c", "writes_to"),
        edge("task:alpha:995:13076", "table:alpha:a", "writes_to"),
        edge("task:alpha:996:1", "table:alpha:a", "writes_to"),
        edge("task:alpha:995:13076", "process:alpha:1016", "depends_on"),
    ]
    _write(tmp_path / "state/graphs/jobs/graph-latest.json",
           json.dumps({"schema_version": 1, "source_fingerprint": "ts", "nodes": nodes, "edges": edges}))
    build_recall_index(knowledge, tmp_path / "build/kb-recall.sqlite")
    build_retrieval_index(tmp_path)
    return tmp_path


def _connect(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{repo / 'build/kb-retrieval.sqlite'}?mode=ro", uri=True)


def _names(refs) -> list[str]:
    return [PurePosixPath(item["doc_id"]).stem.removeprefix("alpha-jobs-") for item in refs]


def test_group_manifest_validation_and_roles(tmp_path: Path):
    _write(tmp_path / "jobs/kb.yaml",
           "id: jobs\nname: x\nlayout: platform_service\ngroup_field: task_type\ncommon_dir: common\n")
    with pytest.raises(KnowledgeBaseError, match="flat"):
        discover_bases(tmp_path)
    _write(tmp_path / "jobs/kb.yaml", "id: jobs\nname: x\nlayout: flat\ngroup_field: task_type\n")
    with pytest.raises(KnowledgeBaseError, match="common_dir"):
        discover_bases(tmp_path)
    _write(tmp_path / "jobs/kb.yaml", TS_YAML)
    (base,) = discover_bases(tmp_path)
    assert base.common_doc_ids() == (
        "jobs/common/alpha-jobs-failure-diagnosis.md",
        "jobs/common/alpha-jobs-task-drilldown.md",
    )
    role = lambda path, problem, pid=None: group_role(base, PurePosixPath(path), problem, pid)  # noqa: E731
    assert role("common/a.md", "failure-diagnosis") == "common"
    assert role("sparksql/a.md", "sparksql-general") == "general"
    assert role("sparksql/a.md", "p1-t2-oom", 1) == "specific"
    assert role("sparksql/a.md", "slow-query") == "related"
    assert role("a.md", "x") is None


def test_place_document_routes_group_and_chained_redirects(tmp_path: Path):
    _write(tmp_path / "cluster/kb.yaml", "id: cluster\nname: 集群\nlayout: platform_service\n")
    _write(tmp_path / "jobs/kb.yaml", TS_YAML)
    relative = Path("alpha/jobs/alpha-jobs-p1-t2-oom.md")
    assert place_document(tmp_path, relative, "jobs", "SPARKSQL") == \
        Path("jobs/sparksql/alpha-jobs-p1-t2-oom.md")
    assert place_document(tmp_path, relative, "jobs") == \
        Path("jobs/common/alpha-jobs-p1-t2-oom.md")
    # 两次迁移:alpha/jobs/x → jobs/task/x(前缀改名 common/)→ jobs/transfer/x
    _write(tmp_path / "_redirects.yaml",
           "prefixes:\n  jobs/task/: jobs/common/\n"
           "docs:\n  alpha/jobs/x.md: jobs/task/x.md\n"
           "  jobs/common/x.md: jobs/transfer/x.md\n")
    _write(tmp_path / "jobs/transfer/x.md", "x")
    assert resolve_doc_id(tmp_path, "alpha/jobs/x.md") == "jobs/transfer/x.md"
    assert {"alpha/jobs/x.md", "jobs/task/x.md", "jobs/common/x.md"} <= \
        doc_id_aliases(tmp_path, "jobs/transfer/x.md")


def test_grouped_lint_rules(repo: Path):
    from lib.kb.lint import lint_doc, lint_kb

    knowledge = repo / "knowledge"

    def errors(relative: str, **kwargs) -> list[str]:
        return lint_doc(_doc(knowledge, relative, "坏文档", kwargs.pop("problem", "bad"), **kwargs),
                        knowledge, repo)

    good = lint_doc(knowledge / "jobs/sparksql/alpha-jobs-p1016-t13373-quotation.md", knowledge, repo)
    assert not any("分组" in e or "特化" in e or "task_type" in e for e in good), good
    assert any("不分组" in e for e in errors("jobs/common/alpha-jobs-x.md", task_type="SHELL"))
    assert any("须声明 task_type" in e for e in errors("jobs/shell/alpha-jobs-x.md"))
    assert any("不一致" in e for e in errors("jobs/shell/alpha-jobs-y.md", task_type="MYSQL"))
    assert any("同时声明 process_id" in e
               for e in errors("jobs/shell/alpha-jobs-z.md", task_type="SHELL", tid=5))
    assert any("p7-" in e for e in errors("jobs/shell/alpha-jobs-w.md", task_type="SHELL", pid=7))
    assert any("须放在 common/" in e for e in errors("jobs/alpha-jobs-root.md"))
    (knowledge / "jobs/common/alpha-jobs-task-drilldown.md").unlink()
    flagged = {path.name: errs for path, errs in lint_kb(knowledge, repo_root=repo)}
    assert any("resolve.common" in e for e in flagged["kb.yaml"])


def test_flat_index_groups_common_first(repo: Path):
    from lib.kb.index_sync import sync_indexes

    sync_indexes(repo / "knowledge")
    text = (repo / "knowledge/jobs/_index.md").read_text(encoding="utf-8")
    assert text.index("### 通用") < text.index("### SPARKSQL") < text.index("### TRANSFER")


def test_resolver_orders_common_general_then_specific(repo: Path):
    with closing(_connect(repo)) as connection:
        route = resolve_task_route(connection, process_id=1016, task_id=13373)
    assert (route.task_type, route.type_source, route.task_name) == ("SPARKSQL", "topology", "成本报价单")
    assert _names(route.required()) == [
        "failure-diagnosis", "task-drilldown",          # kb.yaml resolve.common 顺序,task-latency 不进
        "sparksql-general",
        "p1016-t13373-quotation", "p1016-flow",         # 精确 tid 在前,流程级其后,别的 tid 不进
    ]
    assert _names(route.related) == ["sparksql-slow-query"]
    assert route.gaps == []
    assert route.required()[2]["retrieval_reasons"] == ["task_route:general"]


def test_resolver_tid_only_explicit_type_and_gaps(repo: Path):
    with closing(_connect(repo)) as connection:
        transfer = resolve_task_route(connection, task_id=13606)
        unknown = resolve_task_route(connection, process_id=1, task_id=2)
        explicit = resolve_task_route(connection, task_type="sparksql")
    assert (transfer.process_id, transfer.task_type) == (1030, "TRANSFER")
    assert _names(transfer.related) == ["sqoop-codegen"]
    assert "TRANSFER 类型尚无通用排查文档" in transfer.gaps
    assert unknown.task_type is None and any("未登记任务 1/2" in gap for gap in unknown.gaps)
    assert _names(unknown.required()) == ["failure-diagnosis", "task-drilldown"]
    assert (explicit.task_type, explicit.type_source) == ("SPARKSQL", "request")
    assert RetrievalService(repo).task_route(process_id=1016, task_id=13373)["required_reads"][0]["doc_id"] \
        .endswith("failure-diagnosis.md")


def test_extract_task_ids_formats():
    assert extract_task_ids("processId: 1016\ntaskId: 13373") == (["1016"], ["13373"])
    assert extract_task_ids("JobScheduler任务13373失败,流程号：754") == (["754"], ["13373"])
    assert extract_task_ids("p1016-t13373 报错") == (["1016"], ["13373"])
    assert extract_task_ids("任务 3 次重试后失败") == ([], [])


def test_context_pack_prepends_task_route_only_in_group_context(repo: Path):
    alert = build_context(repo, RetrievalRequest(
        query="JobScheduler任务执行失败\nprocessId: 1016\ntaskId: 13373", profile="alert", platform="alpha",
        alertname="Mtail监控-JobScheduler任务执行失败告警",
    )).to_dict()
    assert _names(alert["required_reads"])[:5] == [
        "failure-diagnosis", "task-drilldown", "sparksql-general", "p1016-t13373-quotation", "p1016-flow",
    ]
    assert alert["topology_context"]["task_resolution"]["task_type"] == "SPARKSQL"
    assert alert["retrieval_meta"]["task_route_used"] is True
    # Spark 自身的 task 编号:既非 JobScheduler 语境、拓扑也查不到,不组装
    spark = build_context(repo, RetrievalRequest(
        query="Spark task 13 failed in stage 3", profile="investigation", platform="alpha",
    )).to_dict()
    assert spark["retrieval_meta"]["task_route_used"] is False
    # Beta 平台不组装;QA 只作参考不进必读
    beta = build_context(repo, RetrievalRequest(query="pid 1016 tid 13373", profile="investigation", platform="beta"))
    assert beta.retrieval_meta["task_route_used"] is False
    qa = build_context(repo, RetrievalRequest(
        query="任务13373 是什么类型", profile="qa", platform="alpha", service="jobs",
    )).to_dict()
    assert "task_route:general" not in {r for item in qa["required_reads"] for r in item.get("retrieval_reasons", [])}
    assert "sparksql-general" in _names(qa["knowledge_context"]["task_route"])


def test_group_docs_yield_to_common_unless_type_or_pid_is_named(repo: Path):
    from lib.kb.recall import search_recall_index_candidates

    def order(query: str) -> list[str]:
        return [PurePosixPath(hit.path).stem.removeprefix("alpha-jobs-")
                for hit in search_recall_index_candidates(repo / "build/kb-recall.sqlite", query, "alpha", limit=20)]

    generic = order("任务失败排查")
    assert generic.index("failure-diagnosis") < generic.index("sparksql-general")
    named = order("SparkSQL 任务失败排查")                     # 点名类型:类型文档不降权
    assert named.index("sparksql-general") < named.index("failure-diagnosis")
    from lib.kb.recall import _group_factor

    groups = {"jobs": "common"}
    specific = "jobs/sparksql/alpha-jobs-p1016-flow.md"
    assert _group_factor(specific, groups, "流程 1016 报错") == 1.0          # 含该流程号:不降权
    assert _group_factor(specific, groups, "流程 10160 报错") < 1.0
    assert _group_factor(specific, groups, "sparksql 流程 865 报错") < 1.0     # 点名类型但流程号不符
    assert _group_factor("jobs/common/x.md", groups, "报错") == 1.0
    assert _group_factor("cluster/alpha/hdfs/x.md", groups, "报错") == 1.0     # 非分组库不受影响


def test_context_group_factor_matches_recall_rule():
    factor = RetrievalService._group_factor
    assert factor({}, "任何查询") == 1.0
    assert factor({"group_role": "common"}, "任务失败") == 1.0
    assert factor({"group_role": "general", "task_type": "SPARKSQL"}, "sparksql 报错") == 1.0
    assert factor({"group_role": "general", "task_type": "SPARKSQL"}, "任务失败") < 1.0
    assert factor({"group_role": "specific", "task_type": "SPARKSQL", "process_id": "754"}, "流程754 报错") == 1.0
    assert factor({"group_role": "specific", "task_type": "SPARKSQL", "process_id": "754"}, "流程7540 报错") < 1.0
    assert factor({"group_role": "specific", "task_type": "SPARKSQL", "process_id": "754"}, "sparksql 流程865 报错") < 1.0


def test_topology_freshness_gap():
    from datetime import datetime, timezone

    from lib.kb.retrieval.task_route import TaskRoute, _check_freshness

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE topology_meta(graph_id TEXT, generated_at TEXT, status TEXT)")
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)

    def gaps(generated_at: str | None, status: str = "ok") -> list[str]:
        connection.execute("DELETE FROM topology_meta")
        if generated_at is not None:
            connection.execute("INSERT INTO topology_meta VALUES ('jobs_topology', ?, ?)",
                               (generated_at, status))
        route = TaskRoute(process_id=1, task_id=2)
        _check_freshness(connection, route, now)
        return route.gaps

    assert gaps("2026-10-10T00:00:00+00:00") == []
    assert "39 天前" in gaps("2026-09-02T03:04:23+00:00")[0]
    assert "快照缺失" in gaps(None)[0]
    assert "快照缺失" in gaps("", status="missing")[0]


def test_task_dependencies_find_upstream_writers(repo: Path):
    deps = RetrievalService(repo).task_dependencies(process_id=1016, task_id=13373)
    (task,) = deps["tasks"]
    assert [item["table"] for item in task["reads"]] == ["ods.xsbjdb"]       # 别名 t1 已滤掉
    writers = task["reads"][0]["writers"]
    assert [(w["process_id"], w["task_id"], w["freq_type"]) for w in writers["tasks"]] == [(995, 13076, "I")]
    assert writers["disabled"] == 1
    assert [item["table"] for item in task["writes"]] == ["dws.jt_sale_quotation"]
    assert deps["depended_by_processes"] == [{"process_id": 995, "process_name": "导入实时"}]
    assert deps["reference_only"] is True
    missing = RetrievalService(repo).task_dependencies(process_id=1016, task_id=1)
    assert missing["tasks"] == [] and any("未登记任务 1016/1" in gap for gap in missing["gaps"])
