import uuid

import pytest
from fastapi.testclient import TestClient

from app.adapters.ratelimit.memory import InMemoryRateLimiter
from app.config import Settings
from app.domain.errors import RateLimitedError
from app.main import create_app
from app.services.catchup import CatchUp
from tests.fakes import FakeLlm, fake_container


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_sliding_window_blocks_then_recovers() -> None:
    clock = Clock()
    limiter = InMemoryRateLimiter(clock=clock)
    for _ in range(3):
        await limiter.hit("catchup:u1", limit=3, window_s=60)
    with pytest.raises(RateLimitedError) as exc:
        await limiter.hit("catchup:u1", limit=3, window_s=60)
    assert exc.value.retry_after_s == 60
    await limiter.hit("catchup:u2", limit=3, window_s=60)  # other users have their own budget
    clock.t += 60
    await limiter.hit("catchup:u1", limit=3, window_s=60)  # oldest hits have left the window


def test_catch_up_returns_429_with_retry_after() -> None:
    settings = Settings(env="test", run_worker_in_process=False, rate_limit_catchup_per_hour=1)
    user = uuid.uuid4()
    headers = {"Authorization": f"Bearer user:{user}"}
    with TestClient(create_app(fake_container(settings=settings, llm=FakeLlm([CatchUp(summary="s", points=[])])))) as c:
        meeting = c.post("/meetings", json={"title": "t"}, headers=headers).json()
        url = f"/meetings/{meeting['id']}/catch-up"
        first = c.post(url, json={"minutes": 5, "live_transcript": "hello there"}, headers=headers)
        assert first.status_code != 429
        second = c.post(url, json={"minutes": 5, "live_transcript": "hello there"}, headers=headers)
        assert second.status_code == 429
        assert int(second.headers["Retry-After"]) > 0
        assert second.json()["error"]["type"] == "RateLimitedError"
