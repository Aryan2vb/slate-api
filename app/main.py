from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes import health
from app.config import get_settings
from app.container import Container, build_container
from app.logging import configure_logging


def create_app(container: Container | None = None) -> FastAPI:
    """App factory. Pass a prebuilt Container in tests; production builds one from Settings."""
    settings = container.settings if container else get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        # Workers and other background tasks are started here (see app/jobs/runner.py once it exists).
        yield

    app = FastAPI(title="Slate API", version="0.1.0", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
        )
    install_error_handlers(app)
    app.include_router(health.router)
    return app
