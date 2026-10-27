from __future__ import annotations

from pathlib import Path

import pytest

from lib.kb.browse import (
    BROWSE_LIMIT_MAX,
    browse_search,
    build_tree,
    extract_h1,
    resolve_wikilinks,
    strip_frontmatter,
)
from lib.kb.recall import RecallIndexError, build_recall_index, search_recall_index


def _write_doc(root: Path, relative: str, *, title: str, platform: str,
               service: str, body: str, problem: str = "占位问题",
               symptoms: str = "[]", alerts: str = "[]",
               doc_type: str = "troubleshoot", status: str = "draft") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        f"type: {doc_type}\n"
        f"platform: {platform}\n"
        f"service: {service}\n"
        f"problem: {problem}\n"
        f"symptoms: {symptoms}\n"
        f"alerts: {alerts}\n"
        f"status: {status}\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    _write_doc(root, "alpha/yarn/alpha-yarn-pressure.md", title="alpha-yarn-pressure",
               platform="alpha", service="yarn",
               body="# YARN 资源压力\n\n容器分配失败，队列排队严重。")
    _write_doc(root, "alpha/hdfs/alpha-hdfs-blocks.md", title="alpha-hdfs-blocks",
               platform="alpha", service="hdfs",
               body="# HDFS 坏块\n\n坏块告警持续出现。")
    _write_doc(root, "beta/emqx/beta-emqx-drop.md", title="beta-emqx-drop",
               platform="beta", service="emqx",
               body="# EMQX 掉线\n\n设备大量掉线，队列排队严重。")
    _write_doc(root, "shared/system/shared-disk.md", title="shared-disk",
               platform="shared", service="system",
               body="# 磁盘容量\n\n磁盘使用率超阈值，队列排队严重。")
    # index / 模板 / eval 均不进树与索引
    (root / "alpha" / "yarn" / "_index.md").write_text(
        "---\n{title: _index, platform: alpha}\n---\n# YARN (Alpha 平台)\n\n概述。\n",
        encoding="utf-8")
    _write_doc(root, "_templates/tpl.md", title="tpl", platform="alpha",
               service="yarn", body="模板正文")
    _write_doc(root, "_eval/case.md", title="case", platform="alpha",
               service="yarn", body="评测用例")
    return root


@pytest.fixture
def index_path(knowledge_root: Path, tmp_path: Path) -> Path:
    return build_recall_index(knowledge_root, tmp_path / "build" / "kb.sqlite")


class TestBrowseSearch:
    def test_platform_none_searches_whole_library(self, index_path: Path):
        """人工浏览缺省不限平台——agent 侧则强制 platform IN (?, 'shared')。"""
        hits = browse_search(index_path, "队列排队严重")
        assert {hit.platform for hit in hits} == {"alpha", "beta", "shared"}

    def test_platform_filter_is_exact_not_union_with_shared(self, index_path: Path):
        """选定 alpha 就只看 alpha:不像 agent 召回那样隐式并上 shared。"""
        hits = browse_search(index_path, "队列排队严重", platform="alpha")
        assert {hit.platform for hit in hits} == {"alpha"}

    def test_service_and_type_filters(self, index_path: Path):
        assert [hit.path for hit in browse_search(
            index_path, "队列排队严重", service="emqx")] == ["beta/emqx/beta-emqx-drop.md"]
        assert browse_search(index_path, "队列排队严重", doc_type="knowledge") == ()

    def test_limit_exceeds_agent_top5_cap(self, index_path: Path):
        """browse 能超过 5 条——这正是不复用 search_recall_index 的原因。"""
        hits = browse_search(index_path, "队列排队严重", limit=10)
        assert len(hits) == 3
        agent_hits = search_recall_index(index_path, "队列排队严重", "alpha", limit=10)
        assert len(agent_hits) <= 5

    def test_limit_capped_at_browse_max(self, index_path: Path):
        assert len(browse_search(index_path, "队列排队严重", limit=999)) <= BROWSE_LIMIT_MAX

    def test_zero_limit_and_blank_query_return_empty(self, index_path: Path):
        assert browse_search(index_path, "队列排队严重", limit=0) == ()
        assert browse_search(index_path, "   ") == ()

    def test_hit_carries_title_and_snippet(self, index_path: Path):
        hit = browse_search(index_path, "坏块告警")[0]
        assert hit.path == "alpha/hdfs/alpha-hdfs-blocks.md"
        assert hit.title == "alpha-hdfs-blocks"
        assert hit.status == "draft"
        assert hit.snippet

    def test_index_docs_and_templates_absent(self, index_path: Path):
        paths = {hit.path for hit in browse_search(index_path, "概述 模板 评测")}
        assert not any(p.endswith("_index.md") for p in paths)
        assert not any(p.startswith(("_templates/", "_eval/")) for p in paths)

    def test_missing_index_fails_loudly(self, tmp_path: Path):
        """禁懒构建、禁静默空结果:索引不在必须炸并提示重建。"""
        with pytest.raises(RecallIndexError) as excinfo:
            browse_search(tmp_path / "nope.sqlite", "队列排队严重")
        assert "build_wiki" in str(excinfo.value)

    def test_corrupt_index_fails_loudly(self, tmp_path: Path):
        broken = tmp_path / "broken.sqlite"
        broken.write_bytes(b"not a database")
        with pytest.raises(RecallIndexError):
            browse_search(broken, "队列排队严重")


