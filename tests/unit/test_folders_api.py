import uuid

from fastapi.testclient import TestClient

from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def create_folder(c: TestClient, user: uuid.UUID, **body: object) -> dict:
    r = c.post("/folders", json={"name": "Compilers", **body}, headers=auth(user))
    assert r.status_code == 201, r.text
    return r.json()


def put_notes(c: TestClient, user: uuid.UUID, meeting_id: str, *texts: str) -> None:
    lines = [{"id": str(uuid.uuid4()), "position": i, "text": t} for i, t in enumerate(texts)]
    r = c.put(f"/meetings/{meeting_id}/notes", json={"lines": lines}, headers=auth(user))
    assert r.status_code == 200, r.text


def test_create_and_list_folders_with_counts_ordered_by_name() -> None:
    with client() as (c, _):
        compilers = create_folder(c, ALICE, name="  compilers ", icon="code")
        assert compilers["name"] == "compilers" and compilers["icon"] == "code" and compilers["vocabulary"] == []
        create_folder(c, ALICE, name="Algebra")
        create_folder(c, BOB, name="Bob's")
        create_meeting(c, ALICE, folder_id=compilers["id"])
        create_meeting(c, ALICE, folder_id=compilers["id"])
        create_meeting(c, ALICE)

        items = c.get("/folders", headers=auth(ALICE)).json()["items"]
        assert [(f["name"], f["meeting_count"]) for f in items] == [("Algebra", 0), ("compilers", 2)]


def test_create_validates_name_and_icon() -> None:
    with client() as (c, _):
        assert c.post("/folders", json={"name": ""}, headers=auth(ALICE)).status_code == 422
        assert c.post("/folders", json={"name": "   "}, headers=auth(ALICE)).status_code == 422
        assert c.post("/folders", json={"name": "x" * 81}, headers=auth(ALICE)).status_code == 422
        assert c.post("/folders", json={"name": "ok", "icon": "i" * 41}, headers=auth(ALICE)).status_code == 422
        assert c.get("/folders").status_code == 401


def test_patch_is_partial_and_normalises_vocabulary() -> None:
    with client() as (c, _):
        f = create_folder(c, ALICE, icon="code")
        r = c.patch(
            f"/folders/{f['id']}",
            json={"vocabulary": ["  LR(1) ", "lr(1)", "DFA", "", "First   Follow", "dfa"]},
            headers=auth(ALICE),
        )
        assert r.status_code == 200, r.text
        assert r.json()["vocabulary"] == ["LR(1)", "DFA", "First Follow"]
        assert r.json()["name"] == "Compilers" and r.json()["icon"] == "code"  # untouched

        r = c.patch(f"/folders/{f['id']}", json={"name": "Compiler Design", "icon": None}, headers=auth(ALICE))
        body = r.json()
        assert (body["name"], body["icon"], body["vocabulary"]) == (
            "Compiler Design",
            None,
            ["LR(1)", "DFA", "First Follow"],
        )


def test_patch_rejects_bad_input() -> None:
    with client() as (c, _):
        f = create_folder(c, ALICE)
        url = f"/folders/{f['id']}"
        assert c.patch(url, json={"name": None}, headers=auth(ALICE)).status_code == 422
        assert c.patch(url, json={"vocabulary": None}, headers=auth(ALICE)).status_code == 422
        assert c.patch(url, json={"color": "red"}, headers=auth(ALICE)).status_code == 422
        too_many = [f"term{i}" for i in range(101)]
        assert c.patch(url, json={"vocabulary": too_many}, headers=auth(ALICE)).status_code == 422
        assert c.patch(url, json={"vocabulary": ["x" * 101]}, headers=auth(ALICE)).status_code == 422
        # 101 raw terms that dedupe to 100 are fine.
        ok = [f"term{i}" for i in range(100)] + ["TERM0"]
        assert c.patch(url, json={"vocabulary": ok}, headers=auth(ALICE)).status_code == 200


def test_other_users_folder_is_404_everywhere() -> None:
    with client() as (c, _):
        f = create_folder(c, ALICE)
        url = f"/folders/{f['id']}"
        assert c.patch(url, json={"name": "mine now"}, headers=auth(BOB)).status_code == 404
        assert c.delete(url, headers=auth(BOB)).status_code == 404
        assert c.get(f"{url}/meetings", headers=auth(BOB)).status_code == 404
        assert c.get(f"{url}/vocabulary/suggestions", headers=auth(BOB)).status_code == 404
        assert c.get("/meetings", params={"folder_id": f["id"]}, headers=auth(BOB)).status_code == 404
        assert c.get("/folders", headers=auth(BOB)).json()["items"] == []
        assert c.patch(f"/folders/{uuid.uuid4()}", json={"name": "x"}, headers=auth(ALICE)).status_code == 404


def test_delete_unfiles_meetings() -> None:
    with client() as (c, _):
        f = create_folder(c, ALICE)
        m = create_meeting(c, ALICE, folder_id=f["id"])
        assert m["folder_id"] == f["id"]
        assert c.delete(f"/folders/{f['id']}", headers=auth(ALICE)).status_code == 204
        assert c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()["meeting"]["folder_id"] is None
        assert c.get("/folders", headers=auth(ALICE)).json()["items"] == []
        assert c.delete(f"/folders/{f['id']}", headers=auth(ALICE)).status_code == 404


