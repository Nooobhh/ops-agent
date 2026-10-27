"""Single atomic write core for every supported knowledge entry point."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from lib.atomic_file import atomic_write_bytes
from lib.cmd_filter import redline_sources
from lib.envpack import load_env_pack
from lib.file_lock import FileLock

from .bases import redlines_path, resolve_knowledge_root
from .lint import lint_kb
from .redlines import check_redlines_hash
from .rebuild import rebuild


class KnowledgeWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeWriteResult:
    path: Path
    routing_rebuilt: bool


class KnowledgeWriter:
    def __init__(
        self,
        repo_root: Path,
        state_dir: Path,
        *,
        validate: Optional[Callable[[], None]] = None,
        rebuild_fn: Optional[Callable[[], None]] = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.knowledge_root = resolve_knowledge_root(self.repo_root)
        self.state_dir = Path(state_dir)
        self._validate_fn = validate
        self._rebuild_fn = rebuild_fn

    def create(self, relative: Path, content: str) -> KnowledgeWriteResult:
        return self.mutate(relative, lambda original: self._require_new(original, content))

    def mutate(
        self,
        relative: Path,
        transform: Callable[[Optional[bytes]], str],
    ) -> KnowledgeWriteResult:
        target = self._target(relative)
        with FileLock(str(self.state_dir / "kb-write.lock")):
            original = target.read_bytes() if target.exists() else None
            rendered = transform(original)
            atomic_write_bytes(target, rendered.encode("utf-8"))
            try:
                self._run_validate()
                self._run_rebuild()
            except Exception as exc:
                self._restore(target, original)
                try:
                    self._run_rebuild()
                except Exception:
                    pass
                raise KnowledgeWriteError(
                    f"知识库写入回滚: {type(exc).__name__}: {exc}"
                ) from exc
        return KnowledgeWriteResult(path=target, routing_rebuilt=True)

    def _target(self, relative: Path) -> Path:
        relative = Path(relative)
        if relative.is_absolute() or relative.suffix != ".md":
            raise KnowledgeWriteError("知识文档路径必须是 knowledge/ 下的相对 .md 路径")
        target = (self.knowledge_root / relative).resolve()
        try:
            target.relative_to(self.knowledge_root)
        except ValueError as exc:
            raise KnowledgeWriteError("知识文档路径越界") from exc
        return target

    @staticmethod
    def _require_new(original: Optional[bytes], content: str) -> str:
        if original is not None:
            raise KnowledgeWriteError("知识文档已存在，手动导入不得覆盖")
        return content

    def _run_validate(self) -> None:
        if self._validate_fn is not None:
            self._validate_fn()
            return
        failures = [
            f"{path}: {'; '.join(errors)}"
            for path, errors in lint_kb(
                self.knowledge_root, repo_root=self.repo_root,
            )
            if errors
        ]
        redline_error = check_redlines_hash(
            redlines_path(self.knowledge_root),
            redline_sources(load_env_pack(self.repo_root)),
        )
        if redline_error:
            failures.append(redline_error)
        if failures:
            raise KnowledgeWriteError("kb-lint 失败: " + " | ".join(failures))

    def _run_rebuild(self) -> None:
        if self._rebuild_fn is not None:
            self._rebuild_fn()
        else:
            rebuild(self.repo_root)

    def _restore(self, target: Path, original: Optional[bytes]) -> None:
        if original is None:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
            except OSError:
                pass
        else:
            atomic_write_bytes(target, original)
