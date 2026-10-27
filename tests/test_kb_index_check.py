import subprocess
from pathlib import Path

from scripts.check_kb_indexes import unstaged_indexes


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True)


def test_untracked_and_unstaged_indexes_are_reported(tmp_path):
    _git(tmp_path, "init", "-q")
    tracked = tmp_path / "alpha/hdfs/_index.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init")
    tracked.write_text("new", encoding="utf-8")
    untracked = tmp_path / "beta/emqx/_index.md"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("new", encoding="utf-8")
    assert unstaged_indexes(tmp_path) == ["alpha/hdfs/_index.md", "beta/emqx/_index.md"]


def test_staged_index_is_clean(tmp_path):
    _git(tmp_path, "init", "-q")
    path = tmp_path / "_index.md"
    path.write_text("new", encoding="utf-8")
    _git(tmp_path, "add", "_index.md")
    assert unstaged_indexes(tmp_path) == []
