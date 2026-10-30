"""FastAPI dependencies. Routes get collaborators from here, never by importing adapters (Dependency Inversion)."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.container import Container
from app.domain.errors import AuthenticationError
from app.domain.models import AuthUser

_bearer = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


async def current_user(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token")
    return await container.auth.verify(credentials.credentials)


CurrentUser = Annotated[AuthUser, Depends(current_user)]

LimitedAction = Literal["processing", "catchup"]


def rate_limit(action: LimitedAction) -> Callable[..., Awaitable[None]]:
    """Route dependency charging one `action` to the caller; limits come from Settings."""

    async def _charge(container: ContainerDep, user: CurrentUser) -> None:
        s = container.settings
        limit = {"processing": s.rate_limit_processing_per_hour, "catchup": s.rate_limit_catchup_per_hour}[action]
        await container.rate_limiter.hit(f"{action}:{user.id}", limit=limit, window_s=3600)

    return _charge
