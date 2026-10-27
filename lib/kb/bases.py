"""Knowledge bases under one knowledge root.

The knowledge root is a git repository that may live outside ops-agent.  Each
direct subdirectory carrying a ``kb.yaml`` is one knowledge base; a document id
is its path relative to the root, so it starts with the base id
(``cluster/hd/hdfs/...``).  A root without any ``kb.yaml`` is the legacy single
base: its id is empty and document ids keep the old ``hd/hdfs/...`` form.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import yaml

from lib.data_root import local_config_path, resolve_data_root

from .frontmatter import FrontmatterError, _legacy_platform

MANIFEST = "kb.yaml"
REDIRECTS = "_redirects.yaml"
LAYOUTS = {"platform_service", "flat"}
# 共享范围:一阶段只有 private(自用);二阶段 shared 库经共享服务同步给他人
SHARE_SCOPES = {"private", "shared"}
KNOWLEDGE_ROOT_ENV = "OPS_AGENT_KNOWLEDGE_ROOT"


class KnowledgeBaseError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeBase:
    id: str
    name: str
    description: str = ""
    # platform_service: <platform>/<service>/[<component>/]<doc>.md
    # flat: <doc>.md 或 <group>/<doc>.md,平台/服务取 kb.yaml 默认值与 frontmatter
    layout: str = "platform_service"
    platform: Optional[str] = None
    service: Optional[str] = None
    # flat 库按一级子目录分组:子目录名(大写)须等于该 frontmatter 字段;common_dir 放不分组的通用文档
    group_field: Optional[str] = None
    common_dir: Optional[str] = None
    # 确定性组装时必读的通用文档(common_dir 下文件名去 .md),见 lib/kb/retrieval/task_route.py
    resolve_common: tuple[str, ...] = ()
    share_scope: str = "private"

    def common_doc_ids(self) -> tuple[str, ...]:
        return tuple(f"{self.prefix}{self.common_dir}/{stem}.md" for stem in self.resolve_common)

    @property
    def prefix(self) -> str:
        return f"{self.id}/" if self.id else ""


LEGACY_BASE = KnowledgeBase(id="", name="知识库")


def resolve_knowledge_root(repo_root: Path) -> Path:
    """环境变量 → <数据根>/config/paths.local.yaml → <数据根>/knowledge;相对路径按数据根解析。"""
    data_root = resolve_data_root(repo_root).resolve()
    value = os.environ.get(KNOWLEDGE_ROOT_ENV, "").strip()
    if not value:
        config = local_config_path(repo_root, "paths.local.yaml")
        if config.is_file():
            data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise KnowledgeBaseError(f"{config}: 顶层必须是映射")
            value = str(data.get("knowledge_root") or "").strip()
    if not value:
        return data_root / "knowledge"
    path = Path(value).expanduser()
    return (path if path.is_absolute() else data_root / path).resolve()


def _load_manifest(directory: Path) -> KnowledgeBase:
    path = directory / MANIFEST
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise KnowledgeBaseError(f"{path}: 顶层必须是映射")
    base_id = str(data.get("id") or "").strip()
    if base_id != directory.name:
        raise KnowledgeBaseError(f"{path}: id {base_id!r} 必须与目录名 {directory.name!r} 一致")
    layout = str(data.get("layout") or "platform_service").strip()
    if layout not in LAYOUTS:
        raise KnowledgeBaseError(f"{path}: layout {layout!r} 不在 {sorted(LAYOUTS)}")
    name = str(data.get("name") or "").strip()
    if not name:
        raise KnowledgeBaseError(f"{path}: name 不能为空")
    resolve = data.get("resolve") or {}
    if data.get("group_field") and (layout != "flat" or not data.get("common_dir")):
        raise KnowledgeBaseError(f"{path}: group_field 只用于 flat 库,且须同时声明 common_dir")
    if resolve.get("common") and not data.get("common_dir"):
        raise KnowledgeBaseError(f"{path}: resolve.common 须配合 common_dir 使用")
    share_scope = str(data.get("share_scope") or "private").strip()
    if share_scope not in SHARE_SCOPES:
        raise KnowledgeBaseError(f"{path}: share_scope {share_scope!r} 不在 {sorted(SHARE_SCOPES)}")
    # platform 与文档同口径:资源引用或旧短键,内部归一为短键
    try:
        platform = _legacy_platform(path, str(data["platform"]).strip()) if data.get("platform") else None
    except FrontmatterError as exc:
        raise KnowledgeBaseError(str(exc)) from exc
    return KnowledgeBase(
        id=base_id, name=name,
        description=str(data.get("description") or "").strip(),
        layout=layout,
        platform=platform,
        service=(str(data["service"]).strip() if data.get("service") else None),
        group_field=(str(data["group_field"]).strip() if data.get("group_field") else None),
        common_dir=(str(data["common_dir"]).strip() if data.get("common_dir") else None),
        resolve_common=tuple(str(item).strip() for item in resolve.get("common") or ()),
        share_scope=share_scope,
    )


def discover_bases(knowledge_root: Path) -> tuple[KnowledgeBase, ...]:
    root = Path(knowledge_root)
    found = [
        _load_manifest(child)
        for child in (sorted(root.iterdir()) if root.is_dir() else ())
        if child.is_dir() and (child / MANIFEST).is_file()
    ]
    return tuple(found) if found else (LEGACY_BASE,)


def default_base(bases: Iterable[KnowledgeBase]) -> KnowledgeBase:
    """未指明库的写入(排查沉淀、手工导入)落到首个 platform_service 库。"""
    for base in bases:
        if base.layout == "platform_service":
            return base
    raise KnowledgeBaseError("没有 platform_service 布局的知识库可作为默认写入目标")


def split_doc_path(relative: str | PurePosixPath | Path,
                   bases: Iterable[KnowledgeBase]) -> tuple[Optional[KnowledgeBase], PurePosixPath]:
    """文档 ID → (所属库, 库内路径);不属于任何库(如根下 _templates)返回 (None, 原路径)。"""
    parts = PurePosixPath(Path(relative).as_posix()).parts
    by_id = {base.id: base for base in bases}
    if "" in by_id:
        return by_id[""], PurePosixPath(*parts)
    if parts and parts[0] in by_id:
        return by_id[parts[0]], PurePosixPath(*parts[1:])
    return None, PurePosixPath(*parts)


def load_redirects(knowledge_root: Path) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """→ (逐篇映射, 前缀映射);迁移留下的 _redirects.yaml 不存在时为空。"""
    path = Path(knowledge_root) / REDIRECTS
    if not path.is_file():
        return {}, ()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    docs = {str(k): str(v) for k, v in (data.get("docs") or {}).items()}
    prefixes = tuple(
        (str(k), str(v))
        for k, v in sorted((data.get("prefixes") or {}).items(), key=lambda kv: -len(str(kv[0])))
    )
    return docs, prefixes


def resolve_doc_id(knowledge_root: Path, doc_id: str) -> str:
    """历史文档 ID(迁移前路径)→ 现行 ID;已是现行或无法映射时原样返回。

    文档可能迁移多次(a/svc/x → svc/task/x → svc/transfer/x),
    _redirects.yaml 只记每次的增量,这里顺着映射走到存在的文件为止。
    """
    value = str(doc_id or "").strip().replace("\\", "/")
    root = Path(knowledge_root)
    docs, prefixes = load_redirects(root)
    seen: set[str] = set()
    while value and not (root / value).is_file() and value not in seen:
        seen.add(value)
        if value in docs:
            value = docs[value]
            continue
        prefix = next(((old, new) for old, new in prefixes if value.startswith(old)), None)
        if prefix is None:
            break
        value = prefix[1] + value[len(prefix[0]):]
    return value


@dataclass(frozen=True)
class DocumentPath:
    path: Path
    doc_id: str                 # 相对知识库根的 posix 路径
    base: KnowledgeBase
    inner: PurePosixPath        # 库内路径(去掉库前缀)


def iter_documents(knowledge_root: Path,
                   bases: Optional[tuple[KnowledgeBase, ...]] = None,
                   ) -> Iterable[DocumentPath]:
    """遍历库内 Markdown(含 _index.md);跳过模板、评估集、隐藏目录与根级仓库说明文件。"""
    root = Path(knowledge_root).resolve()
    bases = bases if bases is not None else discover_bases(root)
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in {"_templates", "_eval"} for part in relative.parts):
            continue
        if len(relative.parts) == 1 and relative.name != "_index.md":
            continue                # 根级 CLAUDE.md/README.md 等是仓库说明,不是知识文档
        base, inner = split_doc_path(relative, bases)
        if base is None or not inner.parts:
            continue
        yield DocumentPath(path=path, doc_id=relative.as_posix(), base=base, inner=inner)


GENERAL_SUFFIX = "-general"


def group_role(base: KnowledgeBase, inner: PurePosixPath, problem: str,
               process_id: Optional[int]) -> Optional[str]:
    """分组库内文档的组装角色:通用目录 common / 组通用文档 general(problem = <组>-general)/
    带 process_id 的特化 specific / 其余同组 related;非分组库返回 None。"""
    if not base.group_field or len(inner.parts) < 2:
        return None
    folder = inner.parts[0]
    if folder == base.common_dir:
        return "common"
    if process_id:
        return "specific"
    return "general" if str(problem or "") == f"{folder}{GENERAL_SUFFIX}" else "related"


def redlines_path(knowledge_root: Path) -> Path:
    """红线文档与 lib/data/filter_rules.yaml 双向锁,固定在默认库的 shared/procedures 下。"""
    root = Path(knowledge_root)
    return root / default_base(discover_bases(root)).prefix / "shared" / "procedures" / "redlines.md"


def doc_id_aliases(knowledge_root: Path, doc_id: str) -> frozenset[str]:
    """现行 ID 与所有映射到它的历史 ID;用于在历史任务/报告引用里认出同一篇文档。"""
    docs, prefixes = load_redirects(knowledge_root)
    aliases = {doc_id}
    for _ in range(10):                      # 多次迁移的反向闭包;10 层足够且防环
        grown = {old for old, new in docs.items() if new in aliases}
        grown.update(
            old + alias[len(new):]
            for old, new in prefixes for alias in aliases if alias.startswith(new)
        )
        if grown <= aliases:
            break
        aliases |= grown
    return frozenset(aliases)


def place_document(knowledge_root: Path, relative: Path,
                   base_id: Optional[str] = None, group: Optional[str] = None) -> Path:
    """按 platform/service 约定算出的相对路径 → 指定库(缺省默认库)内的文档 ID 路径。

    flat 库(如按任务类型分组的库)新文档落分组目录:有分组值(如 task_type)进同名小写目录,
    否则进通用目录;未声明分组的 flat 库落库根。
    """
    bases = discover_bases(knowledge_root)
    if base_id:
        base = next((item for item in bases if item.id == base_id), None)
        if base is None:
            raise KnowledgeBaseError(f"知识库不存在: {base_id}")
    else:
        base = default_base(bases)
    if base.layout == "flat":
        folder = (group or "").lower() if group else (base.common_dir or "")
        inner = Path(folder) / Path(relative).name if folder else Path(Path(relative).name)
    else:
        inner = Path(relative)
    return Path(base.prefix) / inner if base.prefix else inner
