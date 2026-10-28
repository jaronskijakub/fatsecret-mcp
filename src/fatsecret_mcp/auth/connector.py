"""The connector's OAuth endpoints, sharing one set of sign-in limits, password workers, spent codes
and the connector apps' client metadata.

Each endpoint's steps live in its own module: `authorization` shows the sign-in page, `sign_in`
checks the password and issues a code, `grants` exchanges codes and refresh tokens, and `sessions`
issues and verifies the tokens that select a User's Food Diary.
"""

from __future__ import annotations

import httpx2
from starlette.requests import Request
from starlette.responses import Response

from fatsecret_mcp.auth.authorization import start_authorization
from fatsecret_mcp.auth.client_metadata import KNOWN_CLIENTS, ClientMetadata
from fatsecret_mcp.auth.grants import SpentCodes, grant_tokens
from fatsecret_mcp.auth.models import AccessClaims, ConnectorIdentity
from fatsecret_mcp.auth.passwords import PasswordChecks
from fatsecret_mcp.auth.sessions import verify_access_token
from fatsecret_mcp.auth.sign_in import sign_in
from fatsecret_mcp.auth.throttling import SignInThrottle
from fatsecret_mcp.config.models import Household
from fatsecret_mcp.domain.diary_day import Clock


class ConnectorAuth:
    """One per HTTP app, so every request shares the same limits and workers."""

    def __init__(self, household: Household, http: httpx2.AsyncClient, clock: Clock) -> None:
        clients = [KNOWN_CLIENTS[name] for name in household.connector_clients]

        self.household = household
        self.clock = clock
        self.client_metadata = ClientMetadata(http, clock, clients)
        self.identity = ConnectorIdentity.of(household)
        self.spent_codes = SpentCodes()
        self.throttle = SignInThrottle()
        self.password_checks = PasswordChecks()

    async def authorize(self, request: Request) -> Response:
        """GET shows the sign-in page; POST submits it."""
        if request.method == "POST":
            return await sign_in(
                request,
                identity=self.identity,
                household=self.household,
                throttle=self.throttle,
                password_checks=self.password_checks,
                clock=self.clock,
            )

        return await start_authorization(
            request,
            identity=self.identity,
            client_metadata=self.client_metadata,
            clock=self.clock,
        )

    async def token(self, request: Request) -> Response:
        return await grant_tokens(
            request,
            identity=self.identity,
            household=self.household,
            spent_codes=self.spent_codes,
            clock=self.clock,
        )

    def authenticate(self, token: str) -> AccessClaims | None:
        return verify_access_token(
            token,
            self.identity,
            self.household,
            self.clock().timestamp(),
        )
