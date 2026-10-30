"""HTTPS hop between Supabase's OAuth redirect and the mobile app.

Android Chrome often refuses to open an app link (slate://, exp://) reached through an automatic redirect at the
end of a login chain, leaving the tab stuck "loading". Supabase therefore redirects here; this page forwards the
OAuth result to the app link and offers a button, since a user tap is always allowed to open the app.

Only app-scheme targets are accepted, so this page cannot be used as an open redirect to websites. The `code`
it forwards is a PKCE code, useless without the verifier that never leaves the device.
"""

import html
import json
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["auth"])

# App links only (no web targets), and only ordinary URL characters.
APP_LINK = re.compile(r"^(slate|exp)://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]*$")
FORWARDED_PARAMS = ("code", "error", "error_code", "error_description")
# Android Chrome will not open a custom-scheme link, even on tap, but always honours an intent:// link naming the
# app's package. exp:// belongs to Expo Go during development; slate:// to the Slate app.
ANDROID_PACKAGES = {"exp": "host.exp.exponent", "slate": "com.aryansoni.slate"}


def android_intent(link: str) -> str:
    scheme, rest = link.split("://", 1)
    return f"intent://{rest}#Intent;scheme={scheme};package={ANDROID_PACKAGES[scheme]};end"


@router.get("/auth/return", response_class=HTMLResponse, include_in_schema=False)
async def auth_return(
    request: Request,
    to: str = Query(max_length=500),
    code: str | None = Query(default=None, max_length=500),
    error: str | None = Query(default=None, max_length=200),
    error_code: str | None = Query(default=None, max_length=100),
    error_description: str | None = Query(default=None, max_length=500),
) -> HTMLResponse:
    if not APP_LINK.fullmatch(to):
        return HTMLResponse(_page(None, "This sign-in link is not valid."), status_code=400)
    values = dict(zip(FORWARDED_PARAMS, (code, error, error_code, error_description), strict=True))
    params = {k: v for k, v in values.items() if v}
    target = f"{to}{'&' if '?' in to else '?'}{urlencode(params)}" if params else to
    if "Android" in request.headers.get("user-agent", ""):
        target = android_intent(target)
    message = "Sign-in failed. Return to Slate to try again." if error else "You're signed in."
    return HTMLResponse(_page(target, message), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _script_string(value: str) -> str:
    """JSON string literal that is also safe inside <script>: no '</script>' or HTML-significant characters."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _page(target: str | None, message: str) -> str:
    button = (
        f'<a class="btn" href="{html.escape(target, quote=True)}">Return to Slate</a>'
        f"<script>location.replace({_script_string(target)});</script>"
        if target
        else ""
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Slate</title>
<style>
body{{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
gap:20px;padding:24px;background:#F6F4EF;color:#1F1E1B;font:16px/1.5 system-ui,sans-serif;text-align:center}}
h1{{font:400 30px Georgia,serif;margin:0}}
.btn{{display:inline-block;padding:14px 28px;border-radius:999px;background:#1F1E1B;color:#F6F4EF;
text-decoration:none;font-weight:600}}
@media (prefers-color-scheme:dark){{body{{background:#151513;color:#ECEAE4}}.btn{{background:#ECEAE4;color:#151513}}}}
</style></head><body><h1>Slate</h1><p>{html.escape(message)}</p>{button}</body></html>"""
