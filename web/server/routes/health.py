"""Health and runtime-data availability endpoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from lib.data_root import resolve_data_root
from lib.envpack import env_pack_commit, env_pack_origin, load_env_pack
from web.server.context import RequestContext, request_context
from web.server.data.consistency import check
from web.server.data.timeline_reader import timeline_health


def router(*, state_dir: Path, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)

    @routes.get("/api/health")
    def health(_context: RequestContext = Depends(request_context)) -> dict:
        timeline_path = state_dir / "timeline.db"
        state_path = state_dir / "alerts.json"
        build_dir = resolve_data_root(repo_root) / "build"
        routing_path = build_dir / "routing.json"
        wiki_path = build_dir / "wiki.html"
        frontend_path = repo_root / "web" / "frontend" / "dist" / "index.html"
        timeline = timeline_health(timeline_path)
        return {
            "status": "ok",
            "data_sources": {
                "timeline_db": timeline["status"] == "ok",
                "state_json": state_path.is_file(),
                "routing_json": routing_path.is_file(),
                "wiki_html": wiki_path.is_file(),
                "frontend_built": frontend_path.is_file(),
            },
            "timeline": timeline,
            "consistency": check(state_dir, timeline_path),
        }

    @routes.get("/api/env")
    def env(_context: RequestContext = Depends(request_context)) -> dict:
        """前端启动先取:业务时间按环境时区展示,不看浏览器时区;origin=example 时提示未挂载环境数据。"""
        pack = load_env_pack(repo_root)
        return {
            "schema_version": 1, "id": pack.id, "display_name": pack.display_name, "timezone": pack.timezone,
            "origin": env_pack_origin(repo_root, pack.root), "path": str(pack.root), "commit": env_pack_commit(pack.root),
            "clusters": [
                {"key": cluster.legacy_key, "display_name": cluster.display_name,
                 "short_name": cluster.short_name, "roles": list(cluster.roles)}
                for cluster in pack.clusters
            ],
        }

    return routes
