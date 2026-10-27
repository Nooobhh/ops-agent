"""Rebuild the SQLite alert timeline from records/alerts JSONL files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.alerts.timeline import rebuild
from lib.acquisition.settings import load_settings, resolve_storage_path
from lib.data_root import resolve_data_root


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--db")
    parser.add_argument("--jsonl-root")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = resolve_data_root(repo_root) / state_dir
    settings = load_settings(state_dir)
    db_path = Path(args.db) if args.db else state_dir / "timeline.db"
    jsonl_root = Path(args.jsonl_root) if args.jsonl_root else resolve_storage_path(
        repo_root, settings["alerts"]["history_root"],
    )
    count = rebuild(db_path, jsonl_root)
    print(json.dumps({"ok": True, "inserted": count}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
