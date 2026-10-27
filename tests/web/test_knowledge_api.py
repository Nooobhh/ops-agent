from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from lib.kb.recall import build_recall_index
from lib.kb.contract import ImportManifest, render_document
from web.server.app import create_app


def _write_doc(root: Path, relative: str, *, title: str, platform: str,
               service: str, body: str, alerts: str = "[]", symptoms: str = "[]",
               doc_type: str = "troubleshoot", status: str = "draft") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        f"type: {doc_type}\n"
        f"platform: {platform}\n"
        f"service: {service}\n"
        "problem: 占位问题\n"
        f"symptoms: {symptoms}\n"
        f"alerts: {alerts}\n"
        f"status: {status}\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def repo_at(tmp_path: Path, *, with_index: bool = True) -> Path:
    kb_root = tmp_path / "knowledge"
    _write_doc(kb_root, "alpha/yarn/alpha-yarn-pressure.md", title="alpha-yarn-pressure",
               platform="alpha", service="yarn", alerts="[YarnQueuePending]",
               body="# YARN 资源压力\n\n容器分配失败。参见 [[alpha-hdfs-blocks]]。")
    _write_doc(kb_root, "alpha/hdfs/alpha-hdfs-blocks.md", title="alpha-hdfs-blocks",
               platform="alpha", service="hdfs",
               body="# HDFS 坏块\n\n坏块告警持续出现。")
    _write_doc(kb_root, "beta/emqx/beta-emqx-drop.md", title="beta-emqx-drop",
               platform="beta", service="emqx",
               body="# EMQX 掉线\n\n设备大量掉线。")
    (kb_root / "alpha" / "yarn" / "_index.md").write_text(
        "---\n{title: _index, platform: alpha}\n---\n# YARN (Alpha 平台)\n\n概述。\n",
        encoding="utf-8")

    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "routing.json").write_text(json.dumps({
        "alerts": {"alpha": {"YarnQueuePending": {
            "doc": "alpha/yarn/alpha-yarn-pressure.md", "playbook": None,
            "platform": "alpha", "service": "yarn"}}},
        "docs": [],
    }, ensure_ascii=False), encoding="utf-8")
    if with_index:
        build_recall_index(kb_root, build_dir / "kb-recall.sqlite")
    return tmp_path


def client_at(tmp_path: Path, *, with_index: bool = True) -> TestClient:
    repo_root = repo_at(tmp_path, with_index=with_index)
    return TestClient(create_app(state_dir=tmp_path / "state", repo_root=repo_root))


