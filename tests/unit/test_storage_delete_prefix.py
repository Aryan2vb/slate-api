import json

import httpx
import pytest
import respx

from app.adapters.storage import supabase
from app.adapters.storage.supabase import SupabaseStorage

BASE = "https://proj.supabase.co"
API = f"{BASE}/storage/v1"


def _storage() -> SupabaseStorage:
    return SupabaseStorage(supabase_url=BASE, service_role_key="srk", bucket="meeting-audio", upload_url_ttl_s=900)


def _listing(request: httpx.Request, tree: dict[str, list[dict]]) -> httpx.Response:
    body = json.loads(request.content)
    entries = tree.get(body["prefix"], [])
    return httpx.Response(200, json=entries[body["offset"] : body["offset"] + body["limit"]])


@respx.mock
async def test_delete_prefix_walks_folders_and_deletes_every_object() -> None:
    tree = {
        "u1": [{"name": "m1", "id": None}, {"name": "m2", "id": None}],
        "u1/m1": [{"name": "00000-a.m4a", "id": "1"}, {"name": "full.m4a", "id": "2"}],
        "u1/m2": [{"name": "00000-b.m4a", "id": "3"}],
    }
    lister = respx.post(f"{API}/object/list/meeting-audio").mock(side_effect=lambda r: _listing(r, tree))
    deleter = respx.delete(f"{API}/object/meeting-audio").respond(json=[])

    assert await _storage().delete_prefix("u1/") == 3

    assert {json.loads(c.request.content)["prefix"] for c in lister.calls} == {"u1", "u1/m1", "u1/m2"}
    assert lister.calls[0].request.headers["authorization"] == "Bearer srk"
    assert json.loads(deleter.calls.last.request.content) == {
        "prefixes": ["u1/m1/00000-a.m4a", "u1/m1/full.m4a", "u1/m2/00000-b.m4a"]
    }


@respx.mock
async def test_delete_prefix_pages_through_listings_and_batches_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supabase, "_LIST_PAGE", 2)
    monkeypatch.setattr(supabase, "_DELETE_BATCH", 2)
    tree = {"u1": [{"name": f"{i}.m4a", "id": str(i)} for i in range(5)]}
    lister = respx.post(f"{API}/object/list/meeting-audio").mock(side_effect=lambda r: _listing(r, tree))
    deleter = respx.delete(f"{API}/object/meeting-audio").respond(json=[])

    assert await _storage().delete_prefix("u1") == 5

    assert [json.loads(c.request.content)["offset"] for c in lister.calls] == [0, 2, 4]
    assert [len(json.loads(c.request.content)["prefixes"]) for c in deleter.calls] == [2, 2, 1]


@respx.mock
async def test_delete_prefix_with_nothing_stored_makes_no_delete() -> None:
    respx.post(f"{API}/object/list/meeting-audio").respond(json=[])
    deleter = respx.delete(f"{API}/object/meeting-audio").respond(json=[])
    assert await _storage().delete_prefix("u1/") == 0
    assert deleter.call_count == 0


async def test_delete_prefix_refuses_the_whole_bucket() -> None:
    with pytest.raises(ValueError):
        await _storage().delete_prefix("/")
