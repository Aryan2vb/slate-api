import uuid

from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def _lines(*texts: str) -> list[dict]:
    return [
        {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, t)), "position": i, "text": t, "t_ms": i * 1000}
        for i, t in enumerate(texts)
    ]


def test_replace_is_idempotent_and_ordered() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        url = f"/meetings/{m['id']}/notes"
        body = {"lines": _lines("pricing up Q3", "follow up with Priya")}
        first = c.put(url, json=body, headers=auth(ALICE)).json()
        second = c.put(url, json=body, headers=auth(ALICE)).json()
        assert [x["text"] for x in first["lines"]] == ["pricing up Q3", "follow up with Priya"]
        assert [x["id"] for x in second["lines"]] == [x["id"] for x in first["lines"]]
        detail = c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()
        assert len(detail["notes"]) == 2 and detail["notes"][1]["t_ms"] == 1000


def test_replace_with_fewer_lines_drops_removed_ones() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        url = f"/meetings/{m['id']}/notes"
        c.put(url, json={"lines": _lines("a", "b")}, headers=auth(ALICE))
        r = c.put(url, json={"lines": _lines("a")}, headers=auth(ALICE)).json()
        assert [x["text"] for x in r["lines"]] == ["a"]


def test_rejects_duplicate_ids_positions_and_long_text() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        url = f"/meetings/{m['id']}/notes"
        lines = _lines("a", "b")
        dup_id = [lines[0], {**lines[1], "id": lines[0]["id"]}]
        dup_pos = [lines[0], {**lines[1], "position": 0}]
        too_long = [{**lines[0], "text": "x" * 5001}]
        for bad in (dup_id, dup_pos, too_long):
            assert c.put(url, json={"lines": bad}, headers=auth(ALICE)).status_code == 422


def test_other_users_notes_are_404() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        r = c.put(f"/meetings/{m['id']}/notes", json={"lines": _lines("x")}, headers=auth(BOB))
        assert r.status_code == 404
