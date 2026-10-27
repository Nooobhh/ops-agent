"""In-process background jobs for weekly report generation."""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Callable
from uuid import uuid4

from lib.envpack import env_timezone


StageCallback = Callable[[str, str], None]
JobRunner = Callable[[StageCallback], dict[str, Any]]

ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed"})


class WeeklyReportJobConflict(RuntimeError):
    """Raised when the same report period is already being generated."""

    def __init__(self, existing: dict[str, Any]):
        super().__init__("该周期周报正在后台生成")
        self.existing = existing


class WeeklyReportJobNotFound(KeyError):
    pass


class WeeklyReportJobManager:
    """Run report generation while retaining status independently of a page."""

    def __init__(self, *, max_workers: int = 1) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="weekly-report",
        )

    def submit(
        self,
        *,
        start: date,
        end: date,
        operation: str,
        runner: JobRunner,
    ) -> dict[str, Any]:
        with self._lock:
            existing = next(
                (
                    item
                    for item in self._jobs.values()
                    if item["start_date"] == start.isoformat()
                    and item["end_date"] == end.isoformat()
                    and item["status"] in ACTIVE_STATUSES
                ),
                None,
            )
            if existing is not None:
                raise WeeklyReportJobConflict(copy.deepcopy(existing))

            now = _now()
            job_id = f"wr-{now[:10].replace('-', '')}-{uuid4().hex[:10]}"
            state = {
                "schema_version": 1,
                "job_id": job_id,
                "operation": operation,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "status": "queued",
                "current_stage": "排队中",
                "stage_status": "queued",
                "completed_stages": [],
                "report": None,
                "preview": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            self._jobs[job_id] = state
            self._executor.submit(self._run, job_id, runner)
            return copy.deepcopy(state)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise WeeklyReportJobNotFound(job_id)
            return copy.deepcopy(state)

    def list(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                copy.deepcopy(item)
                for item in self._jobs.values()
                if (start is None or item["start_date"] == start)
                and (end is None or item["end_date"] == end)
            ]
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def update_stage(self, job_id: str, stage: str, status: str) -> None:
        if status not in {"running", "completed", "failed"}:
            raise ValueError(f"无效的周报阶段状态: {status}")
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state["status"] in TERMINAL_STATUSES:
                return
            state["status"] = "running"
            state["current_stage"] = stage
            state["stage_status"] = status
            if status == "completed" and stage not in state["completed_stages"]:
                state["completed_stages"].append(stage)
            state["updated_at"] = _now()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job_id: str, runner: JobRunner) -> None:
        try:
            result = runner(
                lambda stage, status: self.update_stage(job_id, stage, status)
            )
            if not isinstance(result, dict):
                raise ValueError("周报后台任务未返回有效结果")
        except Exception as exc:  # noqa: BLE001
            self._fail(job_id, str(exc))
            return
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.update({
                "status": "completed",
                "current_stage": "已完成",
                "stage_status": "completed",
                "report": result.get("report"),
                "preview": result.get("preview"),
                "error": None,
                "updated_at": _now(),
            })

    def _fail(self, job_id: str, error: str) -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.update({
                "status": "failed",
                "stage_status": "failed",
                "error": error or "周报生成失败",
                "updated_at": _now(),
            })


def _now() -> str:
    return datetime.now(env_timezone()).isoformat(timespec="seconds")
