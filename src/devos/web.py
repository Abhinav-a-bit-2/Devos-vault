"""Local FastAPI transport over the same vault and ingestion services as the CLI."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from devos import __version__
from devos.core import ENTITY_TYPES, RELATIONS, DevosError
from devos.jobs import JobRunner
from devos.ports import Transport
from devos.services import IngestService, VaultService
from devos.storage import SQLiteStore


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityInput(InputModel):
    type: str
    name: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=100000)
    url: str | None = Field(default=None, max_length=2048)
    body: str | None = Field(default=None, max_length=500000)


class EntityEdit(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=100000)
    body: str | None = Field(default=None, max_length=500000)


class LinkInput(InputModel):
    subject: str
    relation: str
    object: str
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class IngestInput(InputModel):
    inputs: list[str] = Field(min_length=1, max_length=100)
    adapter: str | None = None
    offline: bool = False
    replay: bool = False


class EntitySummary(BaseModel):
    id: str
    type_key: str
    name: str
    description: str = ""
    canonical_url: str | None = None
    reference_only: bool = False
    connections: int = 0


class EntityPage(BaseModel):
    items: list[EntitySummary]
    total: int
    limit: int
    offset: int


class Relation(BaseModel):
    id: str
    subject_id: str
    object_id: str
    relation_key: str
    qualifiers_json: str | None = None


class EntityDetail(BaseModel):
    id: str
    type_key: str
    created_at: str
    updated_at: str
    values: dict[str, Any]
    identifiers: list[dict[str, Any]]
    relations: list[Relation]
    relations_truncated: bool
    claims: list[dict[str, Any]] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str
    type_key: str
    name: str
    canonical_url: str | None = None


class GraphData(BaseModel):
    nodes: list[GraphNode]
    edges: list[Relation]
    truncated: bool


class ImpactNode(GraphNode):
    depth: int


class ImpactData(BaseModel):
    root: str
    nodes: list[ImpactNode]
    edges: list[Relation]
    truncated: bool
    max_depth: int


class Overview(BaseModel):
    entities: int
    relations: int
    sources: int
    active_jobs: int
    reviews: int
    by_type: dict[str, int]


class JobAccepted(BaseModel):
    id: str
    state: str


class JobSummary(BaseModel):
    id: str
    mode: str
    state: str
    created_at: str
    updated_at: str


class JobDetail(BaseModel):
    id: str
    state: str
    mode: str
    targets: list[dict[str, Any]]
    events: list[dict[str, Any]] = Field(default_factory=list)


def create_app(vault_path: str | Path, *, transport: Transport | None = None, run_worker: bool = True) -> FastAPI:
    path = Path(vault_path).expanduser().resolve()
    runner = JobRunner(path, transport=transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initial = SQLiteStore(path, create=True)
        initial.close()
        if run_worker:
            runner.start()
        yield
        if run_worker:
            await asyncio.to_thread(runner.stop)

    app = FastAPI(title="Devos local vault", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlsplit(origin).netloc != request.headers.get("host")):
            return JSONResponse({"error": {"code": "origin_denied", "message": "Use the local Devos browser tab."}}, status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if request.headers.get("x-devos-request") != "1" or not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"error": {"code": "invalid_request", "message": "JSON and X-Devos-Request headers are required."}}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    return JSONResponse({"error": {"code": "too_large", "message": "Request exceeds 1 MB."}}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DevosError)
    async def domain_error(request: Request, exc: DevosError):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=404 if exc.code == "not_found" else 400)

    def with_vault(operation: Callable[[VaultService], Any]) -> Any:
        store = SQLiteStore(path)
        try:
            return operation(VaultService(store))
        finally:
            store.close()

    @app.get("/api/overview", response_model=Overview, operation_id="overview")
    def overview():
        return with_vault(lambda service: service.overview())

    @app.get("/api/types", operation_id="types")
    def types() -> dict[str, list[str]]:
        return {"entities": list(ENTITY_TYPES), "relations": list(RELATIONS), "adapters": ["github", "arxiv", "huggingface"]}

    @app.get("/api/entities", response_model=EntityPage, operation_id="entities")
    def entities(q: str = Query(default="", max_length=500), type: str | None = None,
                 limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        return with_vault(lambda service: service.browse(query=q, entity_type=type, limit=limit, offset=offset))

    @app.get("/api/entities/{entity_id}", response_model=EntityDetail, operation_id="entity")
    def entity(entity_id: str, provenance: bool = True):
        return with_vault(lambda service: service.show(entity_id, provenance=provenance))

    @app.post("/api/entities", response_model=EntityDetail, status_code=201, operation_id="add_entity")
    def add_entity(body: EntityInput):
        return with_vault(lambda service: service.add(body.type, body.name, description=body.description, url=body.url, body=body.body))

    @app.patch("/api/entities/{entity_id}", response_model=EntityDetail, operation_id="edit_entity")
    def edit_entity(entity_id: str, body: EntityEdit):
        return with_vault(lambda service: service.edit(entity_id, body.model_dump(exclude_unset=True)))

    @app.post("/api/links", response_model=Relation, status_code=201, operation_id="link")
    def link(body: LinkInput):
        return with_vault(lambda service: service.link(body.subject, body.relation, body.object, body.qualifiers))

    @app.get("/api/graph", response_model=GraphData, operation_id="graph")
    def graph(root: str | None = None, limit: int = Query(default=80, ge=1, le=200)):
        return with_vault(lambda service: service.graph(root=root, limit=limit))

    @app.get("/api/impact/{entity_id}", response_model=ImpactData, operation_id="impact")
    def impact(entity_id: str, depth: int = Query(default=3, ge=1, le=6),
               limit: int = Query(default=100, ge=1, le=200)):
        return with_vault(lambda service: service.impact(entity_id, depth=depth, limit=limit))

    @app.get("/api/jobs", response_model=list[JobSummary], operation_id="jobs")
    def jobs():
        return with_vault(lambda service: service.jobs())

    @app.post("/api/jobs", response_model=JobAccepted, status_code=202, operation_id="ingest")
    def ingest(body: IngestInput):
        store = SQLiteStore(path)
        try:
            job_id = IngestService(store, transport=transport).enqueue(**body.model_dump())
        finally:
            store.close()
        runner.wake()
        return {"id": job_id, "state": "pending"}

    @app.get("/api/jobs/{job_id}", response_model=JobDetail, operation_id="job")
    def job(job_id: str):
        return with_vault(lambda service: service.job(job_id))

    @app.post("/api/jobs/{job_id}/resume", response_model=JobDetail, operation_id="resume_job")
    def resume_job(job_id: str):
        result = with_vault(lambda service: service.job(job_id))
        runner.wake()
        return result

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobDetail, operation_id="cancel_job")
    def cancel_job(job_id: str):
        return with_vault(lambda service: service.cancel_job(job_id))

    @app.get("/api/jobs/{job_id}/events", operation_id="job_events")
    async def events(job_id: str, request: Request, after: int = Query(default=0, ge=0)):
        with_vault(lambda service: service.job(job_id))
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = after

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                current = with_vault(lambda service: service.job(job_id))
                for event in current["events"]:
                    if event["sequence"] > cursor:
                        cursor = event["sequence"]
                        yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event)}\n\n"
                if current["state"] in {"complete", "partial", "failed", "canceled"}:
                    yield f"event: done\ndata: {json.dumps({'state': current['state']})}\n\n"
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/export", operation_id="export_vault")
    def export_vault(include_raw: bool = False):
        return JSONResponse(with_vault(lambda service: service.export(include_raw=include_raw)),
                            headers={"Content-Disposition": 'attachment; filename="devos-vault.json"'})

    @app.get("/api/reviews", operation_id="reviews")
    def reviews() -> list[dict[str, Any]]:
        return with_vault(lambda service: service.reviews())

    static = Path(str(files("devos").joinpath("web_dist")))
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        if not (static / "index.html").is_file():
            return JSONResponse({"error": "Frontend assets are missing. Run npm ci and npm run build in web/."}, status_code=503)
        return FileResponse(static / "index.html")

    return app
