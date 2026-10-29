"""The connector's sign-in page: its HTML form and the headers that keep it private."""

from __future__ import annotations

import base64
import hashlib
from html import escape
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse

from fatsecret_mcp.auth.models import LoginRequest
from fatsecret_mcp.icons.links import WEBSITE_URL
from fatsecret_mcp.icons.marks import mark_svg

INVALID_SIGN_IN = "Invalid user name or password."
THROTTLED_SIGN_IN = "Too many sign-in attempts. Try again shortly."
FORM_EXHAUSTED = "Too many submissions of this form. Start sign-in again from your connector."

_STYLE = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
gap:20px;padding:24px 16px;background:#fafafa;color:#0a0a0a;-webkit-font-smoothing:antialiased;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{width:100%;max-width:400px;background:#fff;border:1px solid #eaeaea;border-radius:12px;padding:32px;
box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.04)}
.mark svg{display:block;width:40px;height:40px;margin-bottom:20px}
h1{font-size:24px;line-height:1.25;font-weight:600;letter-spacing:-.025em;margin:0 0 8px}
p{margin:0 0 16px;color:#666}
p strong{color:#0a0a0a;font-weight:500;background:#f2f2f2;border-radius:4px;padding:1px 6px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
[role=alert]{color:#b42318;background:#fef3f2;border:1px solid #fecdca;border-radius:8px;padding:10px 12px;
font-size:14px}
form{margin-top:24px}
label{display:block;font-size:13px;font-weight:500;margin-bottom:16px}
input{display:block;width:100%;margin-top:6px;height:40px;padding:0 12px;font:inherit;color:inherit;
background:#fff;border:1px solid #e5e5e5;border-radius:6px;outline:none}
input:focus{border-color:#0a0a0a;box-shadow:0 0 0 3px rgba(0,0,0,.15)}
button{width:100%;height:40px;margin-top:8px;font:inherit;font-weight:500;color:#fff;background:#0a0a0a;
border:0;border-radius:6px;cursor:pointer}
button:hover{background:#333}
button:focus-visible{outline:2px solid #0a0a0a;outline-offset:2px}
footer a{display:flex;align-items:center;gap:6px;color:#666;font-size:13px;text-decoration:none}
footer a:hover{color:#0a0a0a}
footer svg{display:block;width:18px;height:18px}
"""

# The policy allows exactly this stylesheet, so the page still runs no other style or any script.
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()


def login_page(
    sealed: str | None,
    login: LoginRequest,
    *,
    alert: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The page with a sign-in form for the sealed login request, or only the alert without one."""
    redirect_uri = login.authorization.redirect_uri
    app_name = login.app_name
    message = f'<p role="alert">{alert}</p>' if alert else ""
    form = _form(sealed, redirect_uri, app_name) if sealed is not None else ""

    return HTMLResponse(
        content=_document(message, form, app_name),
        status_code=status_code,
        headers=_headers(redirect_uri),
    )


def throttled_page(sealed: str, login: LoginRequest, retry_after: int) -> HTMLResponse:
    response = login_page(
        sealed,
        login,
        alert=THROTTLED_SIGN_IN,
        status_code=429,
    )

    response.headers["Retry-After"] = str(retry_after)

    return response


def _form(sealed: str, redirect_uri: str, app_name: str) -> str:
    """The form; the host it names comes from a callback already pinned for this app."""
    callback_host = urlsplit(redirect_uri).hostname or ""

    return f"""<p>After you sign in, your browser returns to <strong>{escape(callback_host)}</strong>.</p>
<form method="post" action="/authorize">
<input type="hidden" name="request" value="{escape(sealed, quote=True)}">
<label>User name <input name="username" autocomplete="username" required></label>
<label>Connector password <input type="password" name="password" autocomplete="current-password" required>
</label><button type="submit">Connect to {escape(app_name)}</button>
</form>"""


def _document(message: str, form: str, app_name: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect your FatSecret diary</title><style>{_STYLE}</style></head>
<body><main><div class="mark" aria-hidden="true">{mark_svg("fs.svg")}</div>
<h1>Connect your FatSecret diary</h1>
<p>Sign in to let {escape(app_name)} read your diary and save the drafts you confirm.</p>
{message}
{form}</main>
<footer><a href="{WEBSITE_URL}" target="_blank" rel="noopener noreferrer"
aria-label="fatsecret-mcp on GitHub, by jj.">by {mark_svg("jj.svg")}</a></footer></body></html>"""


def _headers(redirect_uri: str) -> dict[str, str]:
    """Allow only this page's stylesheet and posts to here or the verified callback; forbid framing."""
    callback = urlsplit(redirect_uri)
    callback_source = f"{callback.scheme}://{callback.netloc}{callback.path}"

    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; "
            f"form-action 'self' {callback_source}; frame-ancestors 'none'; base-uri 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
    }
