import uuid

from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def test_create_and_get_meeting_detail() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE, template="one_on_one", attendees=[{"email": "p@x.com", "name": "Priya"}])
        assert m["status"] == "draft" and m["template"] == "one_on_one"
        r = c.get(f"/meetings/{m['id']}", headers=auth(ALICE))
        assert r.status_code == 200
        body = r.json()
        assert body["stage"] == "draft"
        assert body["meeting"]["attendees"] == [{"email": "p@x.com", "name": "Priya"}]
        assert body["notes"] == [] and body["transcript"] == [] and body["enhanced"] is None


def test_missing_and_invalid_token_are_401() -> None:
    with client() as (c, _):
        assert c.get("/meetings").status_code == 401
        r = c.get("/meetings", headers={"Authorization": "Bearer nonsense"})
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"


def test_other_users_meeting_is_404() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        assert c.get(f"/meetings/{m['id']}", headers=auth(BOB)).status_code == 404
        assert c.delete(f"/meetings/{m['id']}", headers=auth(BOB)).status_code == 404


def test_list_is_scoped_and_paginated() -> None:
    with client() as (c, _):
        for i in range(3):
            create_meeting(c, ALICE, title=f"m{i}")
        create_meeting(c, BOB)
        page = c.get("/meetings?limit=2", headers=auth(ALICE)).json()
        assert len(page["items"]) == 2 and page["next_before"] is not None
        rest = c.get("/meetings", params={"limit": 2, "before": page["next_before"]}, headers=auth(ALICE)).json()
        titles = {m["title"] for m in page["items"] + rest["items"]}
        assert titles == {"m0", "m1", "m2"}
        assert rest["next_before"] is None


def test_delete_removes_meeting_and_audio() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE)
        chunk = c.post(f"/meetings/{m['id']}/chunks", json={"seq": 0, "mime_type": "audio/mp4"}, headers=auth(ALICE))
        path = chunk.json()["upload"]["path"]
        assert c.delete(f"/meetings/{m['id']}", headers=auth(ALICE)).status_code == 204
        assert c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).status_code == 404
        assert path in container.storage.deleted  # type: ignore[attr-defined]


def test_create_validates_title() -> None:
    with client() as (c, _):
        assert c.post("/meetings", json={"title": ""}, headers=auth(ALICE)).status_code == 422
