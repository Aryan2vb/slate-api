from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import fake_container


def _get(params: dict[str, str], user_agent: str = "Mozilla/5.0 (Macintosh)"):
    with TestClient(create_app(fake_container())) as c:
        return c.get("/auth/return", params=params, headers={"User-Agent": user_agent})


def test_forwards_the_code_to_the_app_link() -> None:
    r = _get({"to": "exp://10.0.0.5:8081/--/auth-callback", "code": "abc"})
    assert r.status_code == 200
    assert 'href="exp://10.0.0.5:8081/--/auth-callback?code=abc"' in r.text
    assert 'location.replace("exp://10.0.0.5:8081/--/auth-callback?code=abc")' in r.text
    assert r.headers["cache-control"] == "no-store"


def test_forwards_errors() -> None:
    r = _get({"to": "slate://auth-callback", "error": "access_denied", "error_description": "denied"})
    assert "slate://auth-callback?error=access_denied&amp;error_description=denied" in r.text


def test_refuses_web_targets_so_it_is_not_an_open_redirect() -> None:
    for target in ("https://evil.example", "javascript:alert(1)", "//evil.example"):
        r = _get({"to": target, "code": "abc"})
        assert r.status_code == 400
        assert "location.replace" not in r.text and "abc" not in r.text


def test_rejects_markup_in_the_target() -> None:
    r = _get({"to": 'slate://x"><script>alert(1)</script>', "code": "c"})
    assert r.status_code == 400 and "alert(1)" not in r.text


def test_script_string_cannot_close_the_script_tag() -> None:
    from app.api.routes.auth_return import _script_string

    assert "</script>" not in _script_string("slate://a</script><b>&")


def test_android_gets_an_intent_link_for_the_owning_app() -> None:
    android = "Mozilla/5.0 (Linux; Android 15; Pixel 8) Chrome/140 Mobile"
    expo = _get({"to": "exp://10.0.0.5:8081/--/auth-callback", "code": "abc"}, android)
    assert 'href="intent://10.0.0.5:8081/--/auth-callback?code=abc#Intent;scheme=exp;end"' in expo.text
    app = _get({"to": "slate://auth-callback", "code": "abc"}, android)
    assert "intent://auth-callback?code=abc#Intent;scheme=slate;package=com.aryansoni.slate;end" in app.text


HANDOFF = "h" * 40


def test_handoff_parks_the_result_once_for_the_app_to_collect() -> None:
    with TestClient(create_app(fake_container())) as c:
        assert c.get(f"/auth/handoff/{HANDOFF}").status_code == 404  # nothing yet
        page = c.get("/auth/return", params={"to": "exp://h/--/auth-callback", "handoff": HANDOFF, "code": "abc"})
        assert "Close this tab" in page.text
        first = c.get(f"/auth/handoff/{HANDOFF}")
        assert first.status_code == 200 and first.json() == {"code": "abc"}
        assert c.get(f"/auth/handoff/{HANDOFF}").status_code == 404  # one-time


def test_handoff_ids_must_be_long_random_tokens() -> None:
    with TestClient(create_app(fake_container())) as c:
        assert c.get("/auth/handoff/short").status_code == 422
        bad = c.get("/auth/return", params={"to": "exp://h/--/auth-callback", "handoff": "x", "code": "abc"})
        assert bad.status_code == 422


async def test_handoffs_expire() -> None:
    from app.adapters.handoff.memory import InMemoryAuthHandoffs

    now = [0.0]
    store = InMemoryAuthHandoffs(ttl_s=300, clock=lambda: now[0])
    await store.put(HANDOFF, {"code": "abc"})
    now[0] = 301
    assert await store.take(HANDOFF) is None
