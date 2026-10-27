"""Read-only HTTP adapter for browsing the knowledge base.

路由层零写路径:knowledge/ 只读不写。晋升等写操作仍是 lib.kb 的 CLI 唯一入口,
不在中台开口(2026-08-09 范围决策)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from lib.kb.browse import BROWSE_LIMIT_DEFAULT, BROWSE_LIMIT_MAX
from lib.kb.recall import RecallIndexError
from web.server.context import RequestContext, request_context
from lib.envpack.fields import KnowledgePlatform
from web.server.data import knowledge_reader
from web.server.data.knowledge_reader import DocumentNotFound, TopologyNotFound

DocType = Literal["troubleshoot", "knowledge", "procedure"]


def router(*, repo_root: Path, state_dir: Path) -> APIRouter:
    routes = APIRouter()
    repo_root = Path(repo_root).resolve()
    state_dir = Path(state_dir)

    @routes.get("/api/knowledge/tree")
    def get_tree(_context: RequestContext = Depends(request_context)):
        return knowledge_reader.tree(repo_root)

    @routes.get("/api/knowledge/doc")
    def get_doc(
        path: str = Query(min_length=1, max_length=400),
        _context: RequestContext = Depends(request_context),
    ):
        try:
            return knowledge_reader.read_doc(repo_root, path)
        except DocumentNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @routes.get("/api/knowledge/health")
    def get_health(_context: RequestContext = Depends(request_context)):
        return knowledge_reader.health(repo_root, state_dir)

    @routes.get("/api/knowledge/topology")
    def get_topology(_context: RequestContext = Depends(request_context)):
        try:
            return knowledge_reader.topology(repo_root)
        except TopologyNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @routes.get("/api/knowledge/references")
    def get_references(
        path: str = Query(min_length=1, max_length=400),
        _context: RequestContext = Depends(request_context),
    ):
        try:
            return knowledge_reader.references(repo_root, state_dir, path)
        except DocumentNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @routes.get("/api/knowledge/search")
    def get_search(
        q: str = Query(min_length=1, max_length=200),
        platform: Optional[KnowledgePlatform] = None,
        service: Optional[str] = Query(default=None, max_length=64),
        doc_type: Optional[DocType] = Query(default=None, alias="type"),
        limit: int = Query(default=BROWSE_LIMIT_DEFAULT, ge=1, le=BROWSE_LIMIT_MAX),
        _context: RequestContext = Depends(request_context),
    ):
        try:
            return knowledge_reader.search(
                repo_root, q, platform=platform, service=service,
                doc_type=doc_type, limit=limit,
            )
        except RecallIndexError as exc:
            # 索引缺失/损坏必须显形:静默返回空结果会被当成「知识库里没有」
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return routes
