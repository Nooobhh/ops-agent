"""draft→verified 晋升:frontmatter 行级手术(正文零改动),lint 失败回滚。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from lib.data_root import resolve_data_root

from .bases import resolve_knowledge_root
from .frontmatter import FrontmatterError, load_doc
from .writer import KnowledgeWriteError, KnowledgeWriter

# 行级手术专用切分正则:闭合 --- 后只吃一个换行(不像共享 _FM_RE 的 \s* 会
# 贪婪吞掉闭合线后的空行),保证 text[m.end():] 正文字节级不变。
_FM_SPLIT_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)


class PromoteError(Exception):
    pass


class AlreadyVerified(PromoteError):
    # 语义是幂等跳过而非失败;调用方必须先 except AlreadyVerified 再 except PromoteError
    pass


def _apply_promotion(doc_rel: str, *, verified_by: str, reason: str,
                     repo_root: Path, today: Optional[str] = None,
                     state_dir: Optional[Path] = None) -> Dict[str, str]:
    repo_root = Path(repo_root)
    kb_root = resolve_knowledge_root(repo_root)
    verified_at = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def transform(original: Optional[bytes]) -> str:
        if original is None:
            raise PromoteError(f"文档不存在: {doc_rel}")
        text = original.decode("utf-8").lstrip("﻿")
        m = _FM_SPLIT_RE.match(text)
        if m is None:
            raise PromoteError(f"{doc_rel}: 缺少 frontmatter")
        path = kb_root / doc_rel
        try:
            doc = load_doc(path)
        except FrontmatterError as exc:
            raise PromoteError(str(exc)) from exc
        if doc.status == "verified":
            raise AlreadyVerified(doc_rel)
        if doc.status != "draft":
            raise PromoteError(f"{doc_rel}: status={doc.status!r},只有 draft 可晋升")
        if doc.type in {"procedure", "oplog"}:
            raise PromoteError(f"{doc_rel}: {doc.type} 类型禁止晋升")
        out_lines, replaced = [], False
        for line in m.group(1).split("\n"):
            if line.startswith(("verified_by:", "verified_at:", "verified_reason:")):
                continue
            if line.startswith("status:"):
                if replaced:
                    raise PromoteError(f"{doc_rel}: frontmatter 有重复 status 行")
                out_lines += [
                    "status: verified",
                    f"verified_by: {json.dumps(verified_by, ensure_ascii=False)}",
                    f'verified_at: "{verified_at}"',
                    f"verified_reason: {json.dumps(reason, ensure_ascii=False)}",
                ]
                replaced = True
            else:
                out_lines.append(line)
        if not replaced:
            raise PromoteError(f"{doc_rel}: frontmatter 无 status 行")
        return "---\n" + "\n".join(out_lines) + "\n---\n" + text[m.end():]

    writer = KnowledgeWriter(repo_root, state_dir or resolve_data_root(repo_root) / "state")
    try:
        writer.mutate(Path(doc_rel), transform)
    except KnowledgeWriteError as exc:
        raise PromoteError(str(exc)) from exc
    return {"doc": doc_rel, "verified_by": verified_by,
            "verified_at": verified_at, "verified_reason": reason}


def promote_manual(doc_rel: str, *, reason: str, repo_root: Path,
                   run_id: Optional[str] = None,
                   today: Optional[str] = None,
                   state_dir: Optional[Path] = None) -> Dict[str, str]:
    reason = str(reason or "").strip()
    if not reason:
        raise PromoteError("人工晋升必须填写 reason")
    verified_by = f"manual:{run_id}" if run_id else "manual"
    return _apply_promotion(
        doc_rel, verified_by=verified_by, reason=reason,
        repo_root=repo_root, today=today, state_dir=state_dir,
    )


def promote_auto(doc_rel: str, *, run_id: str, repo_root: Path,
                 today: Optional[str] = None,
                 state_dir: Optional[Path] = None) -> Dict[str, str]:
    from .promotion import evaluate_auto_promotion
    decision = evaluate_auto_promotion(repo_root, doc_rel, run_id)
    if not decision.eligible:
        raise PromoteError("自动晋升条件不满足: " + "; ".join(decision.reasons))
    reason = "后续中台排查 conclusive，历史案例与报告内容呼应"
    return _apply_promotion(
        doc_rel, verified_by=run_id, reason=reason,
        repo_root=repo_root, today=today, state_dir=state_dir,
    )
