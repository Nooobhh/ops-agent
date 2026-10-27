"""redlines.md 与有效红线规则的一致性锁。

redlines.md frontmatter 挂 filter_rules_sha256 = sha256(核心 filter_rules.yaml + 环境包红线补充);
任一侧改动未同步则 lint 拒绝入库。只有核心规则时即该文件本身的 sha256。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Optional

from .frontmatter import FrontmatterError, load_doc


def rules_sha256(sources: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sources:
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def check_redlines_hash(redlines_md: Path, sources: Iterable[Path]) -> Optional[str]:
    """校验 redlines.md 的 filter_rules_sha256 与有效规则来源的 hash 一致。

    sources 依次为核心规则文件与环境包补充(``lib.cmd_filter.redline_sources``)。
    返回错误消息(不一致 / 缺字段 / 文件不存在),None 表示 OK。
    """
    redlines_md = Path(redlines_md)
    sources = [Path(item) for item in sources]
    if not redlines_md.exists():
        return f"redlines.md 不存在: {redlines_md}"
    missing = [str(item) for item in sources if not item.exists()]
    if not sources or missing:
        return f"红线规则文件不存在: {', '.join(missing) or '(未给出)'}"
    try:
        doc = load_doc(redlines_md)
    except FrontmatterError as e:
        return f"redlines.md frontmatter 无法解析: {e}"

    declared = doc.raw.get("filter_rules_sha256")
    if not declared:
        return (f"{redlines_md}: 缺少 frontmatter 字段 filter_rules_sha256 "
                f"(应等于 sha256(核心规则 + 环境包红线补充))")

    actual = rules_sha256(sources)
    if str(declared).strip() != actual:
        return (f"{redlines_md}: filter_rules_sha256 不匹配\n"
                f"  声明: {declared}\n"
                f"  实际: {actual}\n"
                f"  → 规则来源 {', '.join(str(item) for item in sources)};请更新 redlines.md 的 hash,"
                f"同时同步规则清单说明段")
    return None
