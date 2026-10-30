import uuid

from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def _start(c, user, meeting_id, seq=0, mime="audio/mp4", size=1024):
    return c.post(
        f"/meetings/{meeting_id}/chunks", json={"seq": seq, "mime_type": mime, "byte_size": size}, headers=auth(user)
    )


def test_chunk_upload_flow_and_finish() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        r = _start(c, ALICE, m["id"])
        assert r.status_code == 201
        body = r.json()
        assert body["upload"]["path"].startswith(f"{ALICE}/{m['id']}/00000-")
        assert c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()["meeting"]["status"] == "recording"

        up = c.post(f"/meetings/{m['id']}/chunks/{body['chunk_id']}/uploaded", headers=auth(ALICE))
        assert up.status_code == 200 and up.json()["status"] == "uploaded"

        fin = c.post(f"/meetings/{m['id']}/finish", headers=auth(ALICE))
        assert fin.status_code == 202
        jobs = fin.json()["jobs"]
        assert [(j["kind"], j["status"]) for j in jobs] == [("transcribe", "queued")]
        detail = c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()
        assert detail["meeting"]["status"] == "processing" and detail["stage"] == "transcribing"


def test_finish_twice_does_not_duplicate_jobs() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        chunk = _start(c, ALICE, m["id"]).json()
        c.post(f"/meetings/{m['id']}/chunks/{chunk['chunk_id']}/uploaded", headers=auth(ALICE))
        first = c.post(f"/meetings/{m['id']}/finish", headers=auth(ALICE)).json()["jobs"]
        second = c.post(f"/meetings/{m['id']}/finish", headers=auth(ALICE)).json()["jobs"]
        assert len(second) == 1 and second[0]["id"] == first[0]["id"]


def test_finish_without_uploaded_chunk_is_409() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        assert c.post(f"/meetings/{m['id']}/finish", headers=auth(ALICE)).status_code == 409
        _start(c, ALICE, m["id"])  # registered but never uploaded
        assert c.post(f"/meetings/{m['id']}/finish", headers=auth(ALICE)).status_code == 409


def test_rejects_bad_mime_size_and_duplicate_seq() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        assert _start(c, ALICE, m["id"], mime="video/mp4").status_code == 422
        assert _start(c, ALICE, m["id"], size=51 * 1024 * 1024).status_code == 422
        assert _start(c, ALICE, m["id"], mime="audio/mp4; codecs=mp4a").status_code == 201
        assert _start(c, ALICE, m["id"]).status_code == 409


def test_no_new_chunks_after_finish() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        chunk = _start(c, ALICE, m["id"]).json()
        c.post(f"/meetings/{m['id']}/chunks/{chunk['chunk_id']}/uploaded", headers=auth(ALICE))
        c.post(f"/meetings/{m['id']}/finish", headers=auth(ALICE))
        assert _start(c, ALICE, m["id"], seq=1).status_code == 409


def test_cross_user_and_unknown_chunk_are_404() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        assert _start(c, BOB, m["id"]).status_code == 404
        chunk = _start(c, ALICE, m["id"]).json()
        assert c.post(f"/meetings/{m['id']}/chunks/{chunk['chunk_id']}/uploaded", headers=auth(BOB)).status_code == 404
        assert c.post(f"/meetings/{m['id']}/chunks/{uuid.uuid4()}/uploaded", headers=auth(ALICE)).status_code == 404
        assert c.post(f"/meetings/{m['id']}/finish", headers=auth(BOB)).status_code == 404
