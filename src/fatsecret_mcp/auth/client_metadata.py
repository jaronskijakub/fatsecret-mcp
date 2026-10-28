"""The connector apps the server knows: their pinned identities and callbacks, what their published
metadata must allow, and how rarely the server fetches it.
"""

from __future__ import annotations

from collections.abc import Iterable

import anyio
import httpx2

from fatsecret_mcp.auth.models import ClientDocument, FetchLimited, KnownClient
from fatsecret_mcp.domain.diary_day import Clock
from fatsecret_mcp.limits import RollingWindow

# Each identity and callback is pinned exactly, so no other document on the same host can choose
# where a User's code is sent. This one serves Claude's web, desktop and mobile apps.
CLAUDE = KnownClient(
    config_name="claude",
    app_name="Claude",
    client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
    callbacks=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
)
# ChatGPT's stable pair, which it uses with a server that supports CIMD and RFC 9207 issuer
# identification.
CHATGPT = KnownClient(
    config_name="chatgpt",
    app_name="ChatGPT",
    client_id="https://chatgpt.com/oauth/client.json",
    callbacks=frozenset({"https://chatgpt.com/connector_platform_oauth_redirect"}),
)
KNOWN_CLIENTS = {client.config_name: client for client in (CLAUDE, CHATGPT)}
CLIENT_LIFETIME = 3600
FETCHES_PER_MINUTE = 10
_MAX_METADATA_BYTES = 65536


class ClientMetadata:
    """The metadata of the clients the Operator turned on, each kept for an hour and fetched one
    request at a time.

    Only a usable document is kept, so a failed fetch is retried by the next sign-in. Fetches
    are limited server-wide, so a flood of sign-in pages can't make a client's host limit this server.
    """

    def __init__(self, http: httpx2.AsyncClient, clock: Clock, clients: Iterable[KnownClient]) -> None:
        self.http = http
        self.clock = clock
        self.clients = {client.client_id: client for client in clients}
        self.cached: dict[str, ClientDocument] = {}
        self.expires_at: dict[str, float] = {}
        self.fetches = RollingWindow(
            limit=FETCHES_PER_MINUTE,
            seconds=60,
        )
        # One lock per client, so a slow fetch of one client's document never holds another's.
        self.locks = {client_id: anyio.Lock() for client_id in self.clients}

    def known_client(self, client_id: str) -> KnownClient | None:
        """The turned-on client with exactly this ID, or None for anything else."""
        return self.clients.get(client_id)

    async def lookup(self, client: KnownClient) -> ClientDocument | FetchLimited | None:
        """The client's usable document, a refusal while fetches are used up, or None."""
        client_id = client.client_id

        async with self.locks[client_id]:
            # Read after waiting for the lock, which a slow fetch ahead may have held.
            now = self.clock().timestamp()
            cached = self.cached.get(client_id)

            if cached is not None and now < self.expires_at[client_id]:
                return cached

            retry_after = self.fetches.retry_after(now)

            if retry_after is not None:
                return FetchLimited(retry_after=retry_after)

            document = await _fetch(self.http, client_id)

            if document is not None:
                self.cached[client_id] = document
                self.expires_at[client_id] = now + CLIENT_LIFETIME

            return document


async def _fetch(http: httpx2.AsyncClient, client_id: str) -> ClientDocument | None:
    """The published client, or None for an unusable document."""
    body = await _download(http, client_id)

    if body is None:
        return None

    try:
        document = ClientDocument.model_validate_json(body)
    except ValueError:
        return None

    if not _is_public_code_client(document, client_id):
        return None

    return document


async def _download(http: httpx2.AsyncClient, url: str) -> bytes | None:
    """The response body, or None on a failure, a redirect or more than the size limit."""
    body = bytearray()

    try:
        async with http.stream("GET", url, follow_redirects=False, timeout=5) as response:
            if response.status_code != 200:
                return None

            async for chunk in response.aiter_bytes():
                body.extend(chunk)

                if len(body) > _MAX_METADATA_BYTES:
                    return None
    except (httpx2.HTTPError, ValueError):
        return None

    return bytes(body)


def _is_public_code_client(document: ClientDocument, client_id: str) -> bool:
    """A client without a secret that describes itself, uses the authorization-code flow and can
    authenticate as a public client, which is all the server offers.
    """
    if document.client_id != client_id or document.client_secret or not _supports_public_auth(document):
        return False

    return "authorization_code" in document.grant_types and "code" in document.response_types


def _supports_public_auth(document: ClientDocument) -> bool:
    """`none` as its declared token endpoint method, or among the methods it also supports."""
    supported = document.token_endpoint_auth_methods_supported or []

    return document.token_endpoint_auth_method == "none" or "none" in supported
