"""SupabaseStorage server-side upload and signed download URLs (the recording kept for playback)."""

import json

import httpx
import pytest
import respx

from app.adapters.storage.supabase import SupabaseStorage
from app.domain.errors import NotFoundError, ProviderError

BASE = "https://proj.supabase.co"
API = f"{BASE}/storage/v1"


def _storage() -> SupabaseStorage:
    return SupabaseStorage(supabase_url=BASE, service_role_key="srk", bucket="meeting-audio", upload_url_ttl_s=900)


@respx.mock
async def test_upload_posts_bytes_with_type_and_upsert() -> None:
    route = respx.post(f"{API}/object/meeting-audio/u1/m1/full.m4a").respond(json={"Key": "meeting-audio/u1/m1"})
    await _storage().upload("u1/m1/full.m4a", b"audio", "audio/mp4")
    request = route.calls.last.request
    assert request.content == b"audio"
    assert request.headers["content-type"] == "audio/mp4"
    assert request.headers["x-upsert"] == "true"
    assert request.headers["authorization"] == "Bearer srk"


@respx.mock
async def test_upload_failure_is_provider_error() -> None:
    respx.post(f"{API}/object/meeting-audio/p.m4a").respond(503)
    with pytest.raises(ProviderError) as info:
        await _storage().upload("p.m4a", b"x", "audio/mp4")
    assert info.value.retryable


@respx.mock
async def test_create_download_url_returns_absolute_signed_url() -> None:
    route = respx.post(f"{API}/object/sign/meeting-audio/u1/m1/full.m4a").respond(
        json={"signedURL": "/object/sign/meeting-audio/u1/m1/full.m4a?token=abc"}
    )
    url = await _storage().create_download_url("u1/m1/full.m4a", 3600)
    assert url == f"{API}/object/sign/meeting-audio/u1/m1/full.m4a?token=abc"
    assert json.loads(route.calls.last.request.content) == {"expiresIn": 3600}


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(400, json={"error": "not_found", "message": "Object not found"})],
    ids=["404", "400-not-found"],
)
@respx.mock
async def test_create_download_url_for_missing_object_is_not_found(response: httpx.Response) -> None:
    respx.post(f"{API}/object/sign/meeting-audio/gone.m4a").mock(return_value=response)
    with pytest.raises(NotFoundError):
        await _storage().create_download_url("gone.m4a", 3600)


@respx.mock
async def test_create_download_url_without_url_is_provider_error() -> None:
    respx.post(f"{API}/object/sign/meeting-audio/p.m4a").respond(json={})
    with pytest.raises(ProviderError):
        await _storage().create_download_url("p.m4a", 60)
