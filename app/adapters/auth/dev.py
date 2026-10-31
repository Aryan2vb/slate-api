"""Development sign-in bypass: the bearer token "dev" is a fixed dev user; anything else goes to the real verifier."""

from uuid import UUID

from app.domain.models import AuthUser
from app.ports.providers import AuthVerifier

DEV_TOKEN = "dev"


class DevAuthVerifier:
    def __init__(self, inner: AuthVerifier, user_id: UUID) -> None:
        self._inner = inner
        self._dev = AuthUser(id=user_id, email="dev@slate.local")

    async def verify(self, token: str) -> AuthUser:
        return self._dev if token == DEV_TOKEN else await self._inner.verify(token)
