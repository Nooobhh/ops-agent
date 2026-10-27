"""图谱(环境包 graphs 扩展)的读取与手动刷新接口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from lib.graphs import GraphError, GraphManager, read_graph
from web.server.context import RequestContext, request_context


def router(*, state_dir: Path, managers: tuple[GraphManager, ...], load_error: str = "") -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    by_id = {manager.decl.id: manager for manager in managers}

    def manager_of(graph_id: str) -> GraphManager:
        if load_error:
            raise HTTPException(status_code=500, detail=f"环境包图谱声明不可用: {load_error}")
        manager = by_id.get(graph_id)
        if manager is None:
            raise HTTPException(status_code=404, detail="图谱未在环境包声明")
        return manager

    @routes.get("/api/knowledge/graphs")
    def list_graphs(_context: RequestContext = Depends(request_context)):
        if load_error:
            raise HTTPException(status_code=500, detail=f"环境包图谱声明不可用: {load_error}")
        return {"graphs": [{"id": item.decl.id, "title": item.decl.title} for item in managers]}

    @routes.get("/api/knowledge/graphs/{graph_id}")
    def get_graph(graph_id: str, _context: RequestContext = Depends(request_context)):
        manager = manager_of(graph_id)
        graph = read_graph(state_dir, graph_id)
        if graph is None:
            raise HTTPException(status_code=503, detail=f"{manager.decl.title} 图谱尚未生成，请点击手动更新")
        return graph

    @routes.get("/api/knowledge/graphs/{graph_id}/status")
    def get_status(graph_id: str, _context: RequestContext = Depends(request_context)):
        return manager_of(graph_id).status()

    @routes.post("/api/knowledge/graphs/{graph_id}/refresh")
    def refresh(graph_id: str, _context: RequestContext = Depends(request_context)):
        manager = manager_of(graph_id)
        try:
            return manager.trigger("incremental")
        except (GraphError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return routes
