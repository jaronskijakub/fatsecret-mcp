"""The connector's identity and the data carried in its signed credentials."""

from __future__ import annotations

from typing import Literal

from mcp.shared.auth import OAuthClientInformationFull
from pydantic import BaseModel, ConfigDict, Field

from fatsecret_mcp.config.models import Household


class FrozenModel(BaseModel):
    """Immutable, and refuses fields it doesn't declare."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )


class ConnectorIdentity(FrozenModel):
    """Where this Household's connector lives, and the key that signs its credentials."""

    base_url: str
    resource: str
    issuer: str
    signing_key: str = Field(repr=False)

    @classmethod
    def of(cls, household: Household) -> ConnectorIdentity:
        base_url = household.public_base_url

        assert base_url is not None

        return cls(
            base_url=base_url,
            resource=base_url + "/mcp",
            issuer=base_url + "/",
            signing_key=household.signing_key.get_secret_value(),
        )


class KnownClient(FrozenModel):
    """A connector app the server can let in: its published client ID and the callbacks pinned for it.

    `config_name` is how the Operator turns it on in `FATSECRET_MCP_CLIENTS`; `app_name` is what the
    sign-in page shows.
    """

    config_name: str
    app_name: str
    client_id: str
    callbacks: frozenset[str]


class ClientDocument(OAuthClientInformationFull):
    """A client's published metadata, with the token endpoint methods it supports besides its own."""

    token_endpoint_auth_methods_supported: list[str] | None = None


class SignedValue(FrozenModel):
    expires_at: float


class FetchLimited(FrozenModel):
    """No client's metadata may be fetched again for `retry_after` seconds."""

    retry_after: int


class AuthorizationDetails(FrozenModel):
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scopes: tuple[str, ...]
    state: str | None


class LoginRequest(SignedValue):
    kind: Literal["login"] = "login"
    authorization: AuthorizationDetails
    app_name: str
    csrf: str


class CodeClaims(SignedValue):
    kind: Literal["code"] = "code"
    authorization: AuthorizationDetails
    user_id: str
    session_generation: int
    nonce: str


class SessionClaims(SignedValue):
    """A signed-in User's connector session, as carried by its access and refresh tokens."""

    issuer: str
    audience: str
    user_id: str
    session_generation: int
    client_id: str
    scopes: tuple[str, ...]
    nonce: str


class AccessClaims(SessionClaims):
    kind: Literal["access"] = "access"


class RefreshClaims(SessionClaims):
    kind: Literal["refresh"] = "refresh"
