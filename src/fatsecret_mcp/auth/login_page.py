"""The connector's sign-in page: its HTML form and the headers that keep it private."""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse

from fatsecret_mcp.auth.models import LoginRequest

INVALID_SIGN_IN = "Invalid user name or password."
THROTTLED_SIGN_IN = "Too many sign-in attempts. Try again shortly."
FORM_EXHAUSTED = "Too many submissions of this form. Start sign-in again from your connector."


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
<p><label>User name <input name="username" autocomplete="username" required></label></p>
<p><label>Connector password <input type="password" name="password" autocomplete="current-password" required>
</label></p><button type="submit">Connect to {escape(app_name)}</button>
</form>"""


def _document(message: str, form: str, app_name: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Connect your FatSecret diary</title></head>
<body><main><h1>Connect your FatSecret diary</h1>
<p>Sign in to let {escape(app_name)} read your diary and save the drafts you confirm.</p>
{message}
{form}</main></body></html>"""


def _headers(redirect_uri: str) -> dict[str, str]:
    """Allow the form to post only here and to the verified callback, and keep the page unframed."""
    callback = urlsplit(redirect_uri)
    callback_source = f"{callback.scheme}://{callback.netloc}{callback.path}"

    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",
        "Content-Security-Policy": (
            f"default-src 'none'; form-action 'self' {callback_source}; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
    }
