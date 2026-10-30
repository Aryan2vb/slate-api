"""Maps domain errors to HTTP responses in one place, so routes never build error responses by hand."""

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    ProviderError,
)

log = structlog.get_logger()

_STATUS: dict[type[DomainError], int] = {
    NotFoundError: 404,
    ConflictError: 409,
    InvalidInputError: 422,
    AuthenticationError: 401,
    ProviderError: 502,
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status = next((code for cls, code in _STATUS.items() if isinstance(exc, cls)), 400)
        log.info("http.domain_error", path=request.url.path, status=status, error=type(exc).__name__)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse({"error": {"type": type(exc).__name__, "message": str(exc)}}, status, headers=headers)
