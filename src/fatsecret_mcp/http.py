"""The remote connector's HTTP app: it composes the routes and starts each User's MCP tools.

Sign-in and tokens live in `fatsecret_mcp.auth`. This module only turns a valid Bearer token
into the SDK's authenticated User and sends the request to that User's own MCP app.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urlsplit

import anyio
import httpx2
from anyio.abc import TaskGroup, TaskStatus
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from mcp.shared.auth import OAuthMetadata
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from fatsecret_mcp.auth.connector import ConnectorAuth
from fatsecret_mcp.auth.models import AccessClaims
from fatsecret_mcp.config.loader import validate_http_household
from fatsecret_mcp.config.models import Household, User
from fatsecret_mcp.domain.diary_day import Clock, system_clock
from fatsecret_mcp.fatsecret.budget import daily_budget
from fatsecret_mcp.fatsecret.food_cache import FoodCache
from fatsecret_mcp.fatsecret.gateway import FatSecretGateway
from fatsecret_mcp.icons.links import PUBLIC_FILES, server_icons
from fatsecret_mcp.icons.marks import read_file

# The apps fetch icons from other origins and without credentials; the files never change in a release.
_PUBLIC_FILE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "X-Content-Type-Options": "nosniff",
}

# HTTP client debug logs can contain the authorization query or a token response.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)


def build_http_app(
    household: Household,
    fatsecret: FatSecretGateway,
    *,
    clock: Clock = system_clock,
    metadata_http: httpx2.AsyncClient | None = None,
) -> Starlette:
    validate_http_household(household)

    metadata_client = metadata_http if metadata_http is not None else _metadata_client()
    auth = ConnectorAuth(
        household=household,
        http=metadata_client,
        clock=clock,
    )
    food_cache = FoodCache(clock=clock)
    mcp = _UserServers(
        household=household,
        fatsecret=fatsecret,
        food_cache=food_cache,
        clock=clock,
        auth=auth,
    )
    base_url = auth.identity.base_url
    routes = create_protected_resource_routes(
        resource_url=AnyHttpUrl(auth.identity.resource),
        authorization_servers=[AnyHttpUrl(base_url)],
        scopes_supported=["diary"],
    )

    authorization_metadata = Route(
        path="/.well-known/oauth-authorization-server",
        endpoint=MetadataHandler(_authorization_metadata(base_url)).handle,
    )
    health = Route(
        path="/health",
        endpoint=_health,
    )
    authorize = Route(
        path="/authorize",
        endpoint=auth.authorize,
        methods=["GET", "POST"],
    )
    token = Route(
        path="/token",
        endpoint=auth.token,
        methods=["POST"],
    )
    tools = Route(
        path="/mcp",
        endpoint=mcp,
    )

    routes.extend([authorization_metadata, health, authorize, token, tools])

    for name in PUBLIC_FILES:
        public_file = Route(
            path="/" + name,
            endpoint=_public_file,
        )
        routes.append(public_file)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with AsyncExitStack() as clients:
            if metadata_http is None:
                await clients.enter_async_context(metadata_client)

            async with food_cache.lifespan(), anyio.create_task_group() as tasks:
                mcp.tasks = tasks

                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()

    body_limit = Middleware(
        cls=RequestBodyLimitMiddleware,
        max_body_size=65536,
    )

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[body_limit],
    )


class _UserServers:
    """Start each User's MCP tools on first use, with lifetimes owned by the HTTP app.

    All Users share one Food cache, since catalog data isn't per User.
    """

    def __init__(
        self,
        household: Household,
        fatsecret: FatSecretGateway,
        food_cache: FoodCache,
        clock: Clock,
        auth: ConnectorAuth,
    ) -> None:
        self.household = household
        self.fatsecret = fatsecret
        self.food_cache = food_cache
        self.clock = clock
        self.auth = auth
        self.apps: dict[str, Starlette] = {}
        self.lock = anyio.Lock()
        self.tasks: TaskGroup | None = None
        self.protected = RequireAuthMiddleware(
            app=self._dispatch,
            required_scopes=["diary"],
            resource_metadata_url=AnyHttpUrl(
                auth.identity.base_url + "/.well-known/oauth-protected-resource/mcp"
            ),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Mark the request with its User when the Bearer token is valid; the SDK refuses the rest."""
        request = Request(scope)
        scheme, _, raw_token = request.headers.get("authorization", "").partition(" ")
        claims = self.auth.authenticate(raw_token) if scheme.lower() == "bearer" else None

        if claims is not None:
            scope["user"] = AuthenticatedUser(_access_token(raw_token, claims))
            scope["auth"] = AuthCredentials(list(claims.scopes))

        await self.protected(scope, receive, send)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the request to the MCP app of the User named by its token."""
        principal = scope["user"]

        assert isinstance(principal, AuthenticatedUser)
        user_id = principal.access_token.subject

        assert user_id is not None

        async with self.lock:
            if user_id not in self.apps:
                assert self.tasks is not None
                user = self.household.user(user_id)
                app = await self.tasks.start(self._serve_user, user)

                self.apps[user_id] = app

        await self.apps[user_id](scope, receive, send)

    async def _serve_user(self, user: User, *, task_status: TaskStatus[Starlette]) -> None:
        # Discovery and token exchange do not need tools, diary caches or their imports.
        from fatsecret_mcp.server import build_server

        server = build_server(
            user=user,
            fatsecret=self.fatsecret,
            clock=self.clock,
            signing_key=self.household.signing_key,
            daily_requests=daily_budget(len(self.household.users)),
            food_cache=self.food_cache,
            icons=server_icons(self.auth.identity.base_url),
        )
        security = TransportSecuritySettings(
            allowed_hosts=[urlsplit(self.auth.identity.base_url).netloc],
            allowed_origins=[self.auth.identity.base_url],
        )
        app = server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )

        async with app.router.lifespan_context(app):
            task_status.started(app)
            await anyio.sleep_forever()


def _metadata_client() -> httpx2.AsyncClient:
    """Its own two connections, so slow client metadata fetches never hold the FatSecret client's."""
    limits = httpx2.Limits(
        max_connections=2,
        max_keepalive_connections=2,
    )

    return httpx2.AsyncClient(
        timeout=5,
        limits=limits,
    )


def _authorization_metadata(base_url: str) -> OAuthMetadata:
    return OAuthMetadata(
        issuer=AnyHttpUrl(base_url),
        authorization_endpoint=AnyHttpUrl(base_url + "/authorize"),
        token_endpoint=AnyHttpUrl(base_url + "/token"),
        response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["none"],
        code_challenge_methods_supported=["S256"],
        client_id_metadata_document_supported=True,
        scopes_supported=["diary"],
        authorization_response_iss_parameter_supported=True,
    )


def _access_token(raw_token: str, claims: AccessClaims) -> AccessToken:
    """Our verified claims, in the shape the SDK's auth middleware expects."""
    return AccessToken(
        token=raw_token,
        client_id=claims.client_id,
        subject=claims.user_id,
        scopes=list(claims.scopes),
        resource=claims.audience,
        claims={"iss": claims.issuer},
    )


async def _health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def _public_file(request: Request) -> Response:
    """The connector's icon or favicon, for anyone: the apps fetch it without signing in."""
    name = request.url.path.removeprefix("/")

    return Response(
        content=read_file(name),
        media_type=PUBLIC_FILES[name],
        headers=_PUBLIC_FILE_HEADERS,
    )
