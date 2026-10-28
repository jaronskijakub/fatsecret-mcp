"""Connector sessions: issuing access and refresh tokens, and checking that a session still holds."""

from __future__ import annotations

import secrets

from mcp.shared.auth import OAuthToken

from fatsecret_mcp.auth.client_metadata import KNOWN_CLIENTS
from fatsecret_mcp.auth.models import AccessClaims, ConnectorIdentity, RefreshClaims, SessionClaims
from fatsecret_mcp.auth.tokens import InvalidCredential, open_token, seal_token
from fatsecret_mcp.config.models import Household, User

ACCESS_LIFETIME = 3600
# Every refresh issues a new refresh token with a fresh lifetime, so a connector that is used
# at least once a month stays signed in. Earlier refresh tokens stay valid until they expire.
REFRESH_LIFETIME = 30 * 24 * 3600


def issue_tokens(
    identity: ConnectorIdentity,
    *,
    user_id: str,
    session_generation: int,
    client_id: str,
    scopes: tuple[str, ...],
    now: float,
) -> OAuthToken:
    access = AccessClaims(
        issuer=identity.issuer,
        audience=identity.resource,
        user_id=user_id,
        session_generation=session_generation,
        client_id=client_id,
        scopes=scopes,
        expires_at=now + ACCESS_LIFETIME,
        nonce=secrets.token_urlsafe(32),
    )
    refresh = RefreshClaims(
        issuer=identity.issuer,
        audience=identity.resource,
        user_id=user_id,
        session_generation=session_generation,
        client_id=client_id,
        scopes=scopes,
        expires_at=now + REFRESH_LIFETIME,
        nonce=secrets.token_urlsafe(32),
    )

    return OAuthToken(
        access_token=seal_token(access, identity.signing_key),
        expires_in=ACCESS_LIFETIME,
        scope=" ".join(scopes),
        refresh_token=seal_token(refresh, identity.signing_key),
    )


def verify_access_token(
    token: str, identity: ConnectorIdentity, household: Household, now: float
) -> AccessClaims | None:
    """The claims of a valid access token: they name the User whose Food Diary it opens."""
    try:
        claims = open_token(token, identity.signing_key, AccessClaims, now=now)
    except InvalidCredential:
        return None

    if not is_current_session(claims, identity, household):
        return None

    return claims


def is_current_session(claims: SessionClaims, identity: ConnectorIdentity, household: Household) -> bool:
    """Whether this server issued the session, its app is still turned on, and its User's
    configuration still allows it.
    """
    if claims.issuer != identity.issuer or claims.audience != identity.resource:
        return False

    if claims.client_id not in _turned_on_client_ids(household):
        return False

    return is_current_generation(household, claims.user_id, claims.session_generation)


def _turned_on_client_ids(household: Household) -> set[str]:
    """The client IDs of the connector apps the Operator turned on, so turning one off signs it out."""
    client_ids = {KNOWN_CLIENTS[name].client_id for name in household.connector_clients}

    return client_ids


def is_current_generation(household: Household, user_id: str, session_generation: int) -> bool:
    """Whether the User still exists and their sessions weren't revoked since this one began."""
    user = find_user(household, user_id)

    return user is not None and user.session_generation == session_generation


def find_user(household: Household, user_id: str) -> User | None:
    try:
        return household.user(user_id)
    except KeyError:
        return None
