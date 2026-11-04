"""Supabase Storage over its REST API, authenticated with the service-role key (server-side only)."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

import httpx

from app.config import Settings
from app.domain.errors import NotFoundError, ProviderError
from app.ports.providers import SignedUpload

_PROVIDER = "supabase-storage"
_LIST_PAGE = 1000  # objects per list request
_DELETE_BATCH = 1000  # paths per bulk delete request


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

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        await self._request(
            "POST",
            f"/object/{self._bucket}/{_quote(path)}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )

    async def create_download_url(self, path: str, ttl_s: int) -> str:
        response = await self._request(
            "POST", f"/object/sign/{self._bucket}/{_quote(path)}", json={"expiresIn": ttl_s}, missing=path
        )
        relative = response.json().get("signedURL")
        if not isinstance(relative, str) or "token=" not in relative:
            raise ProviderError(_PROVIDER, "signed download response had no url", retryable=True)
        return f"{self._base}{relative}"

    async def delete(self, paths: Sequence[str]) -> None:
        if paths:
            await self._request("DELETE", f"/object/{self._bucket}", json={"prefixes": list(paths)})

    async def delete_prefix(self, prefix: str) -> int:
        folder = prefix.strip("/")
        if not folder:  # never sweep the whole bucket by accident
            raise ValueError("delete_prefix needs a non-empty prefix")
        paths = await self._list_recursive(folder)
        for start in range(0, len(paths), _DELETE_BATCH):
            await self.delete(paths[start : start + _DELETE_BATCH])
        return len(paths)

    async def _list_recursive(self, folder: str) -> list[str]:
        """Every object path under `folder`. Storage lists one level at a time; sub-folders come back with id null."""
        found: list[str] = []
        offset = 0
        while True:
            response = await self._request(
                "POST",
                f"/object/list/{self._bucket}",
                json={"prefix": folder, "limit": _LIST_PAGE, "offset": offset},
            )
            entries = response.json()
            if not isinstance(entries, list):
                raise ProviderError(_PROVIDER, "list response was not a list", retryable=True)
            for entry in entries:
                name = entry.get("name") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name:
                    continue
                path = f"{folder}/{name}" if folder else name
                if entry.get("id") is None:
                    found += await self._list_recursive(path)
                else:
                    found.append(path)
            if len(entries) < _LIST_PAGE:
                return found
            offset += _LIST_PAGE

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: object = None,
        content: bytes | None = None,
        missing: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, self._base + url, headers={**self._headers, **(headers or {})}, json=json, content=content
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
