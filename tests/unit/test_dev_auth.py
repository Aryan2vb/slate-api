import uuid

import pytest
from pydantic import ValidationError

from app.adapters.auth.dev import DevAuthVerifier
from app.config import Settings
from tests.fakes import FakeAuth


async def test_dev_token_is_the_dev_user_and_real_tokens_still_work() -> None:
    dev_id = uuid.uuid4()
    verifier = DevAuthVerifier(FakeAuth(), dev_id)
    assert (await verifier.verify("dev")).id == dev_id
    real = uuid.uuid4()
    assert (await verifier.verify(f"user:{real}")).id == real


def test_dev_auth_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="DEV_AUTH"):
        Settings(env="prod", dev_auth=True)
    assert Settings(env="dev", dev_auth=True).dev_auth
