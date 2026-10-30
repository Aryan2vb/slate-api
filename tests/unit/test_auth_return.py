from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import fake_container


def _get(params: dict[str, str]):
    with TestClient(create_app(fake_container())) as c:
        return c.get("/auth/return", params=params)


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
