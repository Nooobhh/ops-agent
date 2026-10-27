"""节点 SSH 凭据缓存(credentials.json):按主机读写,写侧文件锁 + 原子替换。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from lib.file_lock import FileLock


class CredentialCache:
    def __init__(self, credentials_path: Path) -> None:
        self.credentials_path = Path(credentials_path)
        self._lock_path = Path(f"{self.credentials_path}.lock")

    def get(self, host: str) -> Optional[dict]:
        record = self._load().get(host)
        if record is None:
            return None
        result = dict(record)
        result.setdefault("valid_to", 0)
        return result

    def set(self, host: str, credential: dict) -> None:
        with FileLock(str(self._lock_path)):
            data = self._load(for_write=True)
            record = dict(credential)
            record["cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record["valid_to"] = int(record.get("valid_to") or 0)
            data[host] = record
            self._write_atomic(data)

    def delete(self, host: str) -> None:
        with FileLock(str(self._lock_path)):
            data = self._load(for_write=True)
            if host not in data:
                return
            del data[host]
            self._write_atomic(data)

    def list_hosts(self) -> List[str]:
        return sorted(self._load())

    def get_status(self) -> List[dict]:
        status = []
        for cache_host, record in sorted(self._load().items()):
            status.append({
                "host": cache_host,
                "target": record.get("target", cache_host),
                "bastion": f"{record.get('host', '')}:{record.get('port', 22)}",
                "cached_at": record.get("cached_at", ""),
                "mode": record.get("mode"),
                "valid_from": int(record.get("valid_from") or 0),
                "valid_to": int(record.get("valid_to") or 0),
            })
        return status

    def _load(self, *, for_write: bool = False) -> Dict[str, dict]:
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("顶层必须是 JSON object")
            return value
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"WARN credentials cache JSON 损坏: {self.credentials_path}: {exc}",
                  file=sys.stderr)
            if for_write:
                raise ValueError(f"拒绝覆盖损坏的 credentials cache: {self.credentials_path}") from exc
            return {}

    def _write_atomic(self, data: Dict[str, dict]) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.credentials_path.name}.",
            suffix=".tmp",
            dir=str(self.credentials_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.credentials_path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(tmp_name)
            except FileNotFoundError:
                pass
            raise
