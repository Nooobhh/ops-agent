"""Small cross-platform advisory file lock."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Optional


class FileLock:
    """An exclusive lock backed by a reusable lock file."""

    def __init__(self, path: str, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self._file: Optional[IO[bytes]] = None

    def __enter__(self) -> "FileLock":
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+b")
        if os.name == "nt":
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)

        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                self._acquire(lock_file)
                self._file = lock_file
                return self
            except OSError as exc:
                if not self._would_block(exc):
                    lock_file.close()
                    raise
                if time.monotonic() >= deadline:
                    lock_file.close()
                    raise TimeoutError(f"获取文件锁超时: {self.path}") from exc
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        lock_file = self._file
        self._file = None
        if lock_file is None:
            return None
        try:
            self._release(lock_file)
        finally:
            lock_file.close()
        return None

    @staticmethod
    def _acquire(lock_file: IO[bytes]) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release(lock_file: IO[bytes]) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _would_block(exc: OSError) -> bool:
        if os.name == "nt":
            return getattr(exc, "winerror", None) in {33, 36} or exc.errno in {13, 36}
        import errno

        return exc.errno in {errno.EACCES, errno.EAGAIN}
