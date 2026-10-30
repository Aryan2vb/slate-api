"""End to end over HTTP with real Postgres: create → notes → upload chunk → finish → worker → ready.

Vendors are swapped for the in-process stubs/fakes; everything else (routes, services, SQL repositories, job
runner, handlers, grounding) is the production code path.
"""

import uuid

import httpx
import pytest

from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory
from app.adapters.llm.stub import StubLanguageModel
from app.jobs.runner import JobRunner
from app.main import create_app
from tests.fakes import FakeStorage, fake_container

pytestmark = pytest.mark.db


async def test_meeting_flows_from_upload_to_enhanced_notes(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    storage = FakeStorage()
    container = fake_container(uow=uow_factory, storage=storage, llm=StubLanguageModel())
    app = create_app(container)
    app.state.container = container  # ASGITransport does not run lifespan
    user = uuid.uuid4()
    h = {"Authorization": f"Bearer user:{user}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        meeting = (await c.post("/meetings", json={"title": "Pricing review"}, headers=h)).json()
        mid = meeting["id"]

        line_id = str(uuid.uuid4())
        notes = {"lines": [{"id": line_id, "position": 0, "text": "Q3 pricing +5%", "t_ms": 5000}]}
        assert (await c.put(f"/meetings/{mid}/notes", json=notes, headers=h)).status_code == 200

        chunk = (
            await c.post(
                f"/meetings/{mid}/chunks", json={"seq": 0, "mime_type": "audio/mp4", "byte_size": 3}, headers=h
            )
        ).json()
        storage.objects[chunk["upload"]["path"]] = b"aud"  # what the phone's signed PUT would have stored
        assert (await c.post(f"/meetings/{mid}/chunks/{chunk['chunk_id']}/uploaded", headers=h)).status_code == 200
        assert (await c.post(f"/meetings/{mid}/finish", headers=h)).status_code == 202

        runner = JobRunner.from_container(container)
        while await runner.run_once():
            pass

        detail = (await c.get(f"/meetings/{mid}", headers=h)).json()
        assert detail["stage"] == "ready", detail
        assert len(detail["transcript"]) == 2
        assert storage.deleted == [chunk["upload"]["path"]]
        user_blocks = [b for s in detail["enhanced"]["sections"] for b in s["blocks"] if b["source"] == "user"]
        assert any(b["text"] == "Q3 pricing +5%" for b in user_blocks)

        other = {"Authorization": f"Bearer user:{uuid.uuid4()}"}
        assert (await c.get(f"/meetings/{mid}", headers=other)).status_code == 404
