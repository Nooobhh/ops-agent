"""Cross-platform web-console scheduler for 24x7 acquisition jobs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.acquisition.maintenance import run_maintenance
from lib.acquisition.runtime import load_runtime, update_job
from lib.acquisition.settings import load_settings
from lib.alerts.poll_control import load_status
from lib.envpack import env_timezone, load_env_pack
from lib.graphs import GraphManager
from scripts import alert_poll as _alert_poll_script
from scripts import resource_collect as _resource_collect_script


LOGGER = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 300.0
RESOURCE_START_OFFSET_SECONDS = 15.0


class _ScriptCompatibility:
    def __init__(self, script, *, supports_repo_root: bool) -> None:
        self.script = script
        self.supports_repo_root = supports_repo_root

    def run(self, argv: list[str]) -> int:
        script_argv = list(argv)
        if not self.supports_repo_root:
            try:
                index = script_argv.index("--repo-root")
            except ValueError:
                pass
            else:
                del script_argv[index:index + 2]
        return self.script.run(script_argv)


alert_poll = _ScriptCompatibility(_alert_poll_script, supports_repo_root=False)
resource_collect = _ScriptCompatibility(_resource_collect_script, supports_repo_root=True)


class Scheduler:
    def __init__(self, state_dir: Path, repo_root: Path,
                 graphs: tuple[GraphManager, ...] = ()) -> None:
        self.state_dir = Path(state_dir)
        self.repo_root = Path(repo_root)
        self._poll_task: asyncio.Task | None = None
        self._resource_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._graph_tasks: list[asyncio.Task] = []
        self._graphs = tuple(graphs)
        self._poll_started_at: datetime | None = None
        self._resource_started_at: datetime | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    def attach_graphs(self, managers) -> None:
        """挂上环境包声明的图谱:声明了周期刷新的随调度器起周更循环。"""
        self._graphs = tuple(managers)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._reconcile_on_loop(initial=True)
        self._graph_tasks = [
            self._loop.create_task(self._graph_weekly_loop(manager), name=f"graph_{manager.decl.id}_weekly_loop")
            for manager in self._graphs if manager.decl.refresh == "weekly"
        ]

    async def stop(self) -> None:
        self._stopping = True
        tasks = [
            task for task in (
                self._poll_task, self._resource_task, self._maintenance_task,
                *self._graph_tasks,
            ) if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._poll_task = None
        self._resource_task = None
        self._maintenance_task = None
        self._graph_tasks = []
        self._poll_started_at = None
        self._resource_started_at = None
        self._loop = None
        for manager in self._graphs:
            manager.stop()

    def reconcile(self) -> None:
        if not self._stopping:
            self._call_on_scheduler_loop(lambda: self._reconcile_on_loop(initial=False))

    def enable_poll(self) -> None:
        if not self._stopping:
            self._call_on_scheduler_loop(self._enable_poll_on_loop)

    def disable_poll(self) -> None:
        self._call_on_scheduler_loop(self._disable_poll_on_loop)

    def is_poll_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    def is_resource_running(self) -> bool:
        return self._resource_task is not None and not self._resource_task.done()

    def poll_started_at(self) -> datetime | None:
        return self._poll_started_at

    def resource_started_at(self) -> datetime | None:
        return self._resource_started_at

    def _call_on_scheduler_loop(self, callback) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if self._loop is not None and running_loop is not self._loop:
            self._loop.call_soon_threadsafe(callback)
            return
        if self._loop is None and running_loop is not None:
            self._loop = running_loop
        callback()

    def _reconcile_on_loop(self, *, initial: bool) -> None:
        if self._stopping:
            return
        settings = load_settings(self.state_dir)
        gate = load_status(self.state_dir / "poll-enabled.json")
        alert_config_available = load_env_pack(self.repo_root).config_file("alert-sources.yaml").is_file()
        resource_config_available = load_env_pack(self.repo_root).config_file("resource-metrics.yaml").is_file()
        if settings["alerts"]["enabled"] and gate["effective"] and alert_config_available:
            self._ensure_poll(startup_delay=0)
        else:
            self._disable_poll_on_loop()
        if settings["resources"]["enabled"] and resource_config_available:
            self._ensure_resource(
                startup_delay=RESOURCE_START_OFFSET_SECONDS if initial else 0,
            )
        else:
            self._cancel_task("_resource_task")
            self._resource_started_at = None
        if settings["maintenance"]["enabled"]:
            if not initial and self._maintenance_task is not None:
                self._cancel_task("_maintenance_task")
            if self._maintenance_task is None or self._maintenance_task.done():
                loop = self._loop or asyncio.get_running_loop()
                self._maintenance_task = loop.create_task(
                    self._maintenance_loop(), name="acquisition_maintenance_loop",
                )
        else:
            self._cancel_task("_maintenance_task")

    def _enable_poll_on_loop(self) -> None:
        self._ensure_poll(startup_delay=0)

    def _disable_poll_on_loop(self) -> None:
        self._cancel_task("_poll_task")
        self._poll_started_at = None

    def _ensure_poll(self, *, startup_delay: float) -> None:
        if self._poll_task is None or self._poll_task.done():
            loop = self._loop or asyncio.get_running_loop()
            self._poll_started_at = datetime.now(timezone.utc)
            self._poll_task = loop.create_task(
                self._poll_loop(startup_delay=startup_delay), name="alert_poll_loop",
            )

    def _ensure_resource(self, *, startup_delay: float) -> None:
        if self._resource_task is None or self._resource_task.done():
            loop = self._loop or asyncio.get_running_loop()
            self._resource_started_at = datetime.now(timezone.utc)
            self._resource_task = loop.create_task(
                self._resource_loop(startup_delay=startup_delay),
                name="resource_collection_loop",
            )

    def _cancel_task(self, attribute: str) -> None:
        task = getattr(self, attribute)
        if task is not None and not task.done():
            task.cancel()
        setattr(self, attribute, None)

    async def _poll_loop(self, startup_delay: float = 0) -> None:
        await self._interval_loop(
            "alerts", alert_poll.run,
            ["--state-dir", str(self.state_dir), "--repo-root", str(self.repo_root)],
            startup_delay=startup_delay,
        )

    async def _resource_loop(self, startup_delay: float = RESOURCE_START_OFFSET_SECONDS) -> None:
        await self._interval_loop(
            "resources", resource_collect.run,
            ["--state-dir", str(self.state_dir), "--repo-root", str(self.repo_root)],
            startup_delay=startup_delay,
        )

    async def _interval_loop(self, job, runner, argv, *, startup_delay: float) -> None:
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + startup_delay
        try:
            while not self._stopping:
                delay = max(0.0, next_deadline - loop.time())
                if delay:
                    await asyncio.sleep(delay)
                if self._stopping:
                    break
                section = load_settings(self.state_dir)[job]
                if section["enabled"]:
                    await self._execute(job, runner, argv)
                interval = float(section["interval_seconds"])
                next_deadline += interval
                now = loop.time()
                skipped = 0
                while next_deadline <= now:
                    next_deadline += interval
                    skipped += 1
                if skipped:
                    current = ((load_runtime(self.state_dir).get("jobs") or {}).get(job) or {})
                    update_job(
                        self.state_dir, job,
                        skipped_ticks=int(current.get("skipped_ticks", 0)) + skipped,
                    )
        except asyncio.CancelledError:
            raise

    async def _execute(self, job, runner, argv) -> None:
        started = datetime.now(timezone.utc)
        update_job(self.state_dir, job, state="running", last_started_at=started.isoformat())
        try:
            result = await asyncio.to_thread(runner, list(argv))
            completed = datetime.now(timezone.utc)
            if result == 0:
                current = ((load_runtime(self.state_dir).get("jobs") or {}).get(job) or {})
                update_job(
                    self.state_dir, job, state="running",
                    last_completed_at=completed.isoformat(), last_success_at=completed.isoformat(),
                    duration_seconds=(completed - started).total_seconds(),
                    consecutive_failures=0, last_error=None,
                    skipped_ticks=int(current.get("skipped_ticks", 0)),
                )
            else:
                current = ((load_runtime(self.state_dir).get("jobs") or {}).get(job) or {})
                if not _completed_after(current.get("last_completed_at"), started):
                    self._record_failure(job, started, f"exit code {result}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(job, started, f"{type(exc).__name__}: {exc}")
            LOGGER.exception("scheduler %s iteration failed", job)

    def _record_failure(self, job, started, error) -> None:
        current = ((load_runtime(self.state_dir).get("jobs") or {}).get(job) or {})
        completed = datetime.now(timezone.utc)
        update_job(
            self.state_dir, job, state="degraded",
            last_completed_at=completed.isoformat(),
            duration_seconds=(completed - started).total_seconds(),
            consecutive_failures=int(current.get("consecutive_failures", 0)) + 1,
            last_error=error,
        )

    async def _maintenance_loop(self) -> None:
        try:
            while not self._stopping:
                maintenance = load_settings(self.state_dir)["maintenance"]
                await asyncio.sleep(_seconds_until(
                    int(maintenance["hour"]), int(maintenance["minute"]),
                ))
                if self._stopping:
                    break
                started = datetime.now(timezone.utc)
                update_job(
                    self.state_dir, "maintenance", state="running",
                    last_started_at=started.isoformat(),
                )
                try:
                    result = await asyncio.to_thread(
                        run_maintenance, self.repo_root, self.state_dir,
                    )
                    completed = datetime.now(timezone.utc)
                    update_job(
                        self.state_dir, "maintenance", state="running",
                        last_completed_at=completed.isoformat(),
                        last_success_at=completed.isoformat(),
                        duration_seconds=(completed - started).total_seconds(),
                        consecutive_failures=0, last_error=None, result=result,
                    )
                except Exception as exc:
                    self._record_failure(
                        "maintenance", started, f"{type(exc).__name__}: {exc}",
                    )
        except asyncio.CancelledError:
            raise

    async def _graph_weekly_loop(self, manager: GraphManager) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(_seconds_until_weekly(6, 3))
                if self._stopping:
                    break
                try:
                    manager.trigger("full")
                except Exception:
                    LOGGER.exception("weekly graph %s refresh failed to start", manager.decl.id)
        except asyncio.CancelledError:
            raise


def _seconds_until(hour: int, minute: int) -> float:
    
    zone = env_timezone()
    now = datetime.now(zone)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _seconds_until_weekly(weekday: int, hour: int, minute: int) -> float:
    
    zone = env_timezone()
    now = datetime.now(zone)
    days = (weekday - now.weekday()) % 7
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if days == 0 and target <= now:
        days = 7
    target += timedelta(days=days)
    return max(1.0, (target - now).total_seconds())


def _completed_after(value, started: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= started
