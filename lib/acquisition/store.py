"""Append-only daily JSONL snapshots, transparent gzip sealing and retention."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from lib.envpack import active_env_pack, env_timezone
from lib.file_lock import FileLock




def append_snapshot(root: Path, snapshot: dict) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    collected_at = _as_datetime(snapshot["collected_at"]).astimezone(env_timezone())
    path = root / f"{collected_at.date().isoformat()}.jsonl"
    line = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n"
    with FileLock(str(root / ".resource-snapshots.lock"), timeout=30):
        known = _ids_for_day(root, collected_at.date())
        if snapshot.get("snapshot_id") in known:
            return path
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    return path


def seal_completed_days(root: Path, *, now=None, compression="gzip") -> list[Path]:
    root = Path(root)
    if compression != "gzip" or not root.exists():
        return []
    today = _as_datetime(now or datetime.now(env_timezone())).astimezone(env_timezone()).date()
    sealed = []
    with FileLock(str(root / ".resource-snapshots.lock"), timeout=30):
        for source in sorted(root.glob("*.jsonl")):
            file_date = _file_date(source)
            if file_date is None or file_date >= today:
                continue
            destination = source.with_suffix(source.suffix + ".gz")
            _seal_one(source, destination)
            sealed.append(destination)
    return sealed


def prune_snapshots(root: Path, *, retention_days: int, now=None) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    today = _as_datetime(now or datetime.now(env_timezone())).astimezone(env_timezone()).date()
    cutoff = today - timedelta(days=int(retention_days))
    removed = []
    with FileLock(str(root / ".resource-snapshots.lock"), timeout=30):
        for path in sorted(root.iterdir()):
            if not path.is_file() or not (
                path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz")
            ):
                continue
            file_date = _file_date(path)
            if file_date is not None and file_date < cutoff:
                path.unlink()
                removed.append(path)
    return removed


def iter_snapshots(root: Path, *, since=None, platform: str | None = None) -> Iterator[dict]:
    root = Path(root)
    if not root.exists():
        return
    since_dt = _as_datetime(since) if since is not None else None
    paths = sorted(
        path for path in root.iterdir()
        if path.is_file() and (
            path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz")
        )
    )
    for path in paths:
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if platform and row.get("platform") != platform:
                        continue
                    try:
                        collected = _as_datetime(row.get("collected_at"))
                    except (TypeError, ValueError):
                        continue
                    if since_dt is not None and collected < since_dt:
                        continue
                    yield row
        except (OSError, EOFError):
            continue


def latest_snapshots(root: Path) -> dict[str, dict]:
    root = Path(root)
    latest: dict[str, dict] = {}
    if not root.exists():
        return latest
    paths = sorted(
        (path for path in root.iterdir() if path.is_file() and (
            path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz")
        )), reverse=True,
    )
    # 各 managed 集群都拿到最新快照即可停止向更早的文件回溯
    managed = set(active_env_pack().cluster_keys("managed"))
    for path in paths:
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    platform = str(row.get("platform") or "unknown")
                    previous = latest.get(platform)
                    if previous is None or str(row.get("collected_at")) > str(previous.get("collected_at")):
                        latest[platform] = row
        except (OSError, EOFError):
            continue
        if managed <= set(latest):
            break
    return latest


def storage_summary(root: Path) -> dict:
    root = Path(root)
    files = []
    total = 0
    if root.exists():
        for path in sorted(root.iterdir()):
            if path.is_file() and (
                path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz")
            ):
                size = path.stat().st_size
                total += size
                files.append({"name": path.name, "size_bytes": size})
    return {
        "files": files,
        "file_count": len(files),
        "total_bytes": total,
        "oldest": files[0]["name"] if files else None,
        "newest": files[-1]["name"] if files else None,
    }


def snapshot_id(platform: str, scheduled_at: str) -> str:
    material = f"resource-v1\0{platform}\0{scheduled_at}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _seal_one(source: Path, destination: Path) -> None:
    source_count, source_hash = _line_digest(source, compressed=False)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(source.parent))
    os.close(fd)
    temp = Path(name)
    try:
        with source.open("rb") as src, gzip.open(temp, "wb", compresslevel=6) as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
        with temp.open("rb+") as stream:
            os.fsync(stream.fileno())
        sealed_count, sealed_hash = _line_digest(temp, compressed=True)
        if (sealed_count, sealed_hash) != (source_count, source_hash):
            raise OSError("gzip 封存校验失败")
        os.replace(temp, destination)
        source.unlink()
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _line_digest(path: Path, *, compressed: bool) -> tuple[int, str]:
    opener = gzip.open if compressed else open
    digest = hashlib.sha256()
    count = 0
    with opener(path, "rb") as stream:
        for line in stream:
            digest.update(line)
            count += 1
    return count, digest.hexdigest()


def _ids_for_day(root: Path, day: date) -> set[str]:
    ids = set()
    for suffix in (".jsonl", ".jsonl.gz"):
        path = root / f"{day.isoformat()}{suffix}"
        if not path.is_file():
            continue
        opener = gzip.open if suffix.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line).get("snapshot_id")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if value:
                        ids.add(str(value))
        except (OSError, EOFError):
            continue
    return ids


def _file_date(path: Path) -> date | None:
    stem = path.name.split(".jsonl", 1)[0]
    try:
        return date.fromisoformat(stem)
    except ValueError:
        return None


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=env_timezone())
    return parsed