class TestBuildTree:
    def test_groups_by_platform_then_service(self, knowledge_root: Path):
        (legacy,), warnings = build_tree(knowledge_root)
        platforms = legacy.platforms
        assert warnings == ()
        assert [node.platform for node in platforms] == ["alpha", "beta", "shared"]
        alpha = platforms[0]
        assert [node.service for node in alpha.services] == ["hdfs", "yarn"]

    def test_index_doc_is_service_overview_not_a_list_item(self, knowledge_root: Path):
        (legacy,), _ = build_tree(knowledge_root)
        platforms = legacy.platforms
        yarn = next(node for node in platforms[0].services if node.service == "yarn")
        assert yarn.index_doc == "alpha/yarn/_index.md"
        assert yarn.index_title == "YARN (Alpha 平台)"
        assert [doc.path for doc in yarn.docs] == ["alpha/yarn/alpha-yarn-pressure.md"]

    def test_nested_index_does_not_shadow_service_overview(self, knowledge_root: Path):
        """alpha/hdfs/namenode/_index.md 是子目录索引,不能顶替 alpha/hdfs/_index.md。"""
        nested = knowledge_root / "alpha" / "hdfs" / "namenode" / "_index.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("---\n{title: _index, platform: alpha}\n---\n# NameNode\n",
                          encoding="utf-8")
        (knowledge_root / "alpha" / "hdfs" / "_index.md").write_text(
            "---\n{title: _index, platform: alpha}\n---\n# HDFS (Alpha 平台)\n",
            encoding="utf-8")
        (legacy,), _ = build_tree(knowledge_root)
        platforms = legacy.platforms
        hdfs = next(node for node in platforms[0].services if node.service == "hdfs")
        assert hdfs.index_doc == "alpha/hdfs/_index.md"
        assert hdfs.index_title == "HDFS (Alpha 平台)"
        assert [(item.component, item.index_doc) for item in hdfs.component_indexes] == [
            ("namenode", "alpha/hdfs/namenode/_index.md"),
        ]

    def test_root_index_is_not_a_service_overview(self, knowledge_root: Path):
        (knowledge_root / "_index.md").write_text(
            "---\n{title: _index}\n---\n# 全平台索引\n", encoding="utf-8")
        (legacy,), _ = build_tree(knowledge_root)
        platforms = legacy.platforms
        assert all(service.index_doc != "_index.md"
                   for platform in platforms for service in platform.services)

    def test_templates_eval_excluded_and_oplog_listed_as_category(self, knowledge_root: Path):
        _write_doc(knowledge_root, "alpha/yarn/oplog.md", title="oplog", platform="alpha",
                   service="yarn", body="操作记录", doc_type="oplog")
        (legacy,), _ = build_tree(knowledge_root)
        platforms = legacy.platforms
        docs = {doc.path: doc
                for platform in platforms
                for service in platform.services
                for doc in service.docs}
        assert not any(p.startswith(("_templates/", "_eval/")) for p in docs)
        assert docs["alpha/yarn/oplog.md"].category == "oplog"      # 操作记录是 5 类之一,在知识页可见
        assert docs["alpha/yarn/oplog.md"].sop_kind is None

    def test_doc_title_comes_from_first_h1(self, knowledge_root: Path):
        (legacy,), _ = build_tree(knowledge_root)
        platforms = legacy.platforms
        doc = next(node for node in platforms[0].services
                   if node.service == "yarn").docs[0]
        assert doc.title == "alpha-yarn-pressure"      # frontmatter
        assert doc.doc_title == "YARN 资源压力"      # 正文 H1

    def test_broken_frontmatter_degrades_to_warning(self, knowledge_root: Path):
        (knowledge_root / "alpha" / "yarn" / "broken.md").write_text(
            "---\ntitle: broken\ntype: nonsense\nplatform: alpha\n---\n正文\n",
            encoding="utf-8")
        (legacy,), warnings = build_tree(knowledge_root)
        platforms = legacy.platforms
        assert any("broken.md" in warning for warning in warnings)
        assert platforms                            # 其余文档照常成树

    def test_alerts_injected_from_routing(self, knowledge_root: Path):
        (legacy,), _ = build_tree(
            knowledge_root,
            alerts_by_doc={"alpha/yarn/alpha-yarn-pressure.md": ("YarnQueuePending",)})
        platforms = legacy.platforms
        doc = next(node for node in platforms[0].services
                   if node.service == "yarn").docs[0]
        assert doc.alerts == ("YarnQueuePending",)


