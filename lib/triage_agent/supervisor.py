from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Protocol


class WorkerSupervisor(Protocol):
    def reconcile(self, configured: bool) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def health(self) -> Dict:
        pass


class LocalProcessSupervisor:
    def __init__(
        self,
        *,
        repo_root: Path,
        state_dir: Path,
        python_executable: str = sys.executable,
        popen: Callable = subprocess.Popen,
    ):
        self.repo_root = Path(repo_root)
        self.state_dir = Path(state_dir)
        self.python_executable = python_executable
        self._popen = popen
        self._process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        argv = [
            self.python_executable,
            str(self.repo_root / "scripts" / "triage_worker.py"),
            "--repo-root",
            str(self.repo_root),
            "--state-dir",
            str(self.state_dir),
        ]
        self._process = self._popen(argv, cwd=self.repo_root)

    def reconcile(self, configured: bool) -> None:
        if configured:
            self.start()
        else:
            self.stop()

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def health(self) -> Dict:
        process = self._process
        if process is None:
            return {"state": "stopped", "pid": None}
        return {
            "state": "running" if process.poll() is None else "stopped",
            "pid": process.pid,
        }