def test_create_meeting_in_folder_requires_own_folder() -> None:
    with client() as (c, _):
        bobs = create_folder(c, BOB)
        r = c.post("/meetings", json={"title": "t", "folder_id": bobs["id"]}, headers=auth(ALICE))
        assert r.status_code == 404
        assert c.get("/meetings", headers=auth(ALICE)).json()["items"] == []


def test_move_meeting_between_folders_and_unfile() -> None:
    with client() as (c, _):
        a, b = create_folder(c, ALICE, name="A"), create_folder(c, ALICE, name="B")
        m = create_meeting(c, ALICE)
        assert m["folder_id"] is None
        url = f"/meetings/{m['id']}/folder"

        r = c.put(url, json={"folder_id": a["id"]}, headers=auth(ALICE))
        assert r.status_code == 200 and r.json()["folder_id"] == a["id"]
        assert c.put(url, json={"folder_id": b["id"]}, headers=auth(ALICE)).json()["folder_id"] == b["id"]
        counts = {f["name"]: f["meeting_count"] for f in c.get("/folders", headers=auth(ALICE)).json()["items"]}
        assert counts == {"A": 0, "B": 1}
        assert c.put(url, json={"folder_id": None}, headers=auth(ALICE)).json()["folder_id"] is None
        assert c.put(url, json={}, headers=auth(ALICE)).status_code == 422  # folder_id is required (null unfiles)


def test_move_checks_ownership_of_meeting_and_folder() -> None:
    with client() as (c, _):
        mine, bobs = create_folder(c, ALICE), create_folder(c, BOB)
        m = create_meeting(c, ALICE)
        url = f"/meetings/{m['id']}/folder"
        assert c.put(url, json={"folder_id": bobs["id"]}, headers=auth(ALICE)).status_code == 404
        assert c.put(url, json={"folder_id": mine["id"]}, headers=auth(BOB)).status_code == 404
        assert c.put(url, json={"folder_id": bobs["id"]}, headers=auth(BOB)).status_code == 404
        assert c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()["meeting"]["folder_id"] is None


def test_folder_meetings_and_list_filters() -> None:
    with client() as (c, _):
        f = create_folder(c, ALICE)
        filed = [create_meeting(c, ALICE, title=f"lecture {i}", folder_id=f["id"]) for i in range(3)]
        loose = create_meeting(c, ALICE, title="loose")

        page = c.get(f"/folders/{f['id']}/meetings", params={"limit": 2}, headers=auth(ALICE)).json()
        assert len(page["items"]) == 2 and page["next_before"] is not None
        rest = c.get(
            f"/folders/{f['id']}/meetings", params={"limit": 2, "before": page["next_before"]}, headers=auth(ALICE)
        ).json()
        assert {m["id"] for m in page["items"] + rest["items"]} == {m["id"] for m in filed}
        assert all(m["folder_id"] == f["id"] for m in page["items"])

        by_query = c.get("/meetings", params={"folder_id": f["id"]}, headers=auth(ALICE)).json()["items"]
        assert {m["id"] for m in by_query} == {m["id"] for m in filed}
        unfiled = c.get("/meetings", params={"unfiled": True}, headers=auth(ALICE)).json()["items"]
        assert [m["id"] for m in unfiled] == [loose["id"]]
        assert len(c.get("/meetings", headers=auth(ALICE)).json()["items"]) == 4
        both = c.get("/meetings", params={"folder_id": f["id"], "unfiled": True}, headers=auth(ALICE))
        assert both.status_code == 422


def test_vocabulary_suggestions_rank_folder_jargon_and_skip_known_terms() -> None:
    with client() as (c, _):
        f = create_folder(c, ALICE)
        other = create_folder(c, ALICE, name="Other")
        m1 = create_meeting(c, ALICE, folder_id=f["id"])
        m2 = create_meeting(c, ALICE, folder_id=f["id"])
        elsewhere = create_meeting(c, ALICE, folder_id=other["id"])
        put_notes(c, ALICE, m1["id"], "LR(1) vs LALR, see FIRST/FOLLOW", "plain words only", "lalr tables")
        put_notes(c, ALICE, m2["id"], "LALR again; LR(1) again", "DFA minimisation")
        put_notes(c, ALICE, elsewhere["id"], "eVTOL eVTOL eVTOL")
        c.patch(f"/folders/{f['id']}", json={"vocabulary": ["dfa"]}, headers=auth(ALICE))

        r = c.get(f"/folders/{f['id']}/vocabulary/suggestions", headers=auth(ALICE))
        assert r.status_code == 200
        assert r.json()["suggestions"] == [
            {"term": "LR(1)", "count": 2},  # ties keep first appearance
            {"term": "LALR", "count": 2},  # plain lowercase "lalr" is not jargon, so it is not counted
            {"term": "FIRST/FOLLOW", "count": 1},
        ]
