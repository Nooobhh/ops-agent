from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from lib.envpack import env_timezone
from lib.kb.bases import place_document, resolve_doc_id
from lib.kb.contract import ImportManifest, render_document, target_relative, triage_manifest
from lib.kb.promote import PromoteError, promote_auto
from lib.kb.writer import KnowledgeWriteError, KnowledgeWriter
from lib.record import TroubleshootSession, collect_evidence_fragments

from .errors import KbProposalRejected
from .paths import AppPaths
from .tools import KnowledgeProposal

__all__ = [
    "KbSinkError",
    "KnowledgeProposal",
    "KnowledgePreview",
    "KnowledgeSink",
    "KnowledgeResult",
]


class KbSinkError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeResult:
    path: Path
    routing_rebuilt: bool
    promotion: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class KnowledgePreview:
    """落档前给人看的草稿全文,与 persist 实际写入的内容同源渲染。

    diff:追加/修改既有文档时的 unified diff(供人工看清改了哪里);
    新建文档为 None。
    """

    path: Path
    mode: str
    markdown: str
    diff: Optional[str] = None
    evidence_issues: Tuple[str, ...] = ()


class KnowledgeSink:
    def __init__(
        self,
        paths: AppPaths,
        *,
        validate: Optional[Callable[[], None]] = None,
        rebuild: Optional[Callable[[], None]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.paths = paths
        self._writer = KnowledgeWriter(
            paths.repo_root, paths.state_dir,
            validate=validate, rebuild_fn=rebuild,
        )
        self._now = now or (
            lambda: datetime.now(env_timezone())
        )

    def preview(self, run_id: str, proposal: Dict) -> KnowledgePreview:
        """渲染草稿全文供人工确认,不落盘、不 lint、不 rebuild。"""
        # 证据问题在这里是人工审核意见,不是预览生成失败条件。
        parsed, session, target, evidence_issues = self._prepare(
            run_id, proposal, strict_evidence=False,
        )
        original = target.read_bytes() if target.exists() else None
        markdown = self._compose(
            parsed,
            run_id,
            session,
            target,
            original,
            allow_incomplete_evidence=bool(evidence_issues),
        )
        diff = None
        if original is not None:
            diff = "".join(difflib.unified_diff(
                original.decode("utf-8").splitlines(keepends=True),
                markdown.splitlines(keepends=True),
                fromfile=f"a/{target.name}",
                tofile=f"b/{target.name}",
            ))
        return KnowledgePreview(
            path=target,
            mode=parsed.mode,
            markdown=markdown,
            diff=diff,
            evidence_issues=tuple(evidence_issues),
        )

    def persist(
        self,
        run_id: str,
        proposal: Dict,
        *,
        allow_incomplete_evidence: bool = False,
    ) -> KnowledgeResult:
        parsed, session, target, _evidence_issues = self._prepare(
            run_id,
            proposal,
            strict_evidence=not allow_incomplete_evidence,
        )
        relative = target.relative_to(self.paths.knowledge_root.resolve())
        try:
            result = self._writer.mutate(
                relative,
                lambda original: self._compose(
                    parsed,
                    run_id,
                    session,
                    target,
                    original,
                    allow_incomplete_evidence=allow_incomplete_evidence,
                ),
            )
        except KnowledgeWriteError as exc:
            raise KbSinkError(str(exc)) from exc
        promotion = None
        if parsed.mode == "append_case":
            try:
                promotion = promote_auto(
                    relative.as_posix(), run_id=run_id,
                    repo_root=self.paths.repo_root,
                    state_dir=self.paths.state_dir,
                )
            except PromoteError:
                promotion = None
            if promotion:
                try:
                    TroubleshootSession(
                        dir=self.paths.records_root / run_id,
                        run_id=run_id,
                    ).record_kb_promotion(
                        doc=relative.as_posix(), verified_at=promotion["verified_at"],
                    )
                except Exception:
                    pass
        return KnowledgeResult(
            path=result.path,
            routing_rebuilt=result.routing_rebuilt,
            promotion=promotion,
        )

    def write_oplog(
        self,
        run_id: str,
        *,
        platform: str,
        service: str,
        problem: str,
        title: str,
        summary: str,
        sections: Dict[str, str],
    ) -> str:
        """人工批准的写操作结案时由 Harness 写 oplog 草稿(内容全是结构化事实,不经模型)。

        同一操作重入时文档已存在即原样保留(problem 含操作 id);返回 knowledge/<文档 ID>。
        """
        session = self._load_session(run_id)
        try:
            manifest = ImportManifest.model_validate({
                "title": title, "type": "oplog", "platform": platform, "service": service,
                "problem": problem, "summary": summary, "knowledge_layer": "manual_task",
                "entrypoints": ["manual_task"], "profile": "oplog", "execution_scope": "human_only",
                "confidence": "observed", "observed_at": self._now().isoformat(timespec="seconds"),
                "sections": sections,
            })
        except ValidationError as exc:
            raise KbSinkError(f"oplog 字段无效: {exc.errors()[0].get('msg')}") from exc
        # problem 以日期开头(oplog 契约),文件名须字母开头(slug 约定):文件名去掉日期前缀
        name = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", manifest.problem)
        relative = place_document(self.paths.knowledge_root, target_relative(manifest).with_name(f"{name}.md"))
        content = render_document(manifest, source="triage", source_ref=run_id, author=session.get("author"))
        try:
            self._writer.mutate(
                relative, lambda original: original.decode("utf-8") if original is not None else content,
            )
        except KnowledgeWriteError as exc:
            raise KbSinkError(str(exc)) from exc
        return f"knowledge/{relative.as_posix()}"

    def _prepare(
        self,
        run_id: str,
        proposal: Dict,
        *,
        strict_evidence: bool = True,
    ):
        try:
            parsed = KnowledgeProposal.model_validate(proposal)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors()[:3]
            )
            raise KbProposalRejected(f"知识草稿字段无效: {details}") from exc
        session = self._load_session(run_id)
        evidence_issues = self._evidence_issues(parsed.evidence, session)
        if strict_evidence and evidence_issues:
            raise KbProposalRejected(evidence_issues[0])
        return parsed, session, self._target(parsed), evidence_issues

    def _compose(
        self,
        parsed: KnowledgeProposal,
        run_id: str,
        session: Dict,
        target: Path,
        original: Optional[bytes],
        *,
        allow_incomplete_evidence: bool = False,
    ) -> str:
        if parsed.mode == "new":
            if original is not None:
                raise KbProposalRejected(
                    f"知识文档已存在: {target.name},请改用 mode=append_case 并提供 doc"
                )
            return self._render_new(
                parsed,
                run_id,
                session,
                allow_incomplete_evidence=allow_incomplete_evidence,
            )
        if original is None:
            raise KbProposalRejected(f"追加目标不存在: {parsed.doc}")
        return self._append_case(
            original.decode("utf-8"),
            parsed,
            run_id,
            session,
        )

    def _target(self, proposal: KnowledgeProposal) -> Path:
        if proposal.mode == "new":
            relative = place_document(self.paths.knowledge_root, Path(
                proposal.platform,
                proposal.service,
                f"{proposal.platform}-{proposal.service}-{proposal.problem}.md",
            ))
        else:
            if not proposal.doc:
                raise KbProposalRejected("append_case 必须提供 doc")
            relative = Path(resolve_doc_id(self.paths.knowledge_root, proposal.doc))
            if relative.is_absolute() or relative.suffix != ".md":
                raise KbProposalRejected("知识文档路径无效")
        target = (self.paths.knowledge_root / relative).resolve()
        try:
            target.relative_to(self.paths.knowledge_root.resolve())
        except ValueError as exc:
            raise KbProposalRejected("知识文档路径越界") from exc
        return target

    def _load_session(self, run_id: str) -> Dict:
        path = self.paths.records_root / run_id / "session.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KbSinkError(f"排查 session 不可读: {run_id}") from exc
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise KbSinkError(f"排查 session 结构无效: {run_id}")
        return value

    @staticmethod
    def _evidence_issues(evidence: List[str], session: Dict) -> List[str]:
        # 原始值收集 + 空白归一:
        # LLM 无法复刻列对齐空格,json.dumps 转义会让含引号证据永远匹配不上
        def norm(value: str) -> str:
            return re.sub(r"\s+", " ", value).strip()

        recorded_fragments = collect_evidence_fragments(session)
        recorded = "\n".join(recorded_fragments)
        command_outputs: List[tuple[str, str]] = []
        for step in session.get("steps") or []:
            if not isinstance(step, dict):
                continue
            command = norm(str(step.get("cmd") or ""))
            for key in ("stdout", "stderr"):
                output = step.get(key)
                if command and isinstance(output, str) and output.strip():
                    command_outputs.append((command, norm(output)))
        for result in session.get("playbook_results") or []:
            if not isinstance(result, dict):
                continue
            for step in result.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                command = norm(str(step.get("cmd") or ""))
                output = step.get("actual")
                if command and output is not None and str(output).strip():
                    command_outputs.append((command, norm(str(output))))

        def command_matches(value: str, command: str) -> bool:
            # Execution reports prefix commands with [node].  The model often
            # copies the command without that display-only prefix.
            without_node = re.sub(r"^\[[^\]]+\]\s*", "", command)
            return value == command or value == without_node or command.endswith(value)

        def contextual_match(item: str) -> bool:
            lines = [norm(line) for line in str(item).splitlines() if norm(line)]
            if len(lines) < 2:
                return False
            command_line, output_lines = lines[0], lines[1:]
            for command, output in command_outputs:
                if command_matches(command_line, command) and all(line in output for line in output_lines):
                    return True
            # A table/JSON block copied from a long command output may contain
            # selected rows or keys without the intervening samples.  Accept
            # them only when every line occurs in one recorded output; this
            # remains an owned runtime-output citation, not free text.
            for _command, output in command_outputs:
                if all(line in output for line in lines):
                    return True
            return False

        missing = [
            item for item in evidence
            if norm(item) not in recorded and not contextual_match(item)
        ]
        return [f"知识草稿引用未记录证据: {item}" for item in missing]

    def _render_new(
        self,
        proposal: KnowledgeProposal,
        run_id: str,
        session: Dict,
        *,
        allow_incomplete_evidence: bool = False,
    ) -> str:
        manifest = triage_manifest(
            proposal.model_dump(),
            session,
            self._case_line(run_id, session),
            confidence="unknown" if allow_incomplete_evidence else "verified",
        )
        return render_document(
            manifest, source="triage", source_ref=run_id, author=session.get("author"),
        )

    def _append_case(
        self,
        current: str,
        proposal: KnowledgeProposal,
        run_id: str,
        session: Dict,
    ) -> str:
        block = self._case_block(proposal, run_id, session)
        start_marker = f"<!-- KB-CASE:{run_id}:START -->"
        end_marker = f"<!-- KB-CASE:{run_id}:END -->"
        start = current.find(start_marker)
        if start >= 0:
            end = current.find(end_marker, start)
            if end >= 0:
                end += len(end_marker)
                return current[:start] + block + current[end:]
        lines = current.splitlines(keepends=True)
        history_start = None
        history_end = len(lines)
        for index, line in enumerate(lines):
            content = line.rstrip("\r\n")
            if content == "## 历史案例":
                history_start = index + 1
                continue
            if history_start is not None and content.startswith("## "):
                history_end = index
                break
        if history_start is None:
            # 老文档没有历史案例段:在文末补建,append 对存量文档同样可用
            return current.rstrip() + "\n\n## 历史案例\n\n" + block + "\n"
        case = self._case_line(run_id, session)
        if any(case in line for line in lines[history_start:history_end]):
            return current
        if history_end < len(lines):
            insert_at = history_end
            while insert_at > history_start and not lines[insert_at - 1].strip():
                insert_at -= 1
            return (
                "".join(lines[:insert_at]) + block + "\n"
                + "".join(lines[insert_at:])
            )
        return current.rstrip() + "\n" + block + "\n"

    def _case_block(
        self,
        proposal: KnowledgeProposal,
        run_id: str,
        session: Dict,
    ) -> str:
        """Render a revision-visible case block; raw commands stay in report."""
        title = str(proposal.title or f"排查案例 {run_id}").strip()
        lines = [
            f"<!-- KB-CASE:{run_id}:START -->",
            f"### {title}",
            self._case_line(run_id, session),
            f"- **完整排查报告**: [[report:{run_id}|查看完整排查报告]]",
        ]
        if proposal.summary:
            lines.append(f"- **摘要**: {proposal.summary.strip()}")
        if proposal.symptoms:
            lines.append("- **现象**: " + "；".join(item.strip() for item in proposal.symptoms))
        if proposal.mechanism:
            lines.append(f"- **原因/机理**: {proposal.mechanism.strip()}")
        if proposal.evidence:
            lines.append("- **关键证据摘要**:")
            lines.extend(f"  - {item.strip()}" for item in proposal.evidence)
        else:
            lines.append("- **关键证据摘要**: 当前未提交可验证证据，详见排查报告。")
        if proposal.remediation:
            lines.append("- **解决方案/建议**:")
            lines.extend(f"  - {item.strip()}" for item in proposal.remediation)
        if proposal.playbooks:
            lines.append("- **Playbook**:")
            lines.extend(
                f"  - `{item.get('path') or item}`" for item in proposal.playbooks
            )
        lines.append(f"<!-- KB-CASE:{run_id}:END -->")
        return "\n".join(lines)

    def _case_line(self, run_id: str, session: Dict) -> str:
        date = self._now().astimezone(
            env_timezone()
        ).strftime("%Y-%m-%d")
        verdict = str(session.get("verdict") or "insufficient")
        return (
            f"- {date} · `{run_id}` · {verdict} · "
            f"`records/troubleshoot/{run_id}/report.md`"
        )
