"""Supabase Auth access-token verification against the project's JWKS (asymmetric signing keys)."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import httpx
import jwt
import structlog
from jwt import PyJWK

from app.config import Settings
from app.domain.errors import AuthenticationError
from app.domain.models import AuthUser

log = structlog.get_logger()

_ALGORITHMS = ["ES256", "RS256"]


class SupabaseJwtVerifier:
    """Implements AuthVerifier. Keys are cached for `cache_ttl_s` and refetched when a token names an unknown kid."""

    def __init__(
        self,
        *,
        supabase_url: str,
        audience: str,
        client: httpx.AsyncClient | None = None,
        cache_ttl_s: float = 600,
        min_refresh_interval_s: float = 30,
    ) -> None:
        base = supabase_url.rstrip("/")
        self._issuer = f"{base}/auth/v1"
        self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
        self._audience = audience
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._ttl = cache_ttl_s
        self._min_refresh = min_refresh_interval_s
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = float("-inf")

    @classmethod
    def from_settings(cls, settings: Settings) -> SupabaseJwtVerifier:
        return cls(supabase_url=settings.supabase_url, audience=settings.supabase_jwt_audience)

    async def verify(self, token: str) -> AuthUser:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthenticationError("Invalid token") from e
        kid = header.get("kid")
        if not isinstance(kid, str) or header.get("alg") not in _ALGORITHMS:
            raise AuthenticationError("Invalid token")

        key = await self._key_for(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
            user_id = UUID(str(claims["sub"]))
        except (jwt.PyJWTError, ValueError) as e:
            log.info("auth.token_rejected", reason=type(e).__name__)
            raise AuthenticationError("Invalid or expired token") from e
        email = claims.get("email")
        return AuthUser(id=user_id, email=email if isinstance(email, str) and email else None)

    async def _key_for(self, kid: str) -> PyJWK:
        age = time.monotonic() - self._fetched_at
        # Unknown kids trigger a refetch, rate-limited so random kids can't make us hammer the JWKS endpoint.
        if age > self._ttl or (kid not in self._keys and age > self._min_refresh):
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise AuthenticationError("Invalid token")
        return key

    async def _refresh(self) -> None:
        try:
            response = await self._client.get(self._jwks_url)
            response.raise_for_status()
            keys: dict[str, PyJWK] = {}
            for jwk in response.json().get("keys", []):
                if isinstance(jwk, dict) and jwk.get("kid"):
                    try:
                        keys[jwk["kid"]] = PyJWK.from_dict(jwk)
                    except jwt.PyJWTError:
                        log.warning("auth.jwks_key_skipped", kid=jwk.get("kid"))
        except (httpx.HTTPError, ValueError) as e:
            log.error("auth.jwks_fetch_failed", error=type(e).__name__)
            # Keep serving cached keys if we have them; an outage must not lock everyone out.
            if not self._keys:
                raise AuthenticationError("Unable to verify token") from e
            return
        self._keys = keys
        self._fetched_at = time.monotonic()
