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
async def test_create_upload_url_returns_absolute_signed_url() -> None:
    route = respx.post(f"{API}/object/upload/sign/meeting-audio/u1/m1/0001.m4a").respond(
        json={"url": "/object/upload/sign/meeting-audio/u1/m1/0001.m4a?token=abc"}
    )
    signed = await _storage().create_upload_url("u1/m1/0001.m4a")
    assert signed.url == f"{API}/object/upload/sign/meeting-audio/u1/m1/0001.m4a?token=abc"
    assert signed.path == "u1/m1/0001.m4a"
    assert signed.expires_in_s == 900
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer srk"
    assert request.headers["apikey"] == "srk"


@respx.mock
async def test_create_upload_url_without_token_is_provider_error() -> None:
    respx.post(f"{API}/object/upload/sign/meeting-audio/p.m4a").respond(json={})
    with pytest.raises(ProviderError):
        await _storage().create_upload_url("p.m4a")


@respx.mock
async def test_download_returns_bytes() -> None:
    respx.get(f"{API}/object/meeting-audio/u1/a.m4a").respond(content=b"audio")
    assert await _storage().download("u1/a.m4a") == b"audio"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(400, json={"error": "not_found", "message": "Object not found"})],
    ids=["404", "400-not-found"],
)
@respx.mock
async def test_download_missing_is_not_found(response: httpx.Response) -> None:
    respx.get(f"{API}/object/meeting-audio/missing.m4a").mock(return_value=response)
    with pytest.raises(NotFoundError):
        await _storage().download("missing.m4a")


@respx.mock
async def test_delete_sends_prefixes() -> None:
    route = respx.delete(f"{API}/object/meeting-audio").respond(json=[])
    await _storage().delete(["a.m4a", "b.m4a"])
    assert json.loads(route.calls.last.request.content) == {"prefixes": ["a.m4a", "b.m4a"]}


@respx.mock
async def test_delete_empty_makes_no_request() -> None:
    route = respx.delete(f"{API}/object/meeting-audio").respond(json=[])
    await _storage().delete([])
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (500, True), (503, True), (401, False), (403, False), (413, False)]
)
@respx.mock
async def test_status_mapping(status: int, retryable: bool) -> None:
    respx.get(f"{API}/object/meeting-audio/x.m4a").respond(status)
    with pytest.raises(ProviderError) as info:
        await _storage().download("x.m4a")
    assert info.value.retryable is retryable


@respx.mock
async def test_timeout_is_retryable() -> None:
    respx.get(f"{API}/object/meeting-audio/x.m4a").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as info:
        await _storage().download("x.m4a")
    assert info.value.retryable is True


@respx.mock
async def test_path_is_url_encoded() -> None:
    route = respx.get(f"{API}/object/meeting-audio/u1/my%20file.m4a").respond(content=b"ok")
    await _storage().download("u1/my file.m4a")
    assert route.called
