from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lib.data_root import local_config_path, resolve_data_root
from lib.datastore import open_datastore
from lib.kb.bases import resolve_knowledge_root
from lib.envpack import load_env_pack


@dataclass(frozen=True)
class AppPaths:
    repo_root: Path
    state_dir: Path
    tasks_root: Path
    records_root: Path
    worklog_root: Path
    knowledge_root: Path
    routing_path: Path
    recall_index_path: Path
    retrieval_index_path: Path
    model_config: Path
    diagnostics_config: Path
    log_catalog_path: Path

    @classmethod
    def from_repo_root(
        cls,
        repo_root: Path,
        state_dir: Optional[Path] = None,
    ) -> "AppPaths":
        root = Path(repo_root).resolve()
        data_root = resolve_data_root(root)
        datastore = open_datastore(root)
        state = Path(state_dir).resolve() if state_dir is not None else data_root / "state"
        return cls(
            repo_root=root,
            state_dir=state,
            tasks_root=state / "triage-tasks",
            records_root=datastore.troubleshoot.root,
            worklog_root=datastore.worklogs.root,
            knowledge_root=resolve_knowledge_root(root),
            routing_path=data_root / "build" / "routing.json",
            recall_index_path=data_root / "build" / "kb-recall.sqlite",
            retrieval_index_path=data_root / "build" / "kb-retrieval.sqlite",
            model_config=local_config_path(root, "model.local.yaml"),
            diagnostics_config=load_env_pack(root).config_file("agent-diagnostics.yaml"),
            log_catalog_path=load_env_pack(root).config_file("log-catalog.yaml"),
        )
