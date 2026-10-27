"""Synchronize managed root, service and component index sections."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import json

import yaml

from lib.atomic_file import atomic_write_text
from lib.envpack import KNOWLEDGE_ENV_SCOPE, active_env_pack

from .bases import KnowledgeBase, discover_bases, split_doc_path
from .browse import service_label
from .frontmatter import FrontmatterError, load_doc, platform_ref
from .topology import component_service, knowledge_clusters


START = "<!-- GENERATED:KB-INDEX:START -->"
END = "<!-- GENERATED:KB-INDEX:END -->"
_LEGACY_SERVICE_SECTIONS = {"相关排查", "文档索引"}
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_NODE_NUM_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class PlatformLayout:
    """Observed component placement from one ``config/platform-<p>.yaml``."""

    source: str
    collected_at: str
    # service -> component -> sorted node names
    services: dict[str, dict[str, tuple[str, ...]]]
    # 以下来自集群拓扑快照(v3,环境包 config/topology/),缺失时为空
    topology_source: str = ""
    topology_system: str = ""                          # 快照来源系统名(快照 source_system)
    topology_collected_at: str = ""
    ports: dict[str, tuple[str, ...]] = None          # component -> ("8020 rpc", ...)
    # service -> (方向, 对端服务, 关系, 依据)
    dependencies: dict[str, tuple[tuple[str, str, str, str], ...]] = None


_RELATION_LABELS = {
    "depends_on": "依赖", "uses_service": "调用", "routes_to": "路由到",
    "observes": "监控采集", "audits_to": "审计写入",
}


def _snapshot_topology(path: Optional[Path], manual: list) -> tuple[dict, dict]:
    """v3 快照 + 人工登记 → (组件端口, 服务关系);不读知识库自身,避免生成区自我引用。"""
    data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.is_file() else {}
    if data and data.get("schema_version") != 3:
        data = {}
    ports: dict[str, list[tuple[int, str]]] = {}
    for item in data.get("component_ports") or []:
        ports.setdefault(str(item["component"]), []).append((int(item["port"]), str(item.get("purpose") or "")))
    edges: dict[tuple[str, str, str], list[str]] = {}
    for item in data.get("stack_dependencies") or []:
        edges.setdefault((item["source_service"], item["target_service"], "depends_on"), []).append("栈声明")
    for item in data.get("config_relations") or []:
        props = item.get("properties") or {}
        basis = f"配置 {props.get('config_type')}/{props.get('config_key')}"
        edges.setdefault((item["source_service"], item["target_service"], item["relation"]), []).append(basis)
    for item in manual:
        purpose = str(item.get("purpose") or "").strip()
        edges.setdefault((str(item["source"]), str(item["target"]), "depends_on"), []).append(
            f"人工登记：{purpose}" if purpose else "人工登记",
        )
    relations: dict[str, list[tuple[str, str, str, str]]] = {}
    for (source, target, relation), bases in edges.items():
        if source == target:
            continue
        label = _RELATION_LABELS.get(relation, relation)
        basis = "、".join(dict.fromkeys(bases))
        relations.setdefault(source, []).append(("上游", target, label, basis))
        relations.setdefault(target, []).append(("下游", source, label, basis))
    return (
        {component: tuple(f"{port} {purpose}".strip() for port, purpose in sorted(set(items)))
         for component, items in ports.items()},
        {service: tuple(sorted(rows)) for service, rows in relations.items()},
    )


def _node_sort_key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part
                 for part in _NODE_NUM_RE.split(name))


def load_platform_layouts(config_dir: Path) -> dict[str, PlatformLayout]:
    """Group observed components by KB service; missing files are skipped."""
    layouts: dict[str, PlatformLayout] = {}
    pack = active_env_pack()
    pack_root = Path(config_dir).parent  # 来源路径相对环境包根;调用方给的是该包的 config/
    for platform in knowledge_clusters():
        decl = pack.knowledge_cluster(platform)
        sources = decl.sources if decl is not None else {}
        path = pack_root / sources["hosts"] if "hosts" in sources else None
        if path is None or not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        placement: dict[str, dict[str, list[str]]] = {}
        for node, entry in (data.get("nodes") or {}).items():
            for component in (entry or {}).get("components") or []:
                service = component_service(str(component))
                if service:
                    placement.setdefault(service, {}).setdefault(
                        str(component), [],
                    ).append(str(node))
        # 生成区里的来源文字(config/topology/<文件名>)随文件名生成,文件不搬则输出不变
        snapshot = pack_root / sources["topology"] if "topology" in sources else None
        if snapshot is not None and not snapshot.is_file():
            snapshot = None
        manual_spec = pack.inventory.get("manual_dependencies")
        manual_path = pack_root / manual_spec if manual_spec else None
        manual = (yaml.safe_load(manual_path.read_text(encoding="utf-8")) or {}).get(platform) or [] \
            if manual_path is not None and manual_path.is_file() else []
        ports, dependencies = _snapshot_topology(snapshot, manual)
        topology_collected_at = topology_system = ""
        if snapshot is not None:
            snapshot_data = json.loads(snapshot.read_text(encoding="utf-8"))
            topology_collected_at = str(snapshot_data.get("collected_at") or "")[:10]
            topology_system = str(snapshot_data.get("source_system") or "")
        layouts[platform] = PlatformLayout(
            topology_source=f"config/topology/{snapshot.name}" if snapshot is not None else "",
            topology_collected_at=topology_collected_at,
            topology_system=topology_system,
            ports=ports,
            dependencies=dependencies,
            source=f"config/{path.name}",
            collected_at=str(data.get("collected_at") or "")[:10],
            services={
                service: {
                    component: tuple(sorted(nodes, key=_node_sort_key))
                    for component, nodes in sorted(components.items())
                }
                for service, components in placement.items()
            },
        )
    return layouts


def platform_label(platform: str) -> str:
    """有平台配置的(managed)集群叫「<简称> 平台」,其余集群用显示名。"""
    if platform == KNOWLEDGE_ENV_SCOPE:
        return "跨平台"
    decl = active_env_pack().knowledge_cluster(platform)
    if decl is None:
        return platform
    return f"{decl.short_name} 平台" if "managed" in decl.roles else decl.display_name


@dataclass(frozen=True)
class IndexDoc:
    base: str
    path: str
    platform: str
    service: str
    component: Optional[str]
    title: str
    doc_type: str
    status: str
    alerts: tuple[str, ...]
    playbooks: tuple[str, ...]


def _escape(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _label(value: str) -> str:
    return " ".join(part.upper() if len(part) <= 3 else part.title()
                    for part in value.replace("_", "-").split("-") if part)


def _component_label(component: str) -> str:
    branded = service_label(component)
    return branded if branded != component else _label(component)


def _link(path: str, label: str) -> str:
    target = path[:-3] if path.endswith(".md") else path
    # GFM tables treat a bare wikilink alias separator as a cell boundary.
    # Escaping it keeps the source valid for both the console and offline wiki.
    return f"[[{target}\\|{_escape(label)}]]"


def _documents(kb_root: Path, bases: tuple[KnowledgeBase, ...]) -> list[IndexDoc]:
    root = Path(kb_root).resolve()
    documents: list[IndexDoc] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if path.name == "_index.md" or "_templates" in relative.parts \
                or "_eval" in relative.parts:
            continue
        base, inner = split_doc_path(relative, bases)
        if base is None:
            continue
        try:
            doc = load_doc(path)
        except FrontmatterError:
            continue
        if doc.type == "oplog" or not doc.service:
            continue
        if base.layout == "platform_service":
            component = inner.parts[2] if len(inner.parts) >= 4 else None
        else:                               # flat 库:一级子目录即分组(common / 任务类型)
            component = inner.parts[0] if len(inner.parts) >= 2 else None
        documents.append(IndexDoc(
            base=base.id, path=relative.as_posix(), platform=str(doc.platform),
            service=str(doc.service), component=component,
            title=str(doc.title or path.stem), doc_type=str(doc.type or ""),
            status=str(doc.status or ""), alerts=tuple(doc.alerts),
            playbooks=tuple(
                str(item.get("path")) for item in doc.playbooks
                if item.get("path")
            ),
        ))
    return documents


def _strip_sections(markdown: str, names: set[str]) -> str:
    lines = markdown.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        match = _H2_RE.match(line.rstrip("\r\n"))
        if match:
            skipping = match.group(1).strip() in names
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "".join(kept).rstrip()


def _merge(existing: str, generated: str, *, root: bool = False) -> str:
    block = f"{START}\n\n{generated.strip()}\n\n{END}"
    if START in existing and END in existing:
        before, tail = existing.split(START, 1)
        _, after = tail.split(END, 1)
        return before.rstrip() + "\n\n" + block + after.rstrip() + "\n"
    if root:
        lines = existing.splitlines(keepends=True)
        cut = next((index for index, line in enumerate(lines)
                    if line.startswith("## ")), len(lines))
        manual = "".join(lines[:cut]).rstrip()
    else:
        manual = _strip_sections(existing, _LEGACY_SERVICE_SECTIONS)
    return manual.rstrip() + "\n\n" + block + "\n"


def _frontmatter(platform: Optional[str] = None,
                 service: Optional[str] = None) -> str:
    """platform 传旧短键,落盘写资源引用;迁移脚本按它规范化存量 _index。"""
    fields = ["title: _index"]
    if platform:
        fields.append(f"platform: {platform_ref(platform) or platform}")
    if service:
        fields.append(f"service: {service}")
    return "---\n" + "\n".join(fields) + "\n---\n"


def _new_root(base: KnowledgeBase) -> str:
    if base.layout == "flat":
        return _frontmatter(base.platform or KNOWLEDGE_ENV_SCOPE, base.service) + (
            f"# {base.name}\n\n{base.description or '本库概述待人工补充。'}\n"
        )
    return _frontmatter(KNOWLEDGE_ENV_SCOPE) + (
        f"# {base.name if base.id else '全平台知识路由'}\n\n"
        "> 本页总览由系统维护；服务说明请写在各服务概览的人工区域。\n"
    )


def _new_service(platform: str, service: str) -> str:
    return _frontmatter(platform, service) + (
        f"# {service_label(service)}（{platform_label(platform)}）\n\n"
        "## 概述\n\n服务概述待人工补充。\n"
    )


def _new_component(platform: str, service: str, component: str) -> str:
    return _frontmatter(platform, service) + (
        f"# {_component_label(component)}（{service_label(service)} / {platform_label(platform)}）\n\n"
        "## 概述\n\n组件概述待人工补充。\n"
    )


def _root_generated(documents: list[IndexDoc], prefix: str = "") -> str:
    lines = ["## 平台与服务", ""]
    groups: dict[tuple[str, str], list[IndexDoc]] = {}
    for doc in documents:
        groups.setdefault((doc.platform, doc.service), []).append(doc)
    for platform in active_env_pack().knowledge_platforms():
        platform_groups = [(key, value) for key, value in groups.items()
                           if key[0] == platform]
        if not platform_groups:
            continue
        lines.extend([
            f"### {platform_label(platform)}", "",
            "| 服务 | 文档 | 排查类 | 告警 | 草稿 | 已验证 | 概览 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ])
        for (_, service), docs in sorted(platform_groups):
            alerts = {alert for doc in docs for alert in doc.alerts}
            lines.append(
                f"| {service_label(service)} | {len(docs)} | "
                f"{sum(doc.doc_type == 'troubleshoot' for doc in docs)} | "
                f"{len(alerts)} | {sum(doc.status == 'draft' for doc in docs)} | "
                f"{sum(doc.status == 'verified' for doc in docs)} | "
                f"{_link(f'{prefix}{platform}/{service}/_index.md', '打开')} |"
            )
        lines.append("")

    alerts = sorted(
        (doc.platform, alert, doc.service, doc.path)
        for doc in documents for alert in doc.alerts
    )
    lines.extend(["## 精确告警路由", ""])
    if alerts:
        lines.extend([
            "| 平台 | 告警 | 服务 | 文档 |", "|---|---|---|---|",
            *[
                f"| {platform_label(platform)} | {_escape(alert)} | "
                f"{service_label(service)} | {_link(path, next(doc.title for doc in documents if doc.path == path))} |"
                for platform, alert, service, path in alerts
            ],
        ])
    else:
        lines.append("暂无精确告警路由。")
    return "\n".join(lines)


def _doc_table(documents: Iterable[IndexDoc]) -> list[str]:
    docs = list(documents)
    if not docs:
        return ["暂无文档。"]
    lines = [
        "| 文档 | 类型 | 状态 | 告警 | Playbook |",
        "|---|---|---|---|---|",
    ]
    for doc in sorted(docs, key=lambda item: (item.doc_type, item.title, item.path)):
        lines.append(
            f"| {_link(doc.path, doc.title)} | {_escape(doc.doc_type)} | "
            f"{_escape(doc.status)} | {_escape('、'.join(doc.alerts) or '—')} | "
            f"{_escape('、'.join(doc.playbooks) or '—')} |"
        )
    return lines


def _placement_table(layout: PlatformLayout, service: str) -> list[str]:
    components = layout.services.get(service)
    if not components:
        return []
    collected = f"（采集于 {layout.collected_at}）" if layout.collected_at else ""
    sources = f"`{layout.source}`" + (f" 与 `{layout.topology_source}`" if layout.topology_source else "")
    ports = layout.ports or {}
    lines = [
        "## 组件分布", "",
        f"> 由 {sources} 生成{collected}，节点与端口以此为准；"
        "人工区只写概述与经验，不维护节点、端口和依赖清单。", "",
        "| 组件 | 节点数 | 节点 | 端口 |", "|---|---:|---|---|",
    ]
    for component, nodes in components.items():
        lines.append(
            f"| {_escape(component)} | {len(nodes)} | {_escape(', '.join(nodes))} | "
            f"{_escape(' / '.join(ports.get(component, ())) or '—')} |"
        )
    return [*lines, ""]


def _dependency_table(layout: PlatformLayout, platform: str, service: str,
                      prefix: str, linkable: set[tuple[str, str]]) -> list[str]:
    rows = (layout.dependencies or {}).get(service)
    if not rows:
        return []
    collected = f"（采集于 {layout.topology_collected_at}）" if layout.topology_collected_at else ""
    source = f"`{layout.topology_source}` 与 " if layout.topology_source else ""
    lines = [
        "## 服务依赖", "",
        f"> 由 {source}`config/topology/manual-dependencies.yaml` 生成{collected}："
        f"{layout.topology_system + ' ' if layout.topology_system else ''}栈声明、配置推断与人工登记。"
        "上游故障可能表现为本服务异常，下游是本服务故障的影响面。", "",
        "| 方向 | 服务 | 关系 | 依据 |", "|---|---|---|---|",
    ]
    for direction, other, relation, basis in rows:
        label = service_label(other)
        cell = _link(f"{prefix}{platform}/{other}/_index.md", label) \
            if (platform, other) in linkable else _escape(label)
        lines.append(f"| {direction} | {cell} | {relation} | {_escape(basis)} |")
    return [*lines, ""]


def _service_generated(platform: str, service: str,
                       documents: list[IndexDoc],
                       layout: Optional[PlatformLayout] = None,
                       prefix: str = "",
                       linkable: Optional[set[tuple[str, str]]] = None) -> str:
    direct = [doc for doc in documents if doc.component is None]
    components = sorted({doc.component for doc in documents if doc.component})
    lines = [
        *(_placement_table(layout, service) if layout else []),
        *(_dependency_table(layout, platform, service, prefix, linkable or set()) if layout else []),
        "## 自动文档目录", "", "### 服务级", "", *_doc_table(direct),
    ]
    for component in components:
        component_docs = [doc for doc in documents if doc.component == component]
        component_index = f"{prefix}{platform}/{service}/{component}/_index.md"
        lines.extend([
            "", f"### {_component_label(component)}",
            "", f"组件概览：{_link(component_index, '打开')}", "",
            *_doc_table(component_docs),
        ])
    return "\n".join(lines)


def _flat_generated(base: KnowledgeBase, documents: list[IndexDoc]) -> str:
    """flat 库首页:按一级子目录分组,通用目录在前;未分组的库退回单表。"""
    groups = sorted({doc.component for doc in documents if doc.component},
                    key=lambda name: (name != base.common_dir, name))
    if not groups:
        return _component_generated(documents)
    lines = ["## 自动文档目录"]
    for group in groups:
        title = "通用" if group == base.common_dir else group.upper()
        lines.extend(["", f"### {title}", "",
                      *_doc_table(doc for doc in documents if doc.component == group)])
    loose = [doc for doc in documents if not doc.component]
    if loose:
        lines.extend(["", "### 未分组", "", *_doc_table(loose)])
    return "\n".join(lines)


def _component_generated(documents: list[IndexDoc]) -> str:
    return "\n".join(["## 自动文档目录", "", *_doc_table(documents)])


def _platform_service_indexes(root: Path, base: KnowledgeBase,
                              documents: list[IndexDoc],
                              layouts: dict[str, PlatformLayout]) -> dict[Path, str]:
    groups: dict[tuple[str, str], list[IndexDoc]] = {}
    component_groups: dict[tuple[str, str, str], list[IndexDoc]] = {}
    for doc in documents:
        groups.setdefault((doc.platform, doc.service), []).append(doc)
        if doc.component:
            component_groups.setdefault(
                (doc.platform, doc.service, doc.component), [],
            ).append(doc)

    base_root = root / base.prefix if base.prefix else root
    rendered: dict[Path, str] = {}
    root_index = base_root / "_index.md"
    rendered[root_index] = _merge(
        root_index.read_text(encoding="utf-8") if root_index.exists() else _new_root(base),
        _root_generated(documents, base.prefix), root=True,
    )
    for (platform, service), docs in groups.items():
        path = base_root / platform / service / "_index.md"
        existing = path.read_text(encoding="utf-8") if path.exists() \
            else _new_service(platform, service)
        rendered[path] = _merge(
            existing,
            _service_generated(platform, service, docs, layouts.get(platform), base.prefix, set(groups)),
        )
    for (platform, service, component), docs in component_groups.items():
        path = base_root / platform / service / component / "_index.md"
        existing = path.read_text(encoding="utf-8") if path.exists() \
            else _new_component(platform, service, component)
        if path.exists() and "组件概述待人工补充。" in existing:
            desired = (
                f"# {_component_label(component)}（{service_label(service)} / "
                f"{platform_label(platform)}）"
            )
            existing = re.sub(r"(?m)^#\s+.+$", desired, existing, count=1)
        rendered[path] = _merge(existing, _component_generated(docs))
    return rendered


def sync_indexes(kb_root: Path,
                 config_dir: Optional[Path] = None) -> tuple[str, ...]:
    """Atomically update every managed index, restoring all on failure.

    ``config_dir`` supplies ``platform-<p>.yaml`` so service indexes carry the
    observed component placement instead of hand-maintained node lists.
    """
    root = Path(kb_root).resolve()
    layouts = load_platform_layouts(config_dir) if config_dir else {}
    bases = discover_bases(root)
    documents = _documents(root, bases)
    rendered: dict[Path, str] = {}
    for base in bases:
        base_docs = [doc for doc in documents if doc.base == base.id]
        if base.layout == "flat":
            root_index = root / base.prefix / "_index.md"
            rendered[root_index] = _merge(
                root_index.read_text(encoding="utf-8") if root_index.exists() else _new_root(base),
                _flat_generated(base, base_docs),
            )
            continue
        rendered.update(_platform_service_indexes(root, base, base_docs, layouts))
    originals = {path: path.read_bytes() if path.exists() else None for path in rendered}
    changed = [path for path, text in rendered.items()
               if originals[path] != text.encode("utf-8")]
    try:
        for path in changed:
            atomic_write_text(path, rendered[path])
    except BaseException:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, original.decode("utf-8"))
        raise
    return tuple(path.relative_to(root).as_posix() for path in changed)