class TestTree:
    def test_tree_groups_platforms_and_services(self, tmp_path: Path):
        response = client_at(tmp_path).get("/api/knowledge/tree")
        assert response.status_code == 200
        body = response.json()
        assert [node["platform"] for node in body["bases"][0]["platforms"]] == ["alpha", "beta"]
        alpha = body["bases"][0]["platforms"][0]
        assert [node["service"] for node in alpha["services"]] == ["hdfs", "yarn"]
        assert body["warnings"] == []

    def test_tree_exposes_index_doc_and_routed_alerts(self, tmp_path: Path):
        body = client_at(tmp_path).get("/api/knowledge/tree").json()
        yarn = next(node for node in body["bases"][0]["platforms"][0]["services"]
                    if node["service"] == "yarn")
        assert yarn["index_doc"] == "alpha/yarn/_index.md"
        assert yarn["docs"][0]["alerts"] == ["YarnQueuePending"]

    def test_tree_exposes_component_groups_and_indexes(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        _write_doc(
            repo_root / "knowledge", "alpha/yarn/nodemanager/alpha-yarn-nm.md",
            title="NodeManager 健康", platform="alpha", service="yarn",
            body="# NodeManager 健康\n",
        )
        component_index = repo_root / "knowledge/alpha/yarn/nodemanager/_index.md"
        component_index.write_text(
            "---\ntitle: _index\nplatform: alpha\nservice: yarn\n---\n"
            "# NodeManager（YARN / Alpha）\n",
            encoding="utf-8",
        )
        client = TestClient(create_app(
            state_dir=tmp_path / "state", repo_root=repo_root,
        ))
        tree = client.get("/api/knowledge/tree").json()
        yarn = next(service for service in tree["bases"][0]["platforms"][0]["services"]
                    if service["service"] == "yarn")
        component_doc = next(doc for doc in yarn["docs"] if doc["component"])
        assert component_doc["component"] == "nodemanager"
        assert yarn["component_indexes"] == [{
            "component": "nodemanager",
            "index_doc": "alpha/yarn/nodemanager/_index.md",
            "index_title": "NodeManager（YARN / Alpha）",
        }]

    def test_tree_available_without_search_index(self, tmp_path: Path):
        """正文与树直读源文件,索引没建也照样能浏览——只有搜索依赖索引。"""
        body = client_at(tmp_path, with_index=False).get("/api/knowledge/tree").json()
        assert body["bases"][0]["platforms"]
        assert body["index_state"]["available"] is False
        assert "build_wiki" in body["index_state"]["hint"]


class TestTopology:
    def test_topology_returns_generated_graph(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        graph = {
            "schema_version": 1,
            "nodes": [{"id": "service:alpha:yarn", "kind": "service"}],
            "edges": [],
            "coverage": {},
            "stats": {"node_count": 1, "edge_count": 0},
        }
        (repo_root / "build" / "kb-topology.json").write_text(
            json.dumps(graph), encoding="utf-8"
        )

        client = TestClient(create_app(
            state_dir=tmp_path / "state", repo_root=repo_root,
        ))
        response = client.get("/api/knowledge/topology")

        assert response.status_code == 200
        assert response.json() == graph

    def test_missing_topology_returns_503_with_rebuild_hint(self, tmp_path: Path):
        response = client_at(tmp_path).get("/api/knowledge/topology")
        assert response.status_code == 503
        assert "build_topology.py" in response.json()["detail"]


class TestDoc:
    def test_doc_splits_frontmatter_from_body(self, tmp_path: Path):
        response = client_at(tmp_path).get(
            "/api/knowledge/doc", params={"path": "alpha/yarn/alpha-yarn-pressure.md"})
        assert response.status_code == 200
        body = response.json()
        assert "title: alpha-yarn-pressure" in body["frontmatter_raw"]
        # 正文原样保留(含闭合线后的空行),不替前端改首行间距
        assert body["markdown"].lstrip("\n").startswith("# YARN 资源压力")
        assert "---" not in body["markdown"]

    def test_doc_meta_carries_frontmatter_and_alerts(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/doc",
            params={"path": "alpha/yarn/alpha-yarn-pressure.md"}).json()
        meta = body["meta"]
        assert meta["platform"] == "alpha"
        assert meta["service"] == "yarn"
        assert meta["status"] == "draft"
        assert meta["doc_title"] == "YARN 资源压力"
        assert meta["display_title"] == "Alpha - YARN - 资源压力"
        assert meta["alerts"] == ["YarnQueuePending"]
        assert meta["lint_error"] is None

    def test_doc_resolves_wikilinks(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/doc",
            params={"path": "alpha/yarn/alpha-yarn-pressure.md"}).json()
        assert body["wikilinks"] == [{
            "target": "alpha-hdfs-blocks", "label": "alpha-hdfs-blocks",
            "resolved_path": "alpha/hdfs/alpha-hdfs-blocks.md", "ambiguous": False,
        }]

    def test_canonical_source_metadata_is_exposed(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        manifest = ImportManifest.model_validate({
            "title": "YARN 队列知识",
            "type": "knowledge", "platform": "alpha", "service": "yarn",
            "problem": "queue-knowledge", "summary": "队列容量基础知识。",
            "sections": {
                "适用范围": "容量调度器", "关键知识": "队列有上下限。",
                "边界与注意事项": "不代表实时配置。", "参考": "内部配置。",
            },
        })
        target = repo_root / "knowledge/alpha/yarn/alpha-yarn-queue-knowledge.md"
        target.write_text(render_document(
            manifest, source="manual_import", source_ref="queue.md",
            source_sha256="abc",
        ), encoding="utf-8")
        client = TestClient(create_app(
            state_dir=tmp_path / "state", repo_root=repo_root,
        ))
        meta = client.get("/api/knowledge/doc", params={
            "path": "alpha/yarn/alpha-yarn-queue-knowledge.md",
        }).json()["meta"]
        assert meta["schema_version"] == 3
        assert meta["platform"] == "alpha"
        assert meta["source"] == "manual_import"
        assert meta["source_ref"] == "queue.md"
        assert meta["summary"] == "队列容量基础知识。"

    def test_index_doc_readable(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/doc", params={"path": "alpha/yarn/_index.md"}).json()
        assert body["meta"]["type"] == "index"
        assert body["meta"]["doc_title"] == "YARN (Alpha 平台)"

    def test_missing_doc_is_404(self, tmp_path: Path):
        response = client_at(tmp_path).get(
            "/api/knowledge/doc", params={"path": "alpha/yarn/nope.md"})
        assert response.status_code == 404

    def test_path_traversal_is_404(self, tmp_path: Path):
        """越界校验与 triage 报告接口同法。"""
        client = client_at(tmp_path)
        (tmp_path / "secret.md").write_text("绝密", encoding="utf-8")
        for attempt in ("../secret.md", "../../etc/passwd", "/etc/passwd"):
            response = client.get("/api/knowledge/doc", params={"path": attempt})
            assert response.status_code == 404, attempt

    def test_non_markdown_rejected(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        (repo_root / "knowledge" / "note.txt").write_text("x", encoding="utf-8")
        client = TestClient(create_app(state_dir=tmp_path / "state",
                                       repo_root=repo_root))
        assert client.get("/api/knowledge/doc",
                          params={"path": "note.txt"}).status_code == 404

    def test_broken_frontmatter_still_readable(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        (repo_root / "knowledge" / "alpha" / "yarn" / "broken.md").write_text(
            "---\ntitle: broken\ntype: nonsense\nplatform: alpha\n---\n# 坏文档\n正文\n",
            encoding="utf-8")
        client = TestClient(create_app(state_dir=tmp_path / "state",
                                       repo_root=repo_root))
        body = client.get("/api/knowledge/doc",
                          params={"path": "alpha/yarn/broken.md"}).json()
        assert body["meta"]["lint_error"]
        assert body["markdown"].startswith("# 坏文档")


def seed_task(state_dir: Path, task_id: str, **fields) -> None:
    folder = state_dir / "triage-tasks" / task_id
    folder.mkdir(parents=True, exist_ok=True)
    body = {"task_id": task_id, "run_id": f"ts-{task_id}", "status": "completed",
            "source": "manual", "platform": "alpha",
            "created_at": "2026-08-01T00:00:00+00:00",
            "matched_doc": None, "kb_doc": None, "consulted_docs": [], **fields}
    (folder / "task.json").write_text(json.dumps(body, ensure_ascii=False),
                                      encoding="utf-8")


def seed_conclusion(repo_root: Path, run_id: str, **fields) -> None:
    folder = repo_root / "records" / "troubleshoot" / run_id / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "conclusion.json").write_text(
        json.dumps({"verdict": "conclusive", **fields}, ensure_ascii=False),
        encoding="utf-8")


def seed_report(repo_root: Path, run_id: str, *, doc: str,
                verdict: str = "conclusive") -> None:
    folder = repo_root / "records" / "troubleshoot" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.json").write_text(json.dumps({
        "run_id": run_id, "platform": "alpha", "service": "yarn",
        "verdict": verdict, "trigger": "YARN 资源压力",
        "root_cause": "容器分配失败导致 YARN 资源压力",
        "evidence": ["容器分配失败"],
        "remediation": [f"按 {doc} 的处置建议处理"],
    }, ensure_ascii=False), encoding="utf-8")
    (folder / "report.md").write_text(
        f"# 报告\n\nVerdict: {verdict}\n\n参考 `{doc}`。\n",
        encoding="utf-8",
    )


class TestReferences:
    def test_task_relations_are_labelled_separately(self, tmp_path: Path):
        """路由命中 / 已查阅 / 产出必须分得开——三者含义完全不同。"""
        repo_root = repo_at(tmp_path)
        state_dir = tmp_path / "state"
        target = "alpha/yarn/alpha-yarn-pressure.md"
        seed_task(state_dir, "tr-matched", matched_doc=target)
        seed_task(state_dir, "tr-consulted", consulted_docs=[target])
        seed_task(state_dir, "tr-produced", kb_doc=target)
        seed_task(state_dir, "tr-both", matched_doc=target, consulted_docs=[target])
        seed_task(state_dir, "tr-unrelated", consulted_docs=["alpha/hdfs/alpha-hdfs-blocks.md"])
        client = TestClient(create_app(state_dir=state_dir, repo_root=repo_root))

        body = client.get("/api/knowledge/references", params={"path": target}).json()
        relations = {task["task_id"]: task["relations"] for task in body["tasks"]}
        assert relations == {
            "tr-matched": ["matched"],
            "tr-consulted": ["consulted"],
            "tr-produced": ["produced"],
            "tr-both": ["matched", "consulted"],
        }

    def test_tasks_sorted_newest_first(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        state_dir = tmp_path / "state"
        target = "alpha/yarn/alpha-yarn-pressure.md"
        seed_task(state_dir, "tr-old", kb_doc=target, created_at="2026-07-01T00:00:00+00:00")
        seed_task(state_dir, "tr-new", kb_doc=target, created_at="2026-08-08T00:00:00+00:00")
        client = TestClient(create_app(state_dir=state_dir, repo_root=repo_root))

        body = client.get("/api/knowledge/references", params={"path": target}).json()
        assert [task["task_id"] for task in body["tasks"]] == ["tr-new", "tr-old"]

    def test_conclusions_match_kb_doc_and_kb_draft(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        target = "alpha/yarn/alpha-yarn-pressure.md"
        seed_conclusion(repo_root, "ts-20260801-0101-aaaa", kb_doc=target,
                        kb_doc_effective=True)
        seed_conclusion(repo_root, "ts-20260802-0101-bbbb",
                        kb_draft={"doc": target}, verdict="needs_llm")
        seed_conclusion(repo_root, "ts-20260803-0101-cccc", kb_doc="other/doc.md")
        client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=repo_root))

        body = client.get("/api/knowledge/references", params={"path": target}).json()
        assert [(item["run_id"], item["via"], item["kb_doc_effective"])
                for item in body["conclusions"]] == [
            ("ts-20260802-0101-bbbb", "kb_draft", None),
            ("ts-20260801-0101-aaaa", "kb_doc", True),
        ]

    def test_case_lines_extracted_with_run_ids(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        target = repo_root / "knowledge" / "alpha" / "yarn" / "alpha-yarn-pressure.md"
        target.write_text(target.read_text(encoding="utf-8") + (
            "\n## 历史案例\n\n"
            "- 2026-07-13 node12 swap 残留 — `ts-20260713-2012-bba4`\n"
            "- 2026-07-27 node12 复发 — ts-20260727-0741-62be\n"
            "\n## 其他小节\n\n- 这行不算案例\n"
        ), encoding="utf-8")
        client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=repo_root))

        body = client.get("/api/knowledge/references",
                          params={"path": "alpha/yarn/alpha-yarn-pressure.md"}).json()
        assert [line["run_id"] for line in body["case_lines"]] == [
            "ts-20260713-2012-bba4", "ts-20260727-0741-62be"]
        # 后续小节的列表项不能被算成案例
        assert all("这行不算案例" not in line["text"] for line in body["case_lines"])

    def test_routed_alerts_included(self, tmp_path: Path):
        client = client_at(tmp_path)
        body = client.get("/api/knowledge/references",
                          params={"path": "alpha/yarn/alpha-yarn-pressure.md"}).json()
        assert body["alerts"] == ["YarnQueuePending"]

    def test_coverage_reports_the_cli_side_gap(self, tmp_path: Path):
        """session.json 没有 kb_doc 字段,回链覆盖不全——要如实说,不能装完整。"""
        repo_root = repo_at(tmp_path)
        for run_id in ("ts-a", "ts-b", "ts-c"):
            folder = repo_root / "records" / "troubleshoot" / run_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "session.json").write_text("{}", encoding="utf-8")
        seed_conclusion(repo_root, "ts-a", kb_doc="alpha/yarn/alpha-yarn-pressure.md")
        client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=repo_root))

        body = client.get("/api/knowledge/references",
                          params={"path": "alpha/yarn/alpha-yarn-pressure.md"}).json()
        assert body["coverage"]["sessions_total"] == 3
        assert body["coverage"]["sessions_linkable"] == 1

    def test_unreferenced_document_returns_empty_lists(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/references",
            params={"path": "alpha/hdfs/alpha-hdfs-blocks.md"}).json()
        assert body["tasks"] == [] and body["conclusions"] == []
        assert body["case_lines"] == [] and body["alerts"] == []

    def test_traversal_is_404(self, tmp_path: Path):
        assert client_at(tmp_path).get(
            "/api/knowledge/references",
            params={"path": "../secret.md"}).status_code == 404

    def test_corrupt_task_json_is_skipped_not_fatal(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        state_dir = tmp_path / "state"
        target = "alpha/yarn/alpha-yarn-pressure.md"
        seed_task(state_dir, "tr-good", kb_doc=target)
        broken = state_dir / "triage-tasks" / "tr-broken"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "task.json").write_text("{ not json", encoding="utf-8")
        client = TestClient(create_app(state_dir=state_dir, repo_root=repo_root))

        body = client.get("/api/knowledge/references", params={"path": target}).json()
        assert [task["task_id"] for task in body["tasks"]] == ["tr-good"]


