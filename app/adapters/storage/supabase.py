"""Supabase Storage over its REST API, authenticated with the service-role key (server-side only)."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

import httpx

from app.config import Settings
from app.domain.errors import NotFoundError, ProviderError
from app.ports.providers import SignedUpload

_PROVIDER = "supabase-storage"


class SupabaseStorage:
    """Implements ObjectStorage for one bucket."""

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        bucket: str,
        upload_url_ttl_s: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = f"{supabase_url.rstrip('/')}/storage/v1"
        self._bucket = bucket
        self._ttl = upload_url_ttl_s
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0))
        self._headers = {"Authorization": f"Bearer {service_role_key}", "apikey": service_role_key}

    @classmethod
    def from_settings(cls, settings: Settings) -> SupabaseStorage:
        return cls(
            supabase_url=settings.supabase_url,
            service_role_key=settings.supabase_service_role_key.get_secret_value(),
            bucket=settings.storage_bucket,
            upload_url_ttl_s=settings.upload_url_ttl_s,
        )

    async def create_upload_url(self, path: str, *, overwrite: bool = False) -> SignedUpload:
        extra = {"x-upsert": "true"} if overwrite else None
        response = await self._request("POST", f"/object/upload/sign/{self._bucket}/{_quote(path)}", headers=extra)
        relative = response.json().get("url")
        if not isinstance(relative, str) or "token=" not in relative:
            raise ProviderError(_PROVIDER, "signed upload response had no url", retryable=True)
        return SignedUpload(url=f"{self._base}{relative}", path=path, expires_in_s=self._ttl)

    async def download(self, path: str) -> bytes:
        response = await self._request("GET", f"/object/{self._bucket}/{_quote(path)}", missing=path)
        return response.content

    async def delete(self, paths: Sequence[str]) -> None:
        if paths:
            await self._request("DELETE", f"/object/{self._bucket}", json={"prefixes": list(paths)})

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: object = None,
        missing: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, self._base + url, headers={**self._headers, **(headers or {})}, json=json
            )
        except httpx.TimeoutException as e:
            raise ProviderError(_PROVIDER, f"{method} timed out", retryable=True) from e
        except httpx.HTTPError as e:
            raise ProviderError(_PROVIDER, f"{method} failed: {type(e).__name__}", retryable=True) from e
        if response.is_success:
            return response
        status = response.status_code
        # Storage reports missing objects as 404, or as 400 with an inner "not_found" error.
        if missing is not None and (status == 404 or (status == 400 and "not_found" in response.text.lower())):
            raise NotFoundError(missing)
        retryable = status == 429 or status >= 500
        raise ProviderError(_PROVIDER, f"{method} returned {status}", retryable=retryable)


def _quote(path: str) -> str:
    return quote(path, safe="/")
