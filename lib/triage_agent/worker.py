from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from lib.atomic_file import atomic_write_text
from lib.file_lock import FileLock


class WorkerAlreadyRunning(RuntimeError):
    pass


class TriageWorker:
    def __init__(
        self,
        *,
        store,
        orchestrator=None,
        orchestrator_factory: Optional[Callable] = None,
        state_dir: Path,
        worker_id: str,
        poll_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        session_reaper: Optional[Callable[[], None]] = None,
        session_closer: Optional[Callable[[], None]] = None,
        operation_poller: Optional[Callable[[], None]] = None,
    ):
        self.store = store
        self.orchestrator = orchestrator
        self.orchestrator_factory = orchestrator_factory
        self.state_dir = Path(state_dir)
        self.worker_id = worker_id
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._sleep = sleep
        self.session_reaper = session_reaper
        self.session_closer = session_closer
        # 写操作跟踪:每轮先查到期的 waiting_operation 任务(一次一个插件查询,不调模型)
        self.operation_poller = operation_poller
        self._stop_requested = False
        self._last_status: Optional[tuple] = None

    @contextmanager
    def process_lock(self):
        lock = FileLock(str(self.state_dir / "triage-worker.lock"), timeout=0)
        try:
            lock.__enter__()
        except TimeoutError as exc:
            raise WorkerAlreadyRunning("排查 worker 已在运行") from exc
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def run_once(self) -> bool:
        self._reap_sessions()
        self._poll_operations()
        task = self.store.claim_next(self.worker_id, lease_seconds=60)
        if task is None:
            self._write_status("idle", task_id=None)
            self._reap_sessions()
            return False
        self._write_status("running", task_id=task.task_id)
        try:
            orchestrator = (
                self.orchestrator_factory(task)
                if self.orchestrator_factory is not None
                else self.orchestrator
            )
            if orchestrator is None:
                raise RuntimeError("未配置排查 orchestrator")
            orchestrator.run(task.task_id)
        finally:
            self._write_status("idle", task_id=None)
            self._reap_sessions()
        return True

    def run_forever(self) -> None:
        self._stop_requested = False
        with self.process_lock():
            self._write_status("starting", task_id=None)
            try:
                while not self._stop_requested:
                    if not self.run_once():
                        self._sleep(self.poll_seconds)
            finally:
                self._reap_sessions()
                self._close_sessions()
                self._write_status("stopped", task_id=None)

    def stop(self) -> None:
        self._stop_requested = True

    def _reap_sessions(self) -> None:
        if self.session_reaper is not None:
            try:
                self.session_reaper()
            except Exception:
                # Reaping is a safety cleanup and must not make the worker
                # unable to claim or report the next Task.
                pass

    def _poll_operations(self) -> None:
        if self.operation_poller is not None:
            try:
                self.operation_poller()
            except Exception:
                # 跟踪失败只推迟该任务的下一次轮询,不能挡住 worker 领新任务
                pass

    def _close_sessions(self) -> None:
        if self.session_closer is not None:
            try:
                self.session_closer()
            except Exception:
                pass

    def _write_status(self, state: str, *, task_id: Optional[str]) -> None:
        # 空闲轮询每秒都会报 idle;只在状态变化时落盘,免得每秒一次 fsync
        if self._last_status == (state, task_id):
            return
        payload = {
            "schema_version": 1,
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "state": state,
            "task_id": task_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_text(
            self.state_dir / "triage-worker-status.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        self._last_status = (state, task_id)