class TestHealth:
    def test_status_and_type_counts_use_the_recall_scope(self, tmp_path: Path):
        """统计口径必须与召回索引一致:索引里没有的(_index/模板/oplog)不该计入。"""
        repo_root = repo_at(tmp_path)
        _write_doc(repo_root / "knowledge", "alpha/yarn/verified-one.md",
                   title="verified-one", platform="alpha", service="yarn",
                   body="# 已验证\n", status="verified")
        _write_doc(repo_root / "knowledge", "_templates/tpl.md", title="tpl",
                   platform="alpha", service="yarn", body="模板")
        _write_doc(repo_root / "knowledge", "alpha/yarn/log.md", title="log",
                   platform="alpha", service="yarn", body="记录", doc_type="oplog")
        client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=repo_root))

        body = client.get("/api/knowledge/health").json()
        assert body["documents_total"] == 4          # 3 篇 fixture + verified-one
        assert body["status_counts"] == {"draft": 3, "verified": 1, "other": 0}
        assert "oplog" not in body["type_counts"]

    def test_platform_coverage_breaks_down_alert_binding(self, tmp_path: Path):
        body = client_at(tmp_path).get("/api/knowledge/health").json()
        coverage = {row["platform"]: row for row in body["platform_coverage"]}
        assert coverage["alpha"]["documents"] == 2
        assert coverage["alpha"]["with_alerts"] == 1    # 只有 alpha-yarn-pressure 绑了告警
        assert coverage["beta"]["documents"] == 1

    def test_recall_orphans_are_troubleshoot_docs_with_no_symptoms_or_alerts(
            self, tmp_path: Path):
        """symptoms 与 alerts 皆空 = FTS 高权重列全空,agent 基本召不回。"""
        repo_root = repo_at(tmp_path)
        _write_doc(repo_root / "knowledge", "alpha/yarn/has-symptoms.md",
                   title="has-symptoms", platform="alpha", service="yarn",
                   symptoms="[队列积压]", body="# 有症状\n")
        _write_doc(repo_root / "knowledge", "alpha/yarn/has-alerts.md",
                   title="has-alerts", platform="alpha", service="yarn",
                   alerts="[YarnDown]", body="# 有告警\n")
        _write_doc(repo_root / "knowledge", "alpha/yarn/plain-knowledge.md",
                   title="plain-knowledge", platform="alpha", service="yarn",
                   body="# 纯知识\n", doc_type="knowledge")
        client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=repo_root))

        body = client.get("/api/knowledge/health").json()
        orphans = {item["path"] for item in body["recall_orphans"]["documents"]}
        assert "alpha/yarn/has-symptoms.md" not in orphans
        assert "alpha/yarn/has-alerts.md" not in orphans
        assert "alpha/yarn/plain-knowledge.md" not in orphans   # knowledge 类不参与
        # alpha-yarn-pressure 绑了 YarnQueuePending,alerts 非空故不是孤儿;
        # 另两篇 fixture troubleshoot 两个字段都空,是孤儿
        assert "alpha/yarn/alpha-yarn-pressure.md" not in orphans
        assert orphans == {"alpha/hdfs/alpha-hdfs-blocks.md", "beta/emqx/beta-emqx-drop.md"}
        assert body["recall_orphans"]["count"] == len(orphans)
        # 3 篇 fixture + has-symptoms + has-alerts,plain-knowledge 不是 troubleshoot
        assert body["recall_orphans"]["troubleshoot_total"] == 5

    def test_promotion_candidates_require_a_later_conclusive_aligned_report(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        state_dir = tmp_path / "state"
        referenced = "alpha/yarn/alpha-yarn-pressure.md"
        target = repo_root / "knowledge" / referenced
        target.write_text(target.read_text(encoding="utf-8") + (
            "\n## 历史案例\n\n"
            "- 2026-07-01 · `ts-20260701-0101-origin` · conclusive\n"
            "- 2026-08-01 · `ts-20260801-0101-later` · conclusive\n"
        ), encoding="utf-8")
        seed_conclusion(repo_root, "ts-20260801-0101-later", kb_doc=referenced)
        seed_report(repo_root, "ts-20260801-0101-later", doc=referenced)
        # verified 文档即便被引用也不是候选
        _write_doc(repo_root / "knowledge", "alpha/yarn/already-verified.md",
                   title="already-verified", platform="alpha", service="yarn",
                   body="# 已验证\n", status="verified")
        seed_task(state_dir, "tr-2", kb_doc="alpha/yarn/already-verified.md")
        client = TestClient(create_app(state_dir=state_dir, repo_root=repo_root))

        body = client.get("/api/knowledge/health").json()
        paths = [item["path"] for item in body["promotion_candidates"]]
        assert paths == [referenced]
        assert body["promotion_candidates"][0]["run_id"] == "ts-20260801-0101-later"
        assert body["promotion_candidates"][0]["attempts"][0]["explicit_doc_reference"] is True

    def test_plain_consultation_is_explained_but_not_promotable(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        state_dir = tmp_path / "state"
        referenced = "alpha/yarn/alpha-yarn-pressure.md"
        seed_task(state_dir, "tr-plain", consulted_docs=[referenced])
        client = TestClient(create_app(state_dir=state_dir, repo_root=repo_root))

        body = client.get("/api/knowledge/health").json()
        assert body["promotion_candidates"] == []
        review = body["promotion_reviews"][0]
        assert review["path"] == referenced
        assert review["eligible"] is False
        assert "完整 report/session 不可读" in review["attempts"][0]["reasons"]

    def test_report_path_without_content_alignment_is_not_promotable(self, tmp_path: Path):
        repo_root = repo_at(tmp_path)
        target = "alpha/yarn/alpha-yarn-pressure.md"
        doc = repo_root / "knowledge" / target
        doc.write_text(doc.read_text(encoding="utf-8") + (
            "\n## 历史案例\n\n"
            "- 2026-07-01 · `ts-20260701-0101-origin` · conclusive\n"
            "- 2026-08-01 · `ts-20260801-0101-later` · conclusive\n"
        ), encoding="utf-8")
        seed_conclusion(repo_root, "ts-20260801-0101-later", kb_doc=target)
        run_root = repo_root / "records" / "troubleshoot" / "ts-20260801-0101-later"
        (run_root / "session.json").write_text(json.dumps({
            "run_id": "ts-20260801-0101-later", "platform": "alpha",
            "service": "yarn", "verdict": "conclusive",
            "root_cause": "完全无关的证书过期", "evidence": ["certificate expired"],
            "remediation": ["renew certificate"],
        }, ensure_ascii=False), encoding="utf-8")
        (run_root / "report.md").write_text(
            f"参考 `{target}`，但内容完全无关。\n", encoding="utf-8")

        body = TestClient(create_app(
            state_dir=tmp_path / "state", repo_root=repo_root,
        )).get("/api/knowledge/health").json()
        assert body["promotion_candidates"] == []
        attempt = body["promotion_reviews"][0]["attempts"][0]
        assert attempt["explicit_doc_reference"] is True
        assert attempt["aligned"] is False

    def test_unreferenced_draft_is_not_a_promotion_candidate(self, tmp_path: Path):
        body = client_at(tmp_path).get("/api/knowledge/health").json()
        assert body["promotion_candidates"] == []
        assert body["promotion_reviews"] == []

    def test_health_reports_index_state(self, tmp_path: Path):
        body = client_at(tmp_path, with_index=False).get("/api/knowledge/health").json()
        assert body["index"]["available"] is False


class TestSearch:
    def test_search_spans_all_platforms_by_default(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/search", params={"q": "告警"}).json()
        assert body["hits"]

    def test_search_uses_display_title_without_changing_rank_fields(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/search", params={"q": "YARN 资源压力"}).json()
        hit = next(item for item in body["hits"]
                   if item["path"] == "alpha/yarn/alpha-yarn-pressure.md")
        assert hit["title"] == "alpha-yarn-pressure"
        assert hit["display_title"] == "Alpha - YARN - 资源压力"

    def test_search_platform_filter(self, tmp_path: Path):
        body = client_at(tmp_path).get(
            "/api/knowledge/search",
            params={"q": "掉线", "platform": "beta"}).json()
        assert {hit["platform"] for hit in body["hits"]} == {"beta"}

    def test_search_rejects_unknown_platform(self, tmp_path: Path):
        response = client_at(tmp_path).get(
            "/api/knowledge/search", params={"q": "掉线", "platform": "nope"})
        assert response.status_code == 422

    def test_search_rejects_blank_query(self, tmp_path: Path):
        assert client_at(tmp_path).get(
            "/api/knowledge/search", params={"q": ""}).status_code == 422

    def test_search_limit_bounds(self, tmp_path: Path):
        client = client_at(tmp_path)
        assert client.get("/api/knowledge/search",
                          params={"q": "告警", "limit": 51}).status_code == 422
        assert client.get("/api/knowledge/search",
                          params={"q": "告警", "limit": 0}).status_code == 422

    def test_missing_index_returns_503_not_empty(self, tmp_path: Path):
        """禁静默空结果:索引没建时搜索必须报 503 并提示重建。"""
        response = client_at(tmp_path, with_index=False).get(
            "/api/knowledge/search", params={"q": "告警"})
        assert response.status_code == 503
        assert "build_wiki" in response.json()["detail"]

    def test_stale_index_is_flagged(self, tmp_path: Path):
        """索引建好后再改源文件——树/正文实时,但搜索结果可能滞后,必须显形。"""
        repo_root = repo_at(tmp_path)
        target = repo_root / "knowledge" / "alpha" / "hdfs" / "alpha-hdfs-blocks.md"
        target.write_text(target.read_text(encoding="utf-8") + "\n新增段落\n",
                          encoding="utf-8")
        client = TestClient(create_app(state_dir=tmp_path / "state",
                                       repo_root=repo_root))
        body = client.get("/api/knowledge/search", params={"q": "告警"}).json()
        assert body["index_state"]["stale"] is True
        assert "build_wiki" in body["index_state"]["hint"]
