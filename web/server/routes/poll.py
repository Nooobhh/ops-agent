"""Poll gate control and source status endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from lib.alerts.poll_control import load_status, set_enabled
from lib.alerts.source_notify import configured_source_ids, load_source_config, source_notify
from lib.acquisition.settings import load_settings as load_acquisition_settings
from lib.acquisition.settings import save_settings as save_acquisition_settings
from web.server.context import RequestContext, request_context
from web.server.data.state_reader import load_last_tick_at, load_source_fail
from web.server.data.timeline_reader import latest_event_at, latest_source_errors


STALE_AFTER = timedelta(minutes=10)


class PollToggle(BaseModel):
    enabled: bool


def router(*, state_dir: Path, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)
    marker_path = state_dir / "poll-enabled.json"
    db_path = state_dir / "timeline.db"

    def status_payload(gate: dict | None = None, *, scheduler=None) -> dict:
        gate = gate or load_status(marker_path)
        last_tick_at = load_last_tick_at(state_dir) or latest_event_at(db_path)
        source_fail = load_source_fail(state_dir)
        errors = latest_source_errors(db_path)
        config = load_source_config(repo_root)
        notify = source_notify(config, state_dir)
        sources = {}
        for source in configured_source_ids(config):
            failures = int(source_fail.get(source, 0))
            error = errors.get(source) or {}
            sources[source] = {
                "last_result": "failure" if failures > 0 else "success",
                "consecutive_failures": failures,
                "last_error_at": error.get("ts"),
                "last_error": error.get("detail"),
                "notify": (notify.get(source) or {}).get("notify", True),
            }
        return {
            **gate,
            "runtime_state": _runtime_state(
                gate=gate,
                last_tick_at=last_tick_at,
                scheduler=scheduler,
            ),
            "last_tick_at": last_tick_at,
            "sources": sources,
        }

    @routes.get("/api/poll/status")
    def poll_status(
        request: Request,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        return status_payload(scheduler=getattr(request.app.state, "scheduler", None))

    @routes.post("/api/poll/toggle")
    def poll_toggle(
        body: PollToggle,
        request: Request,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        gate = set_enabled(marker_path, body.enabled)
        # Legacy installations may only have poll-enabled.json. Once the new
        # acquisition settings document exists, keep both controls in sync.
        if (state_dir / "acquisition-settings.json").is_file():
            settings = load_acquisition_settings(state_dir)
            settings["alerts"]["enabled"] = body.enabled
            save_acquisition_settings(state_dir, settings)
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            if body.enabled:
                scheduler.enable_poll()
            else:
                scheduler.disable_poll()
        return status_payload(gate, scheduler=scheduler)

    return routes


def _runtime_state(*, gate: dict, last_tick_at: str | None, scheduler) -> str:
    if not gate.get("effective"):
        return "stopped"
    if scheduler is None or not scheduler.is_poll_running():
        return "stopped"

    now = datetime.now(timezone.utc)
    started_at = scheduler.poll_started_at()
    last_tick = _parse_utc(last_tick_at)
    if isinstance(started_at, datetime):
        started_at = started_at.astimezone(timezone.utc)
        if last_tick is None or last_tick < started_at:
            if now - started_at <= STALE_AFTER:
                return "starting"
            return "stalled"

    if last_tick is None or now - last_tick > STALE_AFTER:
        return "stalled"
    return "running"


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
