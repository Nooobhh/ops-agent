"""FastAPI application factory for the read-only alert console."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from lib.scheduler import Scheduler
from lib.envpack import EnvPackError, load_env_pack
from lib.graphs import GraphManager, graph_decls
from lib.config import resolve_credentials_path
from lib.data_root import local_config_path, resolve_data_root
from lib.datastore import open_datastore
from lib.inventory import load_inventory
from lib.triage_agent.model_registry import ModelRegistry
from lib.triage_agent.model_client import ModelConfig, OpenAICompatibleClient
from lib.triage_agent.task_classifier import TaskClassifier
from lib.triage_agent.service import TriageTaskService
from lib.triage_agent.supervisor import WorkerSupervisor
from lib.triage_agent.task_store import TaskStore
from lib.weekly_report_jobs import WeeklyReportJobManager
from lib.acquisition.settings import load_settings, resolve_storage_path
from lib.acquisition.store import latest_snapshots
from scripts import node_credentials
from web.server.routes.health import router as health_router
from web.server.routes.knowledge import router as knowledge_router
from web.server.routes.graphs import router as graphs_router
from web.server.routes.overview import router as overview_router
from web.server.routes.alerts import router as alerts_router
from web.server.routes.alert_config import router as alert_config_router
from web.server.routes.credentials import router as credentials_router
from web.server.routes.notify import router as notify_router
from web.server.routes.model_settings import router as model_settings_router
from web.server.routes.poll import router as poll_router
from web.server.routes.triage_tasks import router as triage_tasks_router
from web.server.routes.triage_health import router as triage_health_router
from web.server.routes.worklogs import router as worklogs_router
from web.server.routes.acquisition import router as acquisition_router
from web.server.routes.vector_settings import router as vector_settings_router
from web.server.routes.tool_catalog import router as tool_catalog_router
from web.server.routes.local_user import router as local_user_router
from web.server.routes.plugins import router as plugins_router
from web.server.routes.work_items import router as work_items_router


LOGGER = logging.getLogger(__name__)


class _SpaStaticFiles(StaticFiles):
    # 未注册的 api/ 路径必须原样 404,不能兜进 index.html:否则前端比后端新时,
    # 缺失的接口会返回 200 + HTML,报错现场是前端的「Unexpected token '<'」,
    # 看不出真实病因是进程没重启(常驻进程不热重载)。
    _NO_FALLBACK = ("assets/", "api/")

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # starlette 给的是系统分隔符路径(Windows 上为 api\xxx),先统一成 / 再比前缀
            if exc.status_code != 404 or str(path).replace("\\", "/").startswith(self._NO_FALLBACK):
                raise
            return await super().get_response("index.html", scope)


def create_app(
    *,
    state_dir: Path,
    repo_root: Path,
    chronic_hours: float = 24.0,
    triage_supervisor: Optional[WorkerSupervisor] = None,
) -> FastAPI:
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)
    triage_store = TaskStore(state_dir / "triage-tasks")
    datastore = open_datastore(repo_root)
    model_registry = ModelRegistry(local_config_path(repo_root, "model.local.yaml"))
    weekly_report_jobs = WeeklyReportJobManager()
    # 环境包声明的图谱;声明坏了只让图谱接口报错,不拖垮中台
    graph_load_error = ""
    try:
        pack = load_env_pack(repo_root)
        graph_managers = tuple(GraphManager(repo_root, state_dir, decl, pack) for decl in graph_decls(pack))
    except EnvPackError as exc:
        graph_managers, graph_load_error = (), str(exc)
    def classification_model_factory(selection):
        material = model_registry.client_material_for_selection(selection)
        return OpenAICompatibleClient(
            ModelConfig(
                base_url=material.base_url,
                api_key=material.api_key,
                model=str(selection["model_name"]),
                reasoning_effort=str(selection["reasoning_effort"]),
                timeout_seconds=min(material.timeout_seconds, 60),
                max_tool_turns=1,
            ),
            context={"client_role": "task_classifier"},
        )

    def target_ref_resolver(task) -> Optional[str]:
        try:
            inventory = load_inventory(repo_root)
            if task.scope == "cluster" or not task.node:
                return inventory.cluster_ref(task.platform)
            return inventory.host_ref(task.platform, task.node)
        except (OSError, ValueError):
            return None

    def polling_snapshot_reader(platform: str, node: Optional[str] = None):
        try:
            settings = load_settings(state_dir)
            root = resolve_storage_path(
                repo_root, settings["resources"]["snapshot_root"],
            )
            snapshot = latest_snapshots(root).get(platform)
            if not isinstance(snapshot, dict):
                return {"available": False, "platform": platform, "node": node}
            nodes = [
                item for item in snapshot.get("nodes") or []
                if not node or item.get("node") == node
            ]
            services = [
                item for item in snapshot.get("services") or []
                if not node or item.get("node") == node
            ]
            return {
                "available": True,
                "platform": platform,
                "node": node,
                "collected_at": snapshot.get("collected_at"),
                "status": snapshot.get("status"),
                "coverage": snapshot.get("coverage") or {},
                "nodes": nodes[:64],
                "services": services[:128],
                "errors": (snapshot.get("errors") or [])[:32],
            }
        except Exception:  # noqa: BLE001
            return {"available": False, "platform": platform, "node": node}

    triage_service = TriageTaskService(
        triage_store,
        model_registry=model_registry,
        classifier=TaskClassifier(
            routing_path=resolve_data_root(repo_root) / "build" / "routing.json",
            repo_root=repo_root,
        ),
        classification_model_factory=classification_model_factory,
        polling_snapshot_reader=polling_snapshot_reader,
        users=datastore.users,
        target_ref_resolver=target_ref_resolver,
    )

    def reconcile_triage_worker() -> None:
        if triage_supervisor is not None:
            triage_supervisor.reconcile(model_registry.is_configured())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        app.state.scheduler = scheduler
        app.state.triage_supervisor = triage_supervisor
        try:
            if sys.platform == "darwin":
                try:
                    refresh_result = await asyncio.to_thread(
                        node_credentials.run,
                        ["--credentials",
                         str(resolve_credentials_path(repo_root)),
                         "--profile",
                         str(local_config_path(repo_root, "auth-profiles.json"))],
                    )
                    if refresh_result != 0:
                        LOGGER.warning(
                            "web startup node credential initialization exited with code %s",
                            refresh_result,
                        )
                except Exception:
                    LOGGER.exception("web startup node credential initialization failed")
            scheduler = Scheduler(state_dir=state_dir, repo_root=repo_root)
            scheduler.attach_graphs(graph_managers)
            await scheduler.start()
            app.state.scheduler = scheduler
            reconcile_triage_worker()
            yield
        finally:
            if triage_supervisor is not None:
                triage_supervisor.stop()
            weekly_report_jobs.shutdown()
            if scheduler is not None:
                await scheduler.stop()
            else:
                for manager in graph_managers:
                    manager.stop()

    app = FastAPI(title="OpsAgent Alert Console", lifespan=lifespan)
    app.state.users = datastore.users
    # Graphs are intentionally kept as a single canonical
    # payload. Compress large JSON responses at the HTTP boundary so the
    # browser does not pay the full wire-size cost on every refresh.
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)
    app.include_router(health_router(state_dir=state_dir, repo_root=repo_root))
    app.include_router(overview_router(
        state_dir=state_dir, repo_root=repo_root, chronic_hours=chronic_hours,
    ))
    app.include_router(alerts_router(
        state_dir=state_dir, repo_root=repo_root, chronic_hours=chronic_hours,
    ))
    app.include_router(alert_config_router(state_dir=state_dir, repo_root=repo_root))
    app.include_router(credentials_router(repo_root=repo_root))
    app.include_router(poll_router(state_dir=state_dir, repo_root=repo_root))
    app.include_router(notify_router(state_dir=state_dir))
    app.include_router(model_settings_router(
        registry=model_registry,
        store=triage_store,
        reconcile=reconcile_triage_worker,
    ))
    app.include_router(triage_tasks_router(
        service=triage_service,
        repo_root=repo_root,
    ))
    app.include_router(triage_health_router(
        store=triage_store,
        supervisor=triage_supervisor,
        registry=model_registry,
    ))
    app.include_router(knowledge_router(repo_root=repo_root, state_dir=state_dir))
    app.include_router(graphs_router(
        state_dir=state_dir, managers=graph_managers, load_error=graph_load_error,
    ))
    app.include_router(worklogs_router(
        repo_root=repo_root,
        model_registry=model_registry,
        job_manager=weekly_report_jobs,
        task_store=triage_store,
    ))
    app.include_router(acquisition_router(state_dir=state_dir, repo_root=repo_root))
    app.include_router(vector_settings_router(repo_root=repo_root))
    app.include_router(tool_catalog_router(repo_root=repo_root, state_dir=state_dir))
    app.include_router(plugins_router(repo_root=repo_root, state_dir=state_dir))
    app.include_router(local_user_router(users=datastore.users))
    app.include_router(work_items_router(
        datastore=datastore, task_store=triage_store, model_registry=model_registry,
    ))

    build_dir = resolve_data_root(repo_root) / "build"
    wiki_path = build_dir / "wiki.html"
    if wiki_path.is_file():
        @app.get("/kb/", include_in_schema=False)
        def wiki_home():
            return FileResponse(wiki_path)

        app.mount("/kb", StaticFiles(directory=build_dir), name="knowledge-base")

    frontend_dir = repo_root / "web" / "frontend" / "dist"
    if frontend_dir.joinpath("index.html").is_file():
        app.mount("/", _SpaStaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:
        @app.get("/", include_in_schema=False, response_class=HTMLResponse)
        def frontend_not_built():
            return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>OpsAgent Web Console</title>
<style>body{font:16px/1.6 system-ui;margin:0;padding:48px;color:#1f2937;background:#f7f8fa}
main{max-width:720px;margin:auto}code{display:block;margin-top:16px;padding:16px;background:#111827;color:#e5e7eb}</style>
</head><body><main><h1>前端尚未构建</h1><p>请在仓库根目录完成前端构建后重新打开页面。</p>
<code>cd web/frontend &amp;&amp; npm ci &amp;&amp; npm run build</code></main></body></html>"""
    return app
