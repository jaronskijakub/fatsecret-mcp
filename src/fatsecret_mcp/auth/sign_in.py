"""POST /authorize: turn a submitted name and password into a one-use code for the connector app."""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlencode

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from fatsecret_mcp.auth.authorization import LOGIN_COOKIE
from fatsecret_mcp.auth.login_page import FORM_EXHAUSTED, INVALID_SIGN_IN, login_page, throttled_page
from fatsecret_mcp.auth.models import CodeClaims, ConnectorIdentity, LoginRequest
from fatsecret_mcp.auth.passwords import DUMMY_PASSWORD_HASH, PasswordChecks
from fatsecret_mcp.auth.responses import NO_STORE, is_form_post, oauth_error
from fatsecret_mcp.auth.sessions import find_user
from fatsecret_mcp.auth.throttling import SignInThrottle
from fatsecret_mcp.auth.tokens import InvalidCredential, open_token, seal_token
from fatsecret_mcp.config.models import Household, User
from fatsecret_mcp.domain.diary_day import Clock

_CODE_LIFETIME = 60
# Both password workers are busy only for a moment, so a second is enough before trying again.
_BUSY_RETRY_AFTER = 1


async def sign_in(
    request: Request,
    *,
    identity: ConnectorIdentity,
    household: Household,
    throttle: SignInThrottle,
    password_checks: PasswordChecks,
    clock: Clock,
) -> Response:
    if not _is_same_origin_form(request, identity):
        return oauth_error("invalid_request")

    form = await request.form()
    sealed = str(form.get("request", ""))
    login = _open_login_request(sealed, request, identity, clock)

    if login is None:
        return oauth_error("invalid_request")

    username = str(form.get("username", "")).strip().casefold()
    password = str(form.get("password", ""))
    refusal = _refused_by_limits(throttle, login, sealed, username, clock)

    if refusal is not None:
        return refusal

    user = find_user(household, username)
    verified = await _check_password(password_checks, user, password)

    if verified is None:
        return throttled_page(sealed, login, _BUSY_RETRY_AFTER)

    if user is None or not verified:
        return login_page(
            sealed,
            login,
            alert=INVALID_SIGN_IN,
            status_code=401,
        )

    throttle.succeeded(username)

    return _redirect_with_code(login, user, identity, clock().timestamp())


def _is_same_origin_form(request: Request, identity: ConnectorIdentity) -> bool:
    """A form post from our own sign-in page; browsers that send no Origin are allowed."""
    origin = request.headers.get("origin", identity.base_url)

    return origin == identity.base_url and is_form_post(request)


def _open_login_request(
    sealed: str, request: Request, identity: ConnectorIdentity, clock: Clock
) -> LoginRequest | None:
    """The signed, unexpired request this form was shown for, in the browser that opened it."""
    try:
        login = open_token(sealed, identity.signing_key, LoginRequest, now=clock().timestamp())
    except InvalidCredential:
        return None

    cookie = request.cookies.get(LOGIN_COOKIE, "")

    if not hmac.compare_digest(cookie.encode(), login.csrf.encode()):
        return None

    return login


def _refused_by_limits(
    throttle: SignInThrottle, login: LoginRequest, sealed: str, username: str, clock: Clock
) -> HTMLResponse | None:
    """Count the attempt server-wide, for this form and for this name, before any password check."""
    retry_after = throttle.admit_server(clock().timestamp())

    if retry_after is not None:
        return throttled_page(sealed, login, retry_after)

    if not throttle.admit_form(login.csrf, login.expires_at, clock().timestamp()):
        return login_page(
            None,
            login,
            alert=FORM_EXHAUSTED,
            status_code=429,
        )

    retry_after = throttle.admit(username, clock().timestamp())

    if retry_after is not None:
        return throttled_page(sealed, login, retry_after)

    return None


async def _check_password(password_checks: PasswordChecks, user: User | None, password: str) -> bool | None:
    """Whether the password matches, or None when both password workers are busy.

    An unknown name is checked against a dummy hash, so it takes as long as a wrong password.
    """
    stored = user.connector_password_hash if user else None
    password_hash = stored.get_secret_value() if stored else DUMMY_PASSWORD_HASH

    return await anyio.to_thread.run_sync(password_checks.verify, password, password_hash)


def _redirect_with_code(login: LoginRequest, user: User, identity: ConnectorIdentity, now: float) -> Response:
    """Send the browser back to the app's callback with a one-use code for this User."""
    code = CodeClaims(
        authorization=login.authorization,
        user_id=user.id,
        session_generation=user.session_generation,
        expires_at=now + _CODE_LIFETIME,
        nonce=secrets.token_urlsafe(32),
    )
    query = {
        "code": seal_token(code, identity.signing_key),
        "iss": identity.issuer,
    }

    if login.authorization.state is not None:
        query["state"] = login.authorization.state

    redirect_uri = login.authorization.redirect_uri
    separator = "&" if "?" in redirect_uri else "?"
    response = RedirectResponse(
        url=redirect_uri + separator + urlencode(query),
        status_code=302,
        headers=NO_STORE,
    )

    response.delete_cookie(LOGIN_COOKIE, path="/authorize")

    return response
