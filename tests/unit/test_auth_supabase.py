import json
import time
import uuid

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from app.adapters.auth.supabase import SupabaseJwtVerifier
from app.domain.errors import AuthenticationError

BASE = "https://proj.supabase.co"
JWKS_URL = f"{BASE}/auth/v1/.well-known/jwks.json"
ISS = f"{BASE}/auth/v1"


def _key(kid: str) -> tuple[ec.EllipticCurvePrivateKey, dict]:
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    return private, jwk | {"kid": kid, "alg": "ES256", "use": "sig"}


def _token(private: ec.EllipticCurvePrivateKey, kid: str, **overrides: object) -> str:
    now = int(time.time())
    claims = {"sub": str(uuid.uuid4()), "email": "a@b.com", "aud": "authenticated", "iss": ISS,
              "iat": now, "exp": now + 3600} | overrides  # fmt: skip
    return jwt.encode(claims, private, algorithm="ES256", headers={"kid": kid})


def _verifier() -> SupabaseJwtVerifier:
    return SupabaseJwtVerifier(supabase_url=BASE, audience="authenticated", min_refresh_interval_s=0)


@respx.mock
async def test_valid_token_maps_claims() -> None:
    private, jwk = _key("k1")
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})
    sub = str(uuid.uuid4())
    user = await _verifier().verify(_token(private, "k1", sub=sub))
    assert str(user.id) == sub
    assert user.email == "a@b.com"


@respx.mock
async def test_keys_are_cached() -> None:
    private, jwk = _key("k1")
    route = respx.get(JWKS_URL).respond(json={"keys": [jwk]})
    verifier = _verifier()
    await verifier.verify(_token(private, "k1"))
    await verifier.verify(_token(private, "k1"))
    assert route.call_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "anon"},
        {"iss": "https://evil.example/auth/v1"},
        {"sub": "not-a-uuid"},
    ],
    ids=["expired", "wrong-aud", "wrong-iss", "bad-sub"],
)
@respx.mock
async def test_rejects_bad_claims(overrides: dict) -> None:
    private, jwk = _key("k1")
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})
    with pytest.raises(AuthenticationError):
        await _verifier().verify(_token(private, "k1", **overrides))


@respx.mock
async def test_wrong_signing_key_rejected() -> None:
    _, jwk = _key("k1")
    other, _ = _key("k1")
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})
    with pytest.raises(AuthenticationError):
        await _verifier().verify(_token(other, "k1"))


@respx.mock
async def test_unknown_kid_refetches_once() -> None:
    old_private, old_jwk = _key("old")
    new_private, new_jwk = _key("new")
    route = respx.get(JWKS_URL)
    route.side_effect = [
        httpx.Response(200, json={"keys": [old_jwk]}),
        httpx.Response(200, json={"keys": [old_jwk, new_jwk]}),
    ]
    verifier = _verifier()
    await verifier.verify(_token(old_private, "old"))
    await verifier.verify(_token(new_private, "new"))
    assert route.call_count == 2


@respx.mock
async def test_kid_missing_after_refetch_rejected() -> None:
    private, jwk = _key("k1")
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})
    with pytest.raises(AuthenticationError):
        await _verifier().verify(_token(private, "unknown"))


@respx.mock
async def test_unknown_kid_refetch_is_rate_limited() -> None:
    private, jwk = _key("k1")
    route = respx.get(JWKS_URL).respond(json={"keys": [jwk]})
    verifier = SupabaseJwtVerifier(supabase_url=BASE, audience="authenticated", min_refresh_interval_s=60)
    await verifier.verify(_token(private, "k1"))
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            await verifier.verify(_token(private, f"random-{uuid.uuid4()}"))
    assert route.call_count == 1


@pytest.mark.parametrize("token", ["garbage", "a.b.c", ""])
@respx.mock
async def test_garbage_token_rejected(token: str) -> None:
    route = respx.get(JWKS_URL).respond(json={"keys": []})
    with pytest.raises(AuthenticationError):
        await _verifier().verify(token)
    assert route.call_count == 0


@respx.mock
async def test_hs256_token_rejected_without_fetch() -> None:
    route = respx.get(JWKS_URL).respond(json={"keys": []})
    token = jwt.encode({"sub": str(uuid.uuid4())}, "secret-key-that-is-long-enough-123", algorithm="HS256",
                       headers={"kid": "k1"})  # fmt: skip
    with pytest.raises(AuthenticationError):
        await _verifier().verify(token)
    assert route.call_count == 0


@respx.mock
async def test_jwks_outage_without_cache_is_auth_error() -> None:
    private, _ = _key("k1")
    respx.get(JWKS_URL).respond(503)
    with pytest.raises(AuthenticationError):
        await _verifier().verify(_token(private, "k1"))
