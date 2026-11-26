"""StateWeaver HTTP, media, events and scoped production tools."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import HTTPConnection

from stateweaver.persistence.database import Database, upgrade_schema
from stateweaver.providers.base import ProviderContractError, ProviderExternalError
from stateweaver.providers.coordination import ProviderCoordinator
from stateweaver.providers.registry import APIRegistry, APIService
from stateweaver.settings import Settings
from stateweaver.storage.cas import ContentAddressedStore
from stateweaver.studio.browser_media import BrowserMedia
from stateweaver.studio.contracts import (
    BodyChunk,
    CandidateComparison,
    ClipPreparationUpdate,
    ClipReadiness,
    CommandResult,
    CreateProject,
    EntityLabelBatch,
    EntityPage,
    EventBatch,
    ExternalCandidateReviewRequest,
    ExternalOperatorTakeoverRequest,
    ExternalReviewRequest,
    ProductionStatus,
    ProjectList,
    SetDependencyReport,
    SetPreview,
    StudioProject,
    StudioSnapshot,
    TranscriptPage,
    UserAction,
    WorkspaceLayout,
    WorkspaceLayoutUpdate,
)
from stateweaver.studio.mcp import CanvasMCP, token_secret
from stateweaver.studio.queries import StudioQueries
from stateweaver.studio.service import StudioService
from stateweaver.studio.set_preview import SetPreviewCache
from stateweaver.studio.settings import StudioSettings
from stateweaver.studio.store import StudioError, StudioStore
from stateweaver.studio.workspace import WorkspaceStore


def external_operator_token(
    secret: bytes, project_id: str, reviewer_id: str, authorization_id: str
) -> str:
    """Issue a domain-separated credential for an external human operator.

    This intentionally cannot be used as an actor MCP credential: the message
    includes a fixed credential kind and has no actor lease generation.
    """

    message = json.dumps(
        ["external-operator", 1, project_id, reviewer_id, authorization_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def create_app(
    settings: StudioSettings | None = None,
    *,
    enable_runtime: bool = True,
    service: StudioService | None = None,
    api_registry: APIRegistry | None = None,
    api_coordinator: ProviderCoordinator | None = None,
    coordinator_owner_path: Path | None = None,
    coordinator_token_file: Path | None = None,
    coordinator_recovery_path: Path | None = None,
    coordinator_admission_paused: bool = False,
) -> FastAPI:
    config = settings or StudioSettings()
    from .v2_storage import (
        V2DatabaseUnavailable,
        ensure_v2_storage,
        guard_active_v2_database,
        prepare_v2_runtime,
        v2_storage_identity,
    )

    if service is not None and service.store.database.url != config.database_url:
        raise ValueError("Studio service and configuration database bindings differ")
    if (
        enable_runtime
        and api_coordinator is None
        and not (
            "provider_ledger_path" in config.model_fields_set or coordinator_owner_path is not None
        )
    ):
        raise ValueError("Specify the provider coordinator binding for StateWeaver")
    if coordinator_owner_path is not None and api_coordinator is not None:
        raise ProviderContractError("Private coordinator owner cannot use an injected client")
    ledger_path = coordinator_owner_path or (
        api_coordinator.path if api_coordinator else config.provider_ledger_path
    )
    storage = (
        config.model_copy(update={"database_url": service.store.database.url})
        if service
        else config
    )
    from stateweaver.studio_media.config import load_media_config_with_diagnostics

    if enable_runtime and api_registry is None and config.provider_registry_path is None:
        raise ValueError("Specify a StateWeaver provider registry")
    provider_config, credential_config_error = (
        load_media_config_with_diagnostics()
        if enable_runtime or api_registry is None
        else ({}, None)
    )
    registry = api_registry or APIRegistry.load(provider_config, config.provider_registry_path)
    video_binding: APIService | None = None
    required_credential_bindings: tuple[APIService, ...] = ()
    if enable_runtime:
        from .v2_h3 import require_h3_shared_cas

        text_binding = registry.fixed_service("text", required=True)
        image_binding = registry.fixed_media("image", required=True)
        video_binding = registry.fixed_media("video", required=True)
        assert text_binding is not None and image_binding is not None
        assert video_binding is not None
        required_credential_bindings = (text_binding, image_binding, video_binding)
        require_h3_shared_cas(config, registry)
    ensure_v2_storage(storage, ledger_path=ledger_path)
    studio_storage_id = v2_storage_identity(storage)
    database = service.store.database if service else Database(config.database_url)
    studio = service or StudioService(StudioStore(database))
    canonical_coordinator_path = config.provider_ledger_path.resolve()
    if coordinator_owner_path is not None and coordinator_recovery_path is not None:
        from stateweaver.providers.coordination_storage import CoordinatorBackup

        CoordinatorBackup(coordinator_owner_path, coordinator_recovery_path).restore_if_missing()
    coordinator = (
        ProviderCoordinator(coordinator_owner_path, direct=True)
        if (coordinator_owner_path is not None)
        else (api_coordinator or ProviderCoordinator(config.provider_ledger_path))
    )
    studio.api_registry = registry
    cas = ContentAddressedStore(config.cas_root)
    browser_media = BrowserMedia(
        cas, (config.media_scratch_root or config.work_root) / "browser-media"
    )
    secret = token_secret(config.work_root / "mcp.secret")
    canvas = CanvasMCP(studio, cas, compact_role_reads=True)
    queries = StudioQueries(studio.store)
    workspace = WorkspaceStore(studio.store)
    previews = SetPreviewCache(
        studio.store,
        browser_media.files,
        (config.media_scratch_root or config.work_root) / "set-previews",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database_guard = None
        try:
            await asyncio.to_thread(prepare_v2_runtime, storage, coordinator)
            database_guard = await asyncio.to_thread(guard_active_v2_database, storage, database)
            app.state.v2_database_guard = database_guard
            proxy = getattr(app.state, "native_proxy", None)
            if proxy is not None:
                await proxy.run_io(proxy.receipts.recover)
            if enable_runtime:
                upgrade_schema(database)
                from stateweaver.studio.supervisor import ActorSupervisor

                supervisor = ActorSupervisor(
                    studio,
                    cas,
                    config,
                    secret,
                    api_registry=registry,
                    api_coordinator=coordinator,
                    provider_config=provider_config,
                )
                from .role_runner import StudioRoleRunner

                assert proxy is not None
                supervisor.role_runner = StudioRoleRunner(supervisor, canvas, proxy).run
                app.state.supervisor = supervisor
                await supervisor.start()
                try:
                    yield
                finally:
                    await supervisor.close()
            else:
                yield
        finally:
            try:
                proxy = getattr(app.state, "native_proxy", None)
                if proxy is not None:
                    await proxy.aclose()
            finally:
                try:
                    await asyncio.to_thread(previews.close)
                finally:
                    if database_guard is not None:
                        database_guard.close()
                        app.state.v2_database_guard = None

    app = FastAPI(title="StateWeaver Studio", version="2.0.0", lifespan=lifespan)
    app.state.studio = studio
    app.state.cas = cas
    app.state.set_previews = previews
    app.state.api_registry, app.state.api_coordinator = registry, coordinator
    if enable_runtime:
        from .native_provider import NativeProviderProxy

        app.state.native_proxy = NativeProviderProxy(studio, registry, coordinator, config)
    from .production_status import ProductionMonitor

    def runtime_routes() -> tuple[dict[str, Any], dict[str, int]]:
        supervisor = getattr(app.state, "supervisor", None)
        if supervisor is None:
            return {}, {}
        routes: dict[str, Any] = (
            getattr(supervisor.executor, "video_capacity_routes", None)
            or getattr(supervisor.executor, "video_routes", lambda: {})()
        )
        return routes, dict(supervisor.video_capacities)

    monitor = ProductionMonitor(studio.store, config, runtime_routes)
    from .runtime_metrics import HTTPMetrics

    app.add_middleware(HTTPMetrics, metrics=studio.store.metrics)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=Settings().allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(StudioError)
    async def studio_error(_: Request, exc: StudioError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(V2DatabaseUnavailable)
    async def v2_database_unavailable(_: Request, exc: V2DatabaseUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        if database_guard := getattr(app.state, "v2_database_guard", None):
            database_guard.verify()
        supervisor = getattr(app.state, "supervisor", None)
        loop_error = getattr(supervisor, "last_loop_error", None)
        readiness = getattr(supervisor, "video_readiness", None)
        missing_credentials = []
        for binding in required_credential_bindings:
            try:
                registry.credential(binding)
            except ProviderExternalError:
                missing_credentials.append(binding.kind)
        degraded = (
            loop_error
            or (readiness is not None and not readiness["default_ready"])
            or (enable_runtime and (missing_credentials or credential_config_error))
        )
        return {
            "status": "degraded" if degraded else "ok",
            "workflow": "agentvideo-v2",
            "workspace_schema": "stateweaver-release-v1",
            "version": "2.0.0",
            "provider_credentials": (
                {"missing": missing_credentials, "config_error": credential_config_error}
                if enable_runtime
                else None
            ),
            "video_workers_ready": getattr(supervisor, "video_capacity", None),
            "video_readiness": readiness,
            "video_protocol": video_binding.protocol if video_binding else None,
            "video_capacity_source": (
                "configured"
                if video_binding and video_binding.video_http
                else "advertised"
                if video_binding
                else None
            ),
            "video_capacity_error": getattr(supervisor, "capacity_error", None),
            "scheduler_error": loop_error,
            "scheduler_last_success_at": getattr(supervisor, "last_loop_success_at", None),
            "video_route_capacities": getattr(supervisor, "video_capacities", None),
            "draining": getattr(supervisor, "stopping", False),
            "draining_tasks": getattr(supervisor, "shutdown_pending", 0),
            "studio_contract": "set-native-v2",
            **({"studio_storage_id": studio_storage_id} if studio_storage_id else {}),
        }

    def projects(
        after: str | None = None, limit: int = Query(default=50, ge=1, le=200)
    ) -> ProjectList:
        return studio.store.projects(after, limit)

    def create(
        request: CreateProject,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ] = None,
    ) -> StudioSnapshot:
        if idempotency_key is not None:
            if request.idempotency_key not in {None, idempotency_key}:
                raise StudioError("请求中提供了不同的创建幂等键", 422)
            request = request.model_copy(update={"idempotency_key": idempotency_key})
        return queries.snapshot(studio.create(request).id)

    def delete_project(project_id: str, request: UserAction) -> CommandResult:
        # Retried DELETEs must reach their durable receipt even after tombstoning.
        return studio.store.command_result(studio.delete_project(project_id, request))

    def visible_project(project_id: str) -> None:
        studio.store.require_project(project_id)

    projects_api = APIRouter(
        prefix="/projects/{project_id}", dependencies=[Depends(visible_project)]
    )

    @projects_api.get("", response_model=StudioSnapshot)
    def project(project_id: str) -> StudioSnapshot:
        return queries.snapshot(project_id)

    @projects_api.get("/production-status", response_model=ProductionStatus)
    def production_status(project_id: str, details: bool = False) -> ProductionStatus:
        return monitor.get(project_id, details=details)

    @projects_api.get("/layout", response_model=WorkspaceLayout)
    def layout(project_id: str) -> WorkspaceLayout:
        return workspace.get(project_id)

    @projects_api.get("/sets/{set_id}/dependencies", response_model=SetDependencyReport)
    def set_dependencies(project_id: str, set_id: str) -> SetDependencyReport:
        from .set_handoffs import dependency_report

        def read(p: Any) -> SetDependencyReport:
            if set_id not in p.sets:
                raise StudioError("片场不存在", 404)
            return dependency_report(p, set_id)

        return studio.store.read(project_id, read)

    @projects_api.get(
        "/clips/{clip_id}/readiness",
        response_model=ClipReadiness,
    )
    def readiness(project_id: str, clip_id: str) -> ClipReadiness:
        from .production_reads import clip_readiness

        return studio.store.read(project_id, lambda p: clip_readiness(p, clip_id))

    @projects_api.get(
        "/clips/{clip_id}/comparison",
        response_model=CandidateComparison,
    )
    def comparison(
        project_id: str,
        clip_id: str,
        after: str | None = None,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> CandidateComparison:
        from .production_reads import compare_candidates

        return studio.store.read(
            project_id, lambda p: compare_candidates(p, clip_id, after=after, limit=limit)
        )

    @projects_api.put("/layout", response_model=WorkspaceLayout)
    def save_layout(project_id: str, request: WorkspaceLayoutUpdate) -> WorkspaceLayout:
        return workspace.update(project_id, request)

    @projects_api.patch("/clips/{clip_id}/preparation", response_model=CommandResult)
    def configure_preparation(
        project_id: str, clip_id: str, request: ClipPreparationUpdate
    ) -> CommandResult:
        return studio.store.command_result(
            studio.configure_preparation(project_id, clip_id, request)
        )

    @projects_api.post("/message", response_model=CommandResult)
    def message(project_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(studio.user_action(project_id, "message", request))

    @projects_api.post("/confirm", response_model=CommandResult)
    def confirm(project_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(studio.user_action(project_id, "confirm", request))

    @projects_api.post("/actors/{actor_id}/pause", response_model=CommandResult)
    def pause(project_id: str, actor_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(
            studio.user_action(project_id, "pause", request, actor_id)
        )

    @projects_api.post(
        "/actors/{actor_id}/resume",
        response_model=CommandResult,
    )
    def resume(project_id: str, actor_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(
            studio.user_action(project_id, "resume", request, actor_id)
        )

    @projects_api.post(
        "/actors/{actor_id}/reset-context",
        response_model=CommandResult,
    )
    def reset_context(project_id: str, actor_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(
            studio.user_action(project_id, "reset_context", request, actor_id)
        )

    @projects_api.post("/actors/{actor_id}/recover-image-input", response_model=CommandResult)
    def recover_image_input(project_id: str, actor_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(
            studio.user_action(project_id, "recover_image_input", request, actor_id)
        )

    @projects_api.post("/recover-image-input", response_model=CommandResult)
    def recover_project_image_inputs(project_id: str, request: UserAction) -> CommandResult:
        return studio.store.command_result(
            studio.user_action(project_id, "recover_project_image_inputs", request)
        )

    def authorize_external_operator(
        http_request: Request,
        project_id: str,
        reviewer_id: str,
        authorization_id: str,
    ) -> None:
        """Authenticate the separately-audited external operator surface."""

        header = http_request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "External operator credential required")
        supplied = header[len("Bearer ") :]
        expected = external_operator_token(secret, project_id, reviewer_id, authorization_id)
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "External operator credential required")

    @projects_api.post("/external-reviews", response_model=CommandResult)
    def external_review(
        project_id: str,
        request: ExternalReviewRequest,
        http_request: Request,
    ) -> CommandResult:
        authorize_external_operator(
            http_request, project_id, request.reviewer_id, request.authorization_id
        )
        return studio.store.command_result(studio.submit_external_review(project_id, request))

    @projects_api.post("/external-reviews/candidates/{candidate_id}", response_model=CommandResult)
    def external_candidate_review(
        project_id: str,
        candidate_id: str,
        request: ExternalCandidateReviewRequest,
        http_request: Request,
    ) -> CommandResult:
        authorize_external_operator(
            http_request, project_id, request.reviewer_id, request.authorization_id
        )
        return studio.store.command_result(
            studio.review_candidate_external(project_id, candidate_id, request)
        )

    @projects_api.post("/external-reviews/actors/{actor_id}/takeover", response_model=CommandResult)
    def external_operator_takeover(
        project_id: str,
        actor_id: str,
        request: ExternalOperatorTakeoverRequest,
        http_request: Request,
    ) -> CommandResult:
        authorize_external_operator(
            http_request, project_id, request.reviewer_id, request.authorization_id
        )
        return studio.store.command_result(studio.operator_takeover(project_id, actor_id, request))

    @projects_api.get("/details", response_model=StudioSnapshot)
    def details(project_id: str, scope: str, compact: bool = False) -> StudioSnapshot:
        return queries.snapshot(project_id, scope, compact=compact)

    @projects_api.get("/entity-labels", response_model=EntityLabelBatch)
    def entity_labels(
        project_id: str, ids: Annotated[list[str], Query(min_length=1, max_length=100)]
    ) -> EntityLabelBatch:
        return queries.entity_labels(project_id, ids)

    @projects_api.get(
        "/actors/{actor_id}/transcript",
        response_model=TranscriptPage,
    )
    def transcript(
        project_id: str,
        actor_id: str,
        before: int | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> TranscriptPage:
        return studio.store.transcript_page(project_id, actor_id, before, limit)

    @projects_api.get("/history", response_model=EntityPage)
    def history(
        project_id: str,
        kind: str,
        parent_id: str,
        after: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> EntityPage:
        return queries.history(project_id, kind, parent_id, after, limit)

    @projects_api.get("/memory", response_model=EntityPage)
    def memory_entries(
        project_id: str,
        scope: str = "global",
        status: str = "effective",
        asset_id: str | None = None,
        clip_id: str | None = None,
        event_id: str | None = None,
        query: str = "",
        after: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> EntityPage:
        return queries.memory_page(
            project_id,
            scope=scope,
            status=status,
            asset_id=asset_id,
            clip_id=clip_id,
            event_id=event_id,
            query=query,
            after=after,
            limit=limit,
        )

    @projects_api.get("/bodies/{digest}", response_model=BodyChunk)
    def body(
        project_id: str,
        digest: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=65536, ge=1, le=65536),
    ) -> BodyChunk:
        return studio.store.body(project_id, digest, offset, limit)

    @projects_api.get("/events", response_model=EventBatch)
    def events(project_id: str, after: int = Query(default=0, ge=0)) -> EventBatch:
        return studio.store.events(project_id, after)

    async def websocket(websocket: WebSocket, project_id: str, after: int = 0) -> None:
        try:
            await asyncio.to_thread(studio.store.require_project, project_id)
        except V2DatabaseUnavailable:
            await websocket.close(code=1013)
            return
        except StudioError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        cursor = max(after, 0)
        try:
            while True:
                batch = await asyncio.to_thread(studio.store.events, project_id, cursor)
                for event in batch.events:
                    await websocket.send_json(event.model_dump(mode="json"))
                    if event.kind == "project_deleted":
                        await websocket.close(code=1000)
                        return
                cursor = batch.cursor
                if batch.has_more:
                    continue
                # Receive also detects orderly disconnect when no project events arrive.
                try:
                    incoming = await asyncio.wait_for(websocket.receive(), timeout=0.5)
                    if incoming["type"] == "websocket.disconnect":
                        return
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            return
        except V2DatabaseUnavailable:
            await websocket.close(code=1013)

    @projects_api.get("/sets/{set_id}/preview", response_model=SetPreview)
    def set_preview(
        response: Response,
        project_id: str,
        set_id: str,
        prepare: bool = False,
        retry: bool = False,
    ) -> SetPreview:
        response.headers["Cache-Control"] = "no-store"
        return previews.get(project_id, set_id, prepare=prepare, retry=retry)

    @projects_api.get("/sets/{set_id}/preview/{fingerprint}.mp4")
    def set_preview_media(project_id: str, set_id: str, fingerprint: str) -> FileResponse:
        try:
            item, path = previews.file(project_id, set_id, fingerprint)
        except OSError as error:
            raise HTTPException(
                503,
                "片场连播暂不可读取，请重试预览",
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            ) from error
        return FileResponse(
            path,
            media_type=item.mime_type,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "ETag": f'"{item.sha256}"',
            },
        )

    @projects_api.api_route("/media/{media_id}", methods=["GET", "HEAD"])
    def media(
        request: Request, project_id: str, media_id: str, preview: int | None = None
    ) -> Response:
        item = queries.media(project_id, media_id)
        if preview is not None and (
            preview not in {256, 512, 1024} or not item.mime_type.startswith("image/")
        ):
            raise HTTPException(422, "图片预览尺寸应为 256、512 或 1024")
        try:
            path = (
                browser_media.thumbnail(item.sha256, preview)
                if preview is not None
                else browser_media.original(item.sha256)
            )
        except OSError as error:
            raise HTTPException(
                503,
                "素材暂时无法读取，请重试加载",
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            ) from error
        etag = f'"{item.sha256}{f"-image-v1-{preview}" if preview else ""}"'
        headers = {"Cache-Control": "public, max-age=31536000, immutable", "ETag": etag}
        if any(
            value.strip().removeprefix("W/") in {etag, "*"}
            for value in request.headers.get("if-none-match", "").split(",")
        ):
            return Response(status_code=304, headers=headers)
        return FileResponse(
            path,
            media_type="image/webp" if preview is not None else item.mime_type,
            headers=headers,
            stat_result=path.stat(),
        )

    @projects_api.get("/export")
    def export(project_id: str) -> dict[str, Any]:
        from .assembly import current_final_media_id

        def read(project: StudioProject) -> dict[str, Any]:
            if not project.final_media_id:
                raise HTTPException(409, "成片尚未生成")
            media_id = current_final_media_id(project)
            if not media_id:
                raise HTTPException(409, "当前选片已变化，请重新合成。")
            return {
                "media": project.media[media_id].model_dump(mode="json"),
                "manifest": project.export_manifest,
            }

        return studio.store.read(project_id, read)

    def require_storage(connection: HTTPConnection, binding: str) -> None:
        if binding == studio_storage_id:
            return
        if connection.scope["type"] == "websocket":
            raise WebSocketException(code=1008)
        raise HTTPException(
            412, "StateWeaver 存储已切换，请重新连接。", headers={"Cache-Control": "no-store"}
        )

    bound = APIRouter(dependencies=[Depends(require_storage)])
    bound.add_api_route("/projects", projects, methods=["GET"], response_model=ProjectList)
    bound.add_api_route(
        "/projects", create, methods=["POST"], response_model=StudioSnapshot, status_code=201
    )
    bound.add_api_route(
        "/projects/{project_id}", delete_project, methods=["DELETE"], response_model=CommandResult
    )
    bound.include_router(projects_api)
    bound.add_api_websocket_route("/projects/{project_id}/events/ws", websocket)
    app.include_router(bound, prefix="/api/v1/studio/v2/stores/{binding}")
    if coordinator_owner_path is not None:
        from stateweaver.providers.coordinator_server import install_owner

        if coordinator_token_file is None:
            raise ProviderContractError("Private coordinator owner requires a token file")
        install_owner(
            app,
            coordinator,
            canonical_coordinator_path,
            coordinator_token_file,
            coordinator_recovery_path,
            admission_paused=coordinator_admission_paused,
        )
    return app


def serve(config: StudioSettings) -> None:
    from math import ceil
    from types import FrameType

    import uvicorn

    app = create_app(config)

    class StudioServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            # Uvicorn drains HTTP connections before lifespan shutdown. Stop
            # dispatch at signal receipt, not after that potentially long wait.
            supervisor = getattr(app.state, "supervisor", None)
            if supervisor is not None:
                supervisor.request_shutdown()
            super().handle_exit(sig, frame)

    StudioServer(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            timeout_graceful_shutdown=ceil(config.shutdown_drain_seconds),
        )
    ).run()