class TestWikiLinks:
    def test_relative_path_slug_label_and_anchor(self, knowledge_root: Path):
        links = resolve_wikilinks(
            "见 [[alpha/hdfs/alpha-hdfs-blocks]] 与 [[beta-emqx-drop]] "
            "与 [[alpha/yarn/alpha-yarn-pressure|队列压力]] 与 [[alpha-hdfs-blocks#排查]]",
            knowledge_root)
        resolved = {link.target: link for link in links}
        assert resolved["alpha/hdfs/alpha-hdfs-blocks"].resolved_path == "alpha/hdfs/alpha-hdfs-blocks.md"
        assert resolved["beta-emqx-drop"].resolved_path == "beta/emqx/beta-emqx-drop.md"
        assert resolved["alpha/yarn/alpha-yarn-pressure"].label == "队列压力"
        # anchor 被丢弃,只按文档名解析(对齐 wiki.py:36)
        assert resolved["alpha-hdfs-blocks"].resolved_path == "alpha/hdfs/alpha-hdfs-blocks.md"

    def test_unresolved_target_kept_with_null_path(self, knowledge_root: Path):
        links = resolve_wikilinks("见 [[根本不存在的文档]]", knowledge_root)
        assert links[0].resolved_path is None

    def test_ambiguous_slug_flagged_but_still_resolves(self, tmp_path: Path):
        root = tmp_path / "knowledge"
        _write_doc(root, "alpha/yarn/health.md", title="health", platform="alpha",
                   service="yarn", body="A")
        _write_doc(root, "beta/emqx/health.md", title="health", platform="beta",
                   service="emqx", body="B")
        link = resolve_wikilinks("[[health]]", root)[0]
        assert link.resolved_path is not None      # 保持既有 wiki 的跳转行为
        assert link.ambiguous is True

    def test_full_path_target_never_ambiguous(self, tmp_path: Path):
        root = tmp_path / "knowledge"
        _write_doc(root, "alpha/yarn/health.md", title="health", platform="alpha",
                   service="yarn", body="A")
        _write_doc(root, "beta/emqx/health.md", title="health", platform="beta",
                   service="emqx", body="B")
        link = resolve_wikilinks("[[alpha/yarn/health]]", root)[0]
        assert link.resolved_path == "alpha/yarn/health.md"
        assert link.ambiguous is False

    def test_duplicate_links_deduped(self, knowledge_root: Path):
        links = resolve_wikilinks("[[beta-emqx-drop]] 又见 [[beta-emqx-drop]]",
                                  knowledge_root)
        assert len(links) == 1


class TestMarkdownHelpers:
    def test_extract_h1_skips_fenced_code(self):
        markdown = "```bash\n# 这是 shell 注释不是标题\n```\n\n# 真标题\n"
        assert extract_h1(markdown) == "真标题"

    def test_extract_h1_absent(self):
        assert extract_h1("## 只有二级标题\n") is None

    def test_strip_frontmatter_returns_raw_and_body(self):
        raw, body = strip_frontmatter("---\ntitle: x\n---\n# 正文\n")
        assert raw == "title: x"
        assert body == "# 正文\n"

    def test_strip_frontmatter_without_frontmatter(self):
        raw, body = strip_frontmatter("# 正文\n")
        assert raw == ""
        assert body == "# 正文\n"
