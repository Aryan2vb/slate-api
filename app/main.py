from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes import health, meetings, notes, processing, uploads
from app.config import get_settings
from app.container import Container, build_container
from app.jobs.runner import start_worker, stop_worker
from app.logging import configure_logging


def create_app(container: Container | None = None) -> FastAPI:
    """App factory. Pass a prebuilt Container in tests; production builds one from Settings."""
    settings = container.settings if container else get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        # Free-tier hosting runs the job worker inside the API process; production can run `python -m app.worker`.
        worker = start_worker(app.state.container) if settings.run_worker_in_process else None
        try:
            yield
        finally:
            if worker is not None:
                await stop_worker(worker)

    # Interactive docs and the schema stay off in prod so the API surface is not browsable by anyone.
    public_docs = settings.env != "prod"
    app = FastAPI(
        title="Slate API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if public_docs else None,
        redoc_url="/redoc" if public_docs else None,
        openapi_url="/openapi.json" if public_docs else None,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
        )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(meetings.router)
    app.include_router(notes.router)
    app.include_router(uploads.router)
    app.include_router(processing.router)
    return app
