"""知识库 1.0 格式迁移:frontmatter 的 platform 改写为资源引用,正文 schema v2 → v3。

只动 frontmatter,正文字节不变;写前逐篇自检归一后的平台/服务与其余字段不变,
任一篇不过整体不写。映射取活动环境包,幂等(再跑一次无变更)。
_index.md 的 frontmatter 按 index_sync 的生成格式整块重写(顺带把流式写法统一成块写法)。
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lib.atomic_file import atomic_write_bytes

from .bases import MANIFEST, discover_bases, iter_documents
from .contract import SCHEMA_VERSION
from .frontmatter import FrontmatterError, _legacy_platform, is_platform_ref, load_doc, platform_ref
from .index_sync import _frontmatter as index_frontmatter

# 闭合 --- 后只吃一个换行,保证 text[m.end():] 正文字节级不变(同 promote.py)
_FM_SPLIT_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
_PLATFORM_LINE_RE = re.compile(r"^platform:[ \t]*(\S+)[ \t]*$")
_VERSION_LINE_RE = re.compile(r"^schema_version:[ \t]*(\S+)[ \t]*$")
_INDEX_KEYS = {"title", "platform", "service"}


class MigrationError(RuntimeError):
    pass


class _Skip(Exception):
    pass


@dataclass
class FileChange:
    path: str                                   # 相对知识库根
    fields: list[tuple[str, str, str]] = field(default_factory=list)   # (字段, 旧值, 新值)
    new_text: str = ""


@dataclass
class MigrationPlan:
    changes: list[FileChange] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)       # (路径, 原因)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "changes": [{"path": item.path, "fields": [list(row) for row in item.fields]}
                        for item in self.changes],
            "skipped": [list(row) for row in self.skipped],
            "errors": [list(row) for row in self.errors],
            "summary": {"changed": len(self.changes), "skipped": len(self.skipped),
                        "errors": len(self.errors)},
        }


def _to_ref(value: str) -> str:
    if is_platform_ref(value):
        return value
    ref = platform_ref(value)
    if ref is None:
        raise MigrationError(f"platform {value!r} 不是当前环境的知识库平台")
    return ref


def _rewrite_lines(head: str, replace: dict[str, tuple[re.Pattern, str]]) -> tuple[str, list]:
    """按行替换顶层键的值;每个键必须恰好一行。返回 (新文本, 字段变更)。"""
    lines, changed = head.split("\n"), []
    for key, (pattern, new_value) in replace.items():
        hits = [index for index, line in enumerate(lines) if pattern.match(line)]
        if len(hits) != 1:
            raise MigrationError(f"顶层 {key} 应恰好一行(不带行尾注释),得到 {len(hits)} 行")
        old_value = pattern.match(lines[hits[0]]).group(1)
        if old_value != new_value:
            lines[hits[0]] = f"{key}: {new_value}"
            changed.append((key, old_value, new_value))
    return "\n".join(lines), changed


def _migrate_doc(text: str, doc) -> tuple[str, list]:
    match = _FM_SPLIT_RE.match(text)
    if match is None:
        raise MigrationError("frontmatter 须以单独的 --- 行开始和结束")
    head, body = match.group(1), text[match.end():]
    data = yaml.safe_load(head) or {}
    if doc.is_index:
        extra = set(data) - _INDEX_KEYS
        if extra:
            raise MigrationError(f"_index frontmatter 有未知键 {sorted(extra)},不能整块重写")
        if data.get("title") != "_index":
            raise MigrationError(f"_index title 应为 '_index',得到 {data.get('title')!r}")
        new_head = index_frontmatter(doc.platform, doc.service)
        if new_head == text[:match.end()]:
            return text, []
        old_platform = str(data.get("platform") or "")
        new_platform = (platform_ref(doc.platform) or "") if doc.platform else ""
        fields = ([("platform", old_platform, new_platform)] if old_platform != new_platform
                  else [("frontmatter", "旧写法", "块写法")])
        return new_head + body, fields
    version = data.get("schema_version")
    if version == SCHEMA_VERSION:
        return text, []
    if version != 2:
        raise _Skip(f"schema_version={version!r},只迁移 v2 文档")
    new_head, fields = _rewrite_lines(head, {
        "platform": (_PLATFORM_LINE_RE, _to_ref(str(data.get("platform")))),
        "schema_version": (_VERSION_LINE_RE, str(SCHEMA_VERSION)),
    })
    return "---\n" + new_head + "\n---\n" + body, fields


def _self_check(path: Path, old_text: str, new_text: str) -> None:
    """新旧文本:正文字节一致;解析后归一平台/服务与其余字段一致。"""
    if old_text[_FM_SPLIT_RE.match(old_text).end():] != new_text[_FM_SPLIT_RE.match(new_text).end():]:
        raise MigrationError("自检失败:正文发生变化")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / path.name               # 同名:load_doc 按文件名认 _index
        probe.write_text(new_text, encoding="utf-8")
        new_doc = load_doc(probe)
    old_doc = load_doc(path)
    ignored = {"platform", "schema_version"}
    old_raw = {k: v for k, v in old_doc.raw.items() if k not in ignored}
    new_raw = {k: v for k, v in new_doc.raw.items() if k not in ignored}
    if (old_doc.platform, old_doc.service, old_raw) != (new_doc.platform, new_doc.service, new_raw):
        raise MigrationError("自检失败:归一后的平台/服务或其余字段发生变化")


def _migrate_manifest(path: Path, text: str) -> tuple[str, list]:
    data = yaml.safe_load(text) or {}
    value = data.get("platform")
    if not value:
        return text, []
    content = text.rstrip("\n")
    new_content, fields = _rewrite_lines(content, {
        "platform": (_PLATFORM_LINE_RE, _to_ref(str(value))),
    })
    new_data = yaml.safe_load(new_content) or {}
    if (_legacy_platform(path, new_data.pop("platform")) != _legacy_platform(path, data.pop("platform"))
            or new_data != data):
        raise MigrationError("自检失败:kb.yaml 归一后的平台或其余字段发生变化")
    return new_content + text[len(content):], fields


def plan_migration(knowledge_root: Path) -> MigrationPlan:
    root = Path(knowledge_root).resolve()
    plan = MigrationPlan()
    for item in iter_documents(root):
        text = item.path.read_text(encoding="utf-8")
        try:
            new_text, fields = _migrate_doc(text, load_doc(item.path))
            if fields:
                _self_check(item.path, text, new_text)
                plan.changes.append(FileChange(item.doc_id, fields, new_text))
        except _Skip as exc:
            plan.skipped.append((item.doc_id, str(exc)))
        except (MigrationError, FrontmatterError, yaml.YAMLError) as exc:
            plan.errors.append((item.doc_id, str(exc)))
    for base in discover_bases(root):
        path = root / base.id / MANIFEST
        if not base.id or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            new_text, fields = _migrate_manifest(path, path.read_text(encoding="utf-8"))
            if fields:
                plan.changes.append(FileChange(relative, fields, new_text))
        except (MigrationError, FrontmatterError) as exc:
            plan.errors.append((relative, str(exc)))
    return plan


def apply_migration(knowledge_root: Path, plan: MigrationPlan) -> None:
    if plan.errors:
        raise MigrationError(f"有 {len(plan.errors)} 处错误,整体不写")
    root = Path(knowledge_root).resolve()
    for item in plan.changes:
        atomic_write_bytes(root / item.path, item.new_text.encode("utf-8"))
