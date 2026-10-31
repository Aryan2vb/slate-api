"""Per-request logging: one `http.request` line per request, with a request id bound for every line inside it."""

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger("slate.http")

REQUEST_ID_HEADER = b"x-request-id"
# Polled constantly by the platform and the apps; logged at debug so they do not drown real traffic.
_QUIET_PATHS = frozenset({"/health"})


class RequestLogMiddleware:
    """Pure ASGI middleware (works with streaming responses and does not buffer bodies)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER, b"").decode("latin-1")
        request_id = incoming if 0 < len(incoming) <= 64 and incoming.isprintable() else uuid.uuid4().hex[:16]
        path: str = scope["path"]
        client = scope.get("client")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, method=scope["method"], path=path)

        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (REQUEST_ID_HEADER, request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("http.unhandled_error")
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            fields = {"status": status, "duration_ms": duration_ms, "client": client[0] if client else None}
            if path in _QUIET_PATHS and status < 400:
                log.debug("http.request", **fields)
            elif status >= 500:
                log.error("http.request", **fields)
            elif status >= 400:
                log.warning("http.request", **fields)
            else:
                log.info("http.request", **fields)
            structlog.contextvars.clear_contextvars()
