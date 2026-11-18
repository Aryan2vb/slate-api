"""HTTPS end of the mobile Google sign-in, between Supabase's OAuth redirect and the app.

Supabase redirects here with the PKCE `code`. Two ways back to the app, whichever works first:

1. Hand-off (always works): the app sent a random `handoff` id; the result is parked under it and the app collects
   it once from GET /auth/handoff/{id} after the user closes the tab. No app link involved, which matters in Expo
   Go, where Chrome cannot reliably hand a link back.
2. App link: the page also links to the app (an intent:// link on Android, which Chrome honours on tap).

Only app-scheme targets are accepted, so this is not an open redirect to websites. The `code` is useless without
the PKCE verifier that never leaves the device, and a hand-off is one-time and expires after five minutes.
"""

import html
import json
import re
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import HTMLResponse

from app.api.deps import ContainerDep
from app.domain.errors import NotFoundError
from app.presentation import BRAND_HEADER, PAGE_CSS

router = APIRouter(tags=["auth"])

# App links only (no web targets), and only ordinary URL characters.
APP_LINK = re.compile(r"^(slate|exp)://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]*$")
HANDOFF_ID = r"^[A-Za-z0-9_-]{32,128}$"
FORWARDED_PARAMS = ("code", "error", "error_code", "error_description")
# Android Chrome will not open a custom-scheme link, even on tap, but honours intent:// links. slate:// names the
# Slate app's package. Expo Go (exp://, development only) is left unnamed: with its package named, Chrome failed to
# match Expo Go's link filters and opened the Play Store instead.
ANDROID_PACKAGES = {"slate": "com.aryansoni.slate"}


def android_intent(link: str) -> str:
    scheme, rest = link.split("://", 1)
    package = f"package={ANDROID_PACKAGES[scheme]};" if scheme in ANDROID_PACKAGES else ""
    return f"intent://{rest}#Intent;scheme={scheme};{package}end"


@router.get("/auth/return", response_class=HTMLResponse, include_in_schema=False)
async def auth_return(
    request: Request,
    container: ContainerDep,
    to: str = Query(max_length=500),
    handoff: str | None = Query(default=None, pattern=HANDOFF_ID),
    code: str | None = Query(default=None, max_length=500),
    error: str | None = Query(default=None, max_length=200),
    error_code: str | None = Query(default=None, max_length=100),
    error_description: str | None = Query(default=None, max_length=500),
) -> HTMLResponse:
    if not APP_LINK.fullmatch(to):
        return HTMLResponse(_page(None, "This sign-in link is not valid."), status_code=400)
    values = dict(zip(FORWARDED_PARAMS, (code, error, error_code, error_description), strict=True))
    params = {k: v for k, v in values.items() if v}
    if handoff and params:
        await container.handoffs.put(handoff, params)
    target = f"{to}{'&' if '?' in to else '?'}{urlencode(params)}" if params else to
    if "Android" in request.headers.get("user-agent", ""):
        target = android_intent(target)
    if error:
        message = "Sign-in failed. Close this tab and try again in Slate."
    else:
        message = "You're signed in. Close this tab to go back to Slate." if handoff else "You're signed in."
    return HTMLResponse(
        _page(target, message, failed=bool(error)),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/auth/handoff/{handoff_id}", include_in_schema=False)
async def take_handoff(container: ContainerDep, handoff_id: Annotated[str, Path(pattern=HANDOFF_ID)]) -> dict[str, str]:
    result = await container.handoffs.take(handoff_id)
    if result is None:
        raise NotFoundError("No sign-in waiting")
    return result


def _script_string(value: str) -> str:
    """JSON string literal that is also safe inside <script>: no '</script>' or HTML-significant characters."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(target: str | None, message: str, *, failed: bool = False) -> str:
    button = (
        f'<a class="btn" href="{html.escape(target, quote=True)}">Return to Slate</a>'
        f"<script>location.replace({_script_string(target)});</script>"
        if target
        else ""
    )
    failed = failed or target is None
    heading = "Let's try that again" if failed else "You're all set."
    symbol = "!" if failed else "✓"
    hint = (
        "Open Slate to try signing in again." if failed else "You can safely close this tab once you're back in Slate."
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Sign in · Slate</title>
<style>{PAGE_CSS}</style></head><body class="status-page">{BRAND_HEADER}<main>
<div class="status-symbol" aria-hidden="true">{symbol}</div><div class="eyebrow">Slate sign-in</div>
<h1>{heading}</h1><p class="message">{html.escape(message)}</p>{button}
<p class="hint">{hint}</p></main></body></html>"""
