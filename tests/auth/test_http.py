"""Connector sign-in through HTTP, followed by calls to the User's MCP tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import threading
import weakref
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest
from pydantic import SecretStr

from fatsecret_mcp.config.loader import HouseholdConfigError
from fatsecret_mcp.config.models import Household, User
from fatsecret_mcp.domain.models import Food, Meal, Nutrition, Serving
from fatsecret_mcp.fatsecret import food_cache as food_cache_module
from fatsecret_mcp.http import build_http_app
from fatsecret_mcp.icons.links import PUBLIC_FILES, WEBSITE_URL
from fatsecret_mcp.icons.marks import mark_svg, read_file
from tests.conftest import FakeClock
from tests.fatsecret.fake import FakeFatSecret, food_entry
from tests.test_server import wait_until_collected

pytestmark = pytest.mark.anyio

BASE_URL = "https://diary.example.com"
RESOURCE = BASE_URL + "/mcp"
CLIENT_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT = {"client_id": CHATGPT_ID, "redirect_uri": CHATGPT_CALLBACK}
PASSWORD = "correct-horse-battery"
PASSWORD_HASH = (
    "scrypt$32768$8$1$30313233343536373839616263646566$"
    "45bc2b0be1732b0a114a7e40f77488990681517558e2f3b031cbf0246bb4d89149"
    "adbef2390b15f2c7cb17c076a970b1eefabef68ff92acb9171a351d4f37832"
)
# The verifier/challenge pair from RFC 7636, Appendix B.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
TODAY = date(
    year=2026,
    month=10,
    day=14,
)


class MetadataStall:
    """Holds Claude's metadata responses once started, as a slow claude.ai would."""

    def __init__(self) -> None:
        self.held: anyio.Event | None = None
        self.reached = anyio.Event()
        self.waiting = 0

    def hold(self) -> None:
        self.held = anyio.Event()

    def release(self) -> None:
        assert self.held is not None
        self.held.set()

    async def wait(self) -> None:
        if self.held is None:
            return

        self.waiting += 1
        self.reached.set()

        try:
            await self.held.wait()
        finally:
            self.waiting -= 1


@pytest.fixture
def remote_household(household: Household) -> Household:
    users = [
        user.model_copy(update={"connector_password_hash": SecretStr(PASSWORD_HASH)})
        for user in household.users
    ]

    return household.model_copy(update={"public_base_url": BASE_URL, "users": tuple(users)})


@pytest.fixture
def client_metadata() -> dict[str, object]:
    return {
        "client_id": CLIENT_ID,
        "client_name": "Claude",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }


@pytest.fixture
def chatgpt_metadata() -> dict[str, object]:
    """Shaped like the document ChatGPT publishes: a private_key_jwt client that also allows none."""
    return {
        "client_id": CHATGPT_ID,
        "client_name": "ChatGPT",
        "client_uri": "https://chatgpt.com",
        "redirect_uris": [CHATGPT_CALLBACK],
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


@pytest.fixture
def both_apps(remote_household: Household) -> Household:
    return remote_household.model_copy(update={"connector_clients": ("claude", "chatgpt")})


@pytest.fixture
async def both_http(
    both_apps: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
    chatgpt_metadata: dict[str, object],
    metadata_requests: list[str],
) -> AsyncIterator[httpx2.AsyncClient]:
    """A server with ChatGPT turned on beside Claude, each publishing its own document."""
    async with serve(
        both_apps,
        fatsecret,
        clock,
        client_metadata,
        metadata_requests,
        other_documents={CHATGPT_ID: chatgpt_metadata},
    ) as client:
        yield client


@pytest.fixture
def metadata_requests() -> list[str]:
    return []


@pytest.fixture
def metadata_stall() -> MetadataStall:
    return MetadataStall()


@pytest.fixture
def password_checks(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    scrypt = hashlib.scrypt
    checks: list[bytes] = []

    def derive(password: bytes, *, salt: bytes, n: int, r: int, p: int, maxmem: int, dklen: int) -> bytes:
        checks.append(password)

        return scrypt(
            password,
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=maxmem,
            dklen=dklen,
        )

    monkeypatch.setattr(hashlib, "scrypt", derive)

    return checks


@pytest.fixture
async def http(
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
    metadata_requests: list[str],
    metadata_stall: MetadataStall,
) -> AsyncIterator[httpx2.AsyncClient]:
    async with serve(
        remote_household,
        fatsecret,
        clock,
        client_metadata,
        metadata_requests,
        metadata_stall,
    ) as client:
        yield client


@asynccontextmanager
async def serve(
    household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
    metadata_requests: list[str] | None = None,
    metadata_stall: MetadataStall | None = None,
    other_documents: dict[str, dict[str, object]] | None = None,
) -> AsyncGenerator[httpx2.AsyncClient]:
    """Start the connector app for a Household, as a deploy with that configuration would.

    Every metadata URL serves `client_metadata`, except those `other_documents` publishes.
    """
    base_url = household.public_base_url
    fetched_urls = metadata_requests if metadata_requests is not None else []

    assert base_url is not None

    stall = metadata_stall if metadata_stall is not None else MetadataStall()
    documents = other_documents or {}

    async def metadata_response(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)

        fetched_urls.append(url)
        await stall.wait()

        return httpx2.Response(
            status_code=200,
            json=documents.get(url, client_metadata),
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(metadata_response)) as metadata_http:
        app = build_http_app(household, fatsecret, clock=clock, metadata_http=metadata_http)

        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app),
                base_url=base_url,
            ) as client,
        ):
            yield client


async def open_authorize(http: httpx2.AsyncClient, **overrides: str) -> httpx2.Response:
    """Claude opening the sign-in page, as its connector does."""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "state": "client-state",
    }

    params.update(overrides)

    return await http.get("/authorize", params=params)


async def login_form(http: httpx2.AsyncClient, **overrides: str) -> dict[str, str]:
    response = await open_authorize(http, **overrides)

    assert response.status_code == 200, response.text
    hidden = re.search(r'name="request" value="([^"]+)"', response.text)

    assert hidden is not None
    return {"request": unescape(hidden.group(1))}


async def authorize(
    http: httpx2.AsyncClient, username: str = "jakub", password: str = PASSWORD, **overrides: str
) -> str:
    data = await login_form(http, **overrides)

    data.update(username=username, password=password)
    response = await http.post("/authorize", data=data)

    assert response.status_code == 302, response.text
    callback = urlsplit(response.headers["location"])
    query = parse_qs(callback.query)

    assert callback._replace(query="").geturl() == overrides.get("redirect_uri", CALLBACK)
    assert query["state"] == ["client-state"]
    assert query["iss"] == [str(http.base_url).rstrip("/") + "/"]
    return query["code"][0]


async def exchange(http: httpx2.AsyncClient, code: str, **overrides: str) -> httpx2.Response:
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK,
        "code": code,
        "code_verifier": VERIFIER,
        "resource": RESOURCE,
    }

    data.update(overrides)

    return await http.post("/token", data=data)


async def refresh(http: httpx2.AsyncClient, refresh_token: str, **overrides: str) -> httpx2.Response:
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
        "resource": RESOURCE,
    }

    data.update(overrides)

    return await http.post("/token", data=data)


async def sign_in(
    http: httpx2.AsyncClient, username: str = "jakub", password: str = PASSWORD, **app: str
) -> dict[str, Any]:
    """Sign in through Claude, or through the app whose `client_id` and `redirect_uri` are given."""
    code = await authorize(http, username, password, **app)
    response = await exchange(http, code, **app)

    assert response.status_code == 200, response.text

    return response.json()


async def fail_sign_in(http: httpx2.AsyncClient, username: str, password: str) -> httpx2.Response:
    data = await login_form(http)

    data.update(username=username, password=password)

    return await http.post("/authorize", data=data)


def bump_if_jakub(user: User) -> User:
    if user.id != "jakub":
        return user

    return user.model_copy(update={"session_generation": user.session_generation + 1})


def visible_page(response: httpx2.Response) -> str:
    """The login page without its signed request, which differs on every form."""
    return re.sub(r'name="request" value="[^"]+"', 'name="request"', response.text)


def with_users(household: Household, users: list[User]) -> Household:
    return household.model_copy(update={"users": tuple(users)})


async def diary(http: httpx2.AsyncClient, token: str) -> httpx2.Response:
    return await call_tool(http, token, "get_diary_day", {})


async def call_tool(
    http: httpx2.AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> httpx2.Response:
    return await http.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "MCP-Protocol-Version": "2026-07-28",
            "MCP-Method": "tools/call",
            "MCP-Name": name,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )


async def test_discovery_describes_the_resource_and_public_clients(http: httpx2.AsyncClient) -> None:
    resource = await http.get("/.well-known/oauth-protected-resource/mcp")
    authorization = await http.get("/.well-known/oauth-authorization-server")

    assert resource.status_code == 200
    assert resource.json()["resource"] == RESOURCE
    assert resource.json()["authorization_servers"] == [BASE_URL + "/"]
    assert authorization.status_code == 200
    metadata = authorization.json()

    assert metadata["issuer"] == BASE_URL + "/"
    assert metadata["authorization_endpoint"] == BASE_URL + "/authorize"
    assert metadata["token_endpoint"] == BASE_URL + "/token"
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert "registration_endpoint" not in metadata

    assert (await http.post("/register")).status_code == 404


async def test_health_is_public_but_mcp_requires_a_bearer_token(http: httpx2.AsyncClient) -> None:
    health = await http.get("/health")
    mcp = await http.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert mcp.status_code == 401
    assert (
        'resource_metadata="' + BASE_URL + '/.well-known/oauth-protected-resource/mcp"'
        in (mcp.headers["www-authenticate"])
    )


async def test_the_icons_are_public_and_cacheable(http: httpx2.AsyncClient) -> None:
    for name, media_type in PUBLIC_FILES.items():
        response = await http.get("/" + name)

        assert response.status_code == 200
        assert response.headers["content-type"] == media_type
        assert response.headers["cache-control"] == "public, max-age=86400"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.content == read_file(name)


async def test_the_connector_names_its_icons_on_its_own_origin_and_its_website(
    http: httpx2.AsyncClient,
) -> None:
    token = await exchange(http, await authorize(http))
    response = await diary(http, token.json()["access_token"])
    info = response.json()["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
    sources = [icon["src"] for icon in info["icons"]]

    assert info["websiteUrl"] == WEBSITE_URL
    assert sources == [BASE_URL + "/fs-512.png", BASE_URL + "/fs-256.png", BASE_URL + "/fs-64.png"]

    for source in sources:
        icon = await http.get(source)

        assert icon.status_code == 200


async def test_http_startup_requires_passwords_for_every_user(
    household: Household, fatsecret: FakeFatSecret
) -> None:
    remote = household.model_copy(update={"public_base_url": BASE_URL})

    with pytest.raises(HouseholdConfigError, match="connector_password_hash"):
        build_http_app(remote, fatsecret)


async def test_sign_in_then_pkce_exchange_allows_a_stateless_diary_call(
    http: httpx2.AsyncClient, fatsecret: FakeFatSecret, household: Household
) -> None:
    fatsecret.add_food_entry(
        household.user("jakub").fatsecret,
        food_entry("Owsianka", Meal.BREAKFAST, TODAY, kcal=350),
    )
    code = await authorize(http)
    token = await exchange(http, code)

    assert token.status_code == 200, token.text
    assert token.json()["token_type"] == "Bearer"
    assert token.json()["expires_in"] == 3600
    assert token.headers["cache-control"] == "no-store"
    response = await diary(http, token.json()["access_token"])

    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    result = response.json()["result"]

    assert result["isError"] is False
    assert result["structuredContent"]["totals"]["kcal"] == 350
    assert result["structuredContent"]["daily_targets"]["kcal"] == 2500


async def test_wrong_password_and_unknown_user_have_the_same_error(http: httpx2.AsyncClient) -> None:
    data = await login_form(http)

    data.update(username="jakub", password="wrong-password")
    wrong_password = await http.post("/authorize", data=data)

    data.update(username="stranger", password=PASSWORD)
    unknown_user = await http.post("/authorize", data=data)

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.text == unknown_user.text
    assert "Invalid user name or password" in wrong_password.text
    assert "location" not in wrong_password.headers
    assert PASSWORD not in unknown_user.text
    assert "Jakub" not in unknown_user.text
    assert "Ola" not in unknown_user.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_verifier": "x" * 43},
        {"code_verifier": "too-short"},
        {"redirect_uri": "https://evil.example.com/callback"},
        {"redirect_uri": "https://claude.ai:443/api/mcp/auth_callback"},
        {"client_id": "https://claude.ai/another-client"},
        {"resource": "https://another.example.com/mcp"},
        {"resource": ""},
    ],
)
async def test_the_code_is_bound_to_pkce_redirect_client_and_resource(
    http: httpx2.AsyncClient, overrides: dict[str, str]
) -> None:
    code = await authorize(http)
    response = await exchange(http, code, **overrides)

    assert response.status_code == 400
    assert "access_token" not in response.json()
    assert (await exchange(http, code)).status_code == 200


async def test_a_code_is_single_use_even_with_two_simultaneous_exchanges(http: httpx2.AsyncClient) -> None:
    import asyncio

    code = await authorize(http)
    responses = await asyncio.gather(exchange(http, code), exchange(http, code))
    statuses = sorted(response.status_code for response in responses)

    assert statuses == [200, 400]


async def test_a_code_expires_after_sixty_seconds(http: httpx2.AsyncClient, clock: FakeClock) -> None:
    code = await authorize(http)

    clock.now += timedelta(seconds=60)
    response = await exchange(http, code)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_an_access_token_expires_after_one_hour(http: httpx2.AsyncClient, clock: FakeClock) -> None:
    code = await authorize(http)
    response = await exchange(http, code)
    token = response.json()["access_token"]

    clock.now += timedelta(seconds=3599)

    assert (await diary(http, token)).status_code == 200

    clock.now += timedelta(seconds=1)

    assert (await diary(http, token)).status_code == 401


async def test_two_signed_in_users_see_only_their_own_diary(
    http: httpx2.AsyncClient, fatsecret: FakeFatSecret, household: Household
) -> None:
    import asyncio

    fatsecret.add_food_entry(
        household.user("jakub").fatsecret,
        food_entry("Owsianka Jakuba", Meal.BREAKFAST, TODAY, kcal=350),
    )
    fatsecret.add_food_entry(
        household.user("ola").fatsecret,
        food_entry("Kanapka Oli", Meal.LUNCH, TODAY, kcal=420),
    )
    jakub_code = await authorize(http, "jakub")
    jakub_token = (await exchange(http, jakub_code)).json()["access_token"]
    ola_code = await authorize(http, "ola")
    ola_token = (await exchange(http, ola_code)).json()["access_token"]
    jakub, ola = await asyncio.gather(diary(http, jakub_token), diary(http, ola_token))

    assert jakub.status_code == ola.status_code == 200
    assert "Owsianka Jakuba" in jakub.text
    assert "Kanapka Oli" not in jakub.text
    assert "Kanapka Oli" in ola.text
    assert "Owsianka Jakuba" not in ola.text
    assert ola.json()["result"]["structuredContent"]["daily_targets"]["kcal"] == 1900


async def test_a_food_one_user_fetched_is_reused_for_another_user_and_released_while_idle(
    http: httpx2.AsyncClient, fatsecret: FakeFatSecret, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    short_ttl = timedelta(milliseconds=25)
    serving = Serving(
        id="rice-100g",
        description="100 g",
        metric_amount=100,
        metric_unit="g",
        number_of_units=100,
        nutrition=Nutrition(kcal=130),
    )
    food = Food(
        id="rice",
        name="Cooked Rice",
        brand=None,
        servings=[serving],
    )
    reference = weakref.ref(food)

    monkeypatch.setattr(food_cache_module, "FOOD_CACHE_TTL", short_ttl)
    fatsecret.add_food(food)
    fatsecret.set_search_results("rice", [food])
    del food

    jakub_token = (await sign_in(http, "jakub"))["access_token"]
    ola_token = (await sign_in(http, "ola"))["access_token"]

    await call_tool(http, jakub_token, "search_foods", {"description": "rice"})
    found = await call_tool(http, ola_token, "search_foods", {"description": "rice"})

    assert found.json()["result"]["structuredContent"]["status"] == "found"
    # Jakub's search and its one Food, then only Ola's search.
    assert fatsecret.requests == 2 + 1

    fatsecret.remove_food("rice")
    clock.now += short_ttl
    await wait_until_collected(reference)


async def test_a_token_for_another_resource_is_rejected_even_with_the_same_signing_key(
    http: httpx2.AsyncClient,
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
) -> None:
    foreign_base = "https://foreign.example.com"
    foreign = remote_household.model_copy(update={"public_base_url": foreign_base})

    async with serve(foreign, fatsecret, clock, client_metadata) as other:
        code = await authorize(other, resource=foreign_base + "/mcp")
        response = await exchange(other, code, resource=foreign_base + "/mcp")

        assert response.status_code == 200
        token = response.json()["access_token"]

    assert (await diary(http, token)).status_code == 401


async def test_tampered_tokens_and_codes_are_rejected(http: httpx2.AsyncClient) -> None:
    code = await authorize(http)

    assert (await exchange(http, "changed" + code)).status_code == 400

    token = (await exchange(http, code)).json()["access_token"]

    assert (await diary(http, "changed" + token)).status_code == 401
    assert (await diary(http, code)).status_code == 401


async def test_passwords_codes_tokens_and_keys_never_appear_in_debug_logs(
    http: httpx2.AsyncClient, remote_household: Household, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    code = await authorize(http)
    response = await exchange(http, code)
    token = response.json()["access_token"]

    assert (await diary(http, token)).status_code == 200
    assert (await exchange(http, "invalid-code-secret")).status_code == 400

    for secret in [PASSWORD, PASSWORD_HASH, code, token, "invalid-code-secret"]:
        assert secret not in caplog.text
    assert remote_household.signing_key.get_secret_value() not in caplog.text


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("redirect_uri", "https://evil.example.com/callback"),
        ("redirect_uri", CALLBACK + "/extra"),
        ("resource", "https://evil.example.com/mcp"),
        ("resource", ""),
        ("code_challenge_method", "plain"),
        ("code_challenge", "short"),
        ("scope", "unknown-scope"),
        ("client_id", "http://127.0.0.1/metadata"),
        ("client_id", "https://169.254.169.254/metadata"),
        ("client_id", "https://claude.ai.evil.example/metadata"),
        ("client_id", "https://[malformed"),
    ],
)
async def test_invalid_authorization_parameters_never_issue_a_code(
    http: httpx2.AsyncClient, parameter: str, value: str
) -> None:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }

    params[parameter] = value
    response = await http.get("/authorize", params=params)

    assert response.status_code == 400
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_id", "https://claude.ai/another-identity"),
        ("token_endpoint_auth_method", "client_secret_basic"),
        ("redirect_uris", ["https://claude.ai/different-callback"]),
    ],
)
async def test_the_published_client_identity_and_redirects_are_required(
    http: httpx2.AsyncClient, client_metadata: dict[str, object], field: str, value: object
) -> None:
    client_metadata[field] = value
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )

    assert response.status_code == 400


async def test_a_foreign_claude_ai_client_cannot_send_the_code_to_its_own_callback(
    http: httpx2.AsyncClient, client_metadata: dict[str, object], metadata_requests: list[str]
) -> None:
    foreign_client = "https://claude.ai/uploads/attacker-metadata.json"
    attacker_callback = "https://attacker.example.com/callback"

    client_metadata.update(client_id=foreign_client, redirect_uris=[attacker_callback])
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": foreign_client,
            "redirect_uri": attacker_callback,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "set-cookie" not in response.headers
    assert metadata_requests == []


@pytest.mark.parametrize(
    "client_id",
    [
        "https://claude.ai/another-client",
        CLIENT_ID + "?x=1",
        "https://claude.ai/api/oauth/mcp-oauth-client-metadata",
    ],
)
async def test_any_other_claude_ai_client_is_refused_without_fetching_it(
    http: httpx2.AsyncClient, metadata_requests: list[str], client_id: str
) -> None:
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert metadata_requests == []


async def test_a_callback_listed_in_the_metadata_must_also_be_claudes(
    http: httpx2.AsyncClient, client_metadata: dict[str, object]
) -> None:
    attacker_callback = "https://attacker.example.com/callback"

    client_metadata["redirect_uris"] = [CALLBACK, attacker_callback]
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": attacker_callback,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "set-cookie" not in response.headers


async def test_the_sign_in_page_names_the_host_that_receives_the_code(
    http: httpx2.AsyncClient, metadata_requests: list[str]
) -> None:
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )

    assert response.status_code == 200
    assert "your browser returns to <strong>claude.ai</strong>" in visible_page(response)
    assert metadata_requests == [CLIENT_ID]


async def test_the_sign_in_page_is_styled_only_by_its_own_stylesheet(http: httpx2.AsyncClient) -> None:
    response = await open_authorize(http)
    page = response.text
    policy = response.headers["content-security-policy"]
    stylesheets = re.findall(r"<style>(.*?)</style>", page, flags=re.DOTALL)

    assert len(stylesheets) == 1

    digest = hashlib.sha256(stylesheets[0].encode()).digest()
    allowed_stylesheet = f"style-src 'sha256-{base64.b64encode(digest).decode()}'"

    assert allowed_stylesheet in policy
    assert policy.startswith("default-src 'none';")
    assert "<script" not in page
    assert " style=" not in page


async def test_the_sign_in_page_shows_the_connector_mark_and_its_author(http: httpx2.AsyncClient) -> None:
    page = visible_page(await open_authorize(http))

    assert mark_svg("fs.svg") in page
    assert mark_svg("jj.svg") in page
    assert f'<a href="{WEBSITE_URL}"' in page


async def test_claudes_metadata_is_fetched_once_an_hour(
    http: httpx2.AsyncClient, clock: FakeClock, metadata_requests: list[str]
) -> None:
    for _ in range(3):
        await login_form(http)

    assert metadata_requests == [CLIENT_ID]

    clock.now += timedelta(minutes=59)

    await login_form(http)

    assert metadata_requests == [CLIENT_ID]

    clock.now += timedelta(minutes=1)

    await login_form(http)

    assert metadata_requests == [CLIENT_ID, CLIENT_ID]


async def test_metadata_fetches_are_limited_server_wide_and_failures_are_not_kept(
    http: httpx2.AsyncClient,
    client_metadata: dict[str, object],
    clock: FakeClock,
    metadata_requests: list[str],
) -> None:
    client_metadata["token_endpoint_auth_method"] = "client_secret_basic"

    for _ in range(10):
        assert (await open_authorize(http)).status_code == 400

    refused = await open_authorize(http)

    assert len(metadata_requests) == 10
    assert refused.status_code == 503
    assert refused.json()["error"] == "temporarily_unavailable"
    assert refused.headers["retry-after"] == "60"
    assert "set-cookie" not in refused.headers

    client_metadata["token_endpoint_auth_method"] = "none"
    clock.now += timedelta(seconds=60)

    await login_form(http)

    assert len(metadata_requests) == 11


async def test_slow_metadata_under_an_authorize_flood_leaves_signed_in_users_working(
    http: httpx2.AsyncClient,
    clock: FakeClock,
    metadata_requests: list[str],
    metadata_stall: MetadataStall,
) -> None:
    signed_in = await sign_in(http)
    clock.now += timedelta(hours=1)

    tokens = (await refresh(http, signed_in["refresh_token"])).json()
    forms: list[dict[str, str]] = []

    async def open_form() -> None:
        forms.append(await login_form(http))

    metadata_stall.hold()

    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            for _ in range(50):
                group.start_soon(open_form)

            try:
                await metadata_stall.reached.wait()

                assert (await diary(http, tokens["access_token"])).status_code == 200
                assert metadata_stall.waiting == 1
            finally:
                metadata_stall.release()

    assert len(forms) == 50
    assert metadata_requests == [CLIENT_ID, CLIENT_ID]


async def test_the_served_connector_fetches_claudes_metadata_without_the_fatsecret_client(
    remote_household: Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fatsecret_mcp.cli.serve
    import fatsecret_mcp.http

    built: list[dict[str, object]] = []

    class Built(Exception):
        pass

    def load(environ: object) -> Household:
        return remote_household

    def record(household: Household, fatsecret: object, **options: object) -> None:
        built.append(options)

        raise Built

    monkeypatch.setattr(fatsecret_mcp.cli.serve, "load_household", load)
    monkeypatch.setattr(fatsecret_mcp.http, "build_http_app", record)

    with pytest.raises(Built):
        await fatsecret_mcp.cli.serve.serve(
            {},
            transport="http",
            host="127.0.0.1",
            port=0,
        )

    assert built == [{}]


async def test_the_login_form_requires_its_original_browser_cookie(http: httpx2.AsyncClient) -> None:
    form = await login_form(http)

    form.update(username="jakub", password=PASSWORD)
    http.cookies.clear()
    response = await http.post("/authorize", data=form)

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_login_headers_preserve_the_origin_and_allow_only_the_verified_callback(
    http: httpx2.AsyncClient,
) -> None:
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )

    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "same-origin"
    assert "form-action 'self' " + CALLBACK in response.headers["content-security-policy"]


async def test_the_login_form_rejects_cross_origin_submission(http: httpx2.AsyncClient) -> None:
    form = await login_form(http)

    form.update(username="jakub", password=PASSWORD)
    response = await http.post("/authorize", data=form, headers={"Origin": "https://evil.example.com"})

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_a_changed_or_expired_login_request_cannot_be_submitted(
    http: httpx2.AsyncClient, clock: FakeClock
) -> None:
    form = await login_form(http)

    form.update(username="jakub", password=PASSWORD)
    original = form["request"]
    form["request"] = "changed" + original

    assert (await http.post("/authorize", data=form)).status_code == 400

    form["request"] = original
    clock.now += timedelta(minutes=10)

    assert (await http.post("/authorize", data=form)).status_code == 400


async def test_http_confirmation_belongs_to_one_user_and_survives_separate_requests(
    http: httpx2.AsyncClient, fatsecret: FakeFatSecret
) -> None:
    serving = Serving(
        id="rice-100g",
        description="100 g",
        metric_amount=100,
        metric_unit="g",
        number_of_units=100,
        nutrition=Nutrition(kcal=130),
    )
    food = Food(
        id="rice",
        name="Cooked Rice",
        brand=None,
        servings=[serving],
    )

    fatsecret.add_food(food)
    jakub_code = await authorize(http, "jakub")
    jakub_token = (await exchange(http, jakub_code)).json()["access_token"]
    ola_code = await authorize(http, "ola")
    ola_token = (await exchange(http, ola_code)).json()["access_token"]
    proposal = await call_tool(
        http,
        jakub_token,
        "draft_entries",
        {"items": [{"food_id": "rice", "serving_id": "rice-100g", "number_of_units": 150}]},
    )
    draft_id = proposal.json()["result"]["structuredContent"]["draft_id"]
    arguments = {"draft_id": draft_id}
    wrong_user = await call_tool(http, ola_token, "confirm_draft", arguments)
    first = await call_tool(http, jakub_token, "confirm_draft", arguments)
    repeated = await call_tool(http, jakub_token, "confirm_draft", arguments)
    jakub_day = await diary(http, jakub_token)
    ola_day = await diary(http, ola_token)

    assert wrong_user.json()["result"]["isError"] is True
    assert first.json()["result"]["structuredContent"]["status"] == "confirmed"
    assert repeated.json()["result"]["structuredContent"]["status"] == "already_confirmed"
    assert jakub_day.json()["result"]["structuredContent"]["totals"]["kcal"] == 195
    assert ola_day.json()["result"]["structuredContent"]["totals"]["kcal"] == 0


async def test_issued_credentials_can_be_used_after_starting_a_fresh_app(
    http: httpx2.AsyncClient,
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
) -> None:
    code = await authorize(http)
    app = build_http_app(remote_household, fatsecret, clock=clock)

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            base_url=BASE_URL,
        ) as restarted,
    ):
        response = await exchange(restarted, code)

        assert response.status_code == 200
        token = response.json()["access_token"]

        assert (await diary(restarted, token)).status_code == 200

    assert (await diary(http, token)).status_code == 200


async def test_refresh_rotates_tokens_and_keeps_a_user_connected_for_weeks(
    http: httpx2.AsyncClient, clock: FakeClock
) -> None:
    first = await sign_in(http)

    assert first["refresh_token"] != first["access_token"]

    clock.now += timedelta(days=29)

    assert (await diary(http, first["access_token"])).status_code == 401

    response = await refresh(http, first["refresh_token"])

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    second = response.json()

    assert second["expires_in"] == 3600
    assert second["scope"] == "diary"
    assert second["refresh_token"] != first["refresh_token"]
    assert (await diary(http, second["access_token"])).status_code == 200

    clock.now += timedelta(days=29)

    third = await refresh(http, second["refresh_token"])

    assert third.status_code == 200
    assert (await diary(http, third.json()["access_token"])).status_code == 200


async def test_an_old_refresh_token_stays_valid_until_it_expires(
    http: httpx2.AsyncClient, clock: FakeClock
) -> None:
    first = await sign_in(http)
    rotated = await refresh(http, first["refresh_token"])

    assert rotated.status_code == 200
    assert (await refresh(http, first["refresh_token"])).status_code == 200

    clock.now += timedelta(days=30)

    expired = await refresh(http, first["refresh_token"])

    assert expired.status_code == 400
    assert expired.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"client_id": "https://claude.ai/another-client"}, "invalid_grant"),
        ({"resource": "https://another.example.com/mcp"}, "invalid_target"),
        ({"resource": ""}, "invalid_target"),
        ({"scope": "diary admin"}, "invalid_scope"),
    ],
)
async def test_a_refresh_token_is_bound_to_its_client_resource_and_scope(
    http: httpx2.AsyncClient, overrides: dict[str, str], error: str
) -> None:
    tokens = await sign_in(http)
    response = await refresh(http, tokens["refresh_token"], **overrides)

    assert response.status_code == 400
    assert response.json() == {"error": error}

    same_scope = await refresh(http, tokens["refresh_token"], scope="diary")
    no_resource = await http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"],
        },
    )

    assert same_scope.status_code == 200
    assert same_scope.json()["scope"] == "diary"
    assert no_resource.status_code == 200


async def test_codes_access_tokens_and_refresh_tokens_cannot_stand_in_for_each_other(
    http: httpx2.AsyncClient,
) -> None:
    code = await authorize(http)
    tokens = (await exchange(http, code)).json()

    assert (await refresh(http, tokens["access_token"])).status_code == 400
    assert (await refresh(http, code)).status_code == 400
    assert (await refresh(http, "changed" + tokens["refresh_token"])).status_code == 400
    assert (await diary(http, tokens["refresh_token"])).status_code == 401
    assert (await exchange(http, tokens["refresh_token"])).status_code == 400


async def test_bumping_a_session_generation_revokes_that_users_tokens_on_the_next_deploy(
    http: httpx2.AsyncClient,
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
) -> None:
    jakub = await sign_in(http, "jakub")
    ola = await sign_in(http, "ola")
    users = [bump_if_jakub(user) for user in remote_household.users]
    bumped = with_users(remote_household, users)

    async with serve(bumped, fatsecret, clock, client_metadata) as deployed:
        assert (await diary(deployed, jakub["access_token"])).status_code == 401
        assert (await refresh(deployed, jakub["refresh_token"])).json() == {"error": "invalid_grant"}
        assert (await diary(deployed, ola["access_token"])).status_code == 200
        assert (await refresh(deployed, ola["refresh_token"])).status_code == 200

        signed_in_again = await sign_in(deployed, "jakub")

        assert (await diary(deployed, signed_in_again["access_token"])).status_code == 200
        assert (await refresh(deployed, signed_in_again["refresh_token"])).status_code == 200


async def test_removing_a_user_revokes_their_tokens_and_blocks_their_sign_in(
    http: httpx2.AsyncClient,
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
) -> None:
    jakub = await sign_in(http, "jakub")
    remaining = [user for user in remote_household.users if user.id != "jakub"]
    without_jakub = with_users(remote_household, remaining)

    async with serve(without_jakub, fatsecret, clock, client_metadata) as deployed:
        removed = await fail_sign_in(deployed, "jakub", PASSWORD)
        unknown = await fail_sign_in(deployed, "stranger", PASSWORD)

        assert (await diary(deployed, jakub["access_token"])).status_code == 401
        assert (await refresh(deployed, jakub["refresh_token"])).json() == {"error": "invalid_grant"}
        assert removed.status_code == 401
        assert visible_page(removed) == visible_page(unknown)
        assert "location" not in removed.headers


async def test_repeated_failed_sign_ins_are_throttled_without_revealing_whether_the_name_exists(
    http: httpx2.AsyncClient, clock: FakeClock
) -> None:
    for _ in range(5):
        assert (await fail_sign_in(http, "jakub", "wrong-password")).status_code == 401
        assert (await fail_sign_in(http, "stranger", "wrong-password")).status_code == 401

    known = await fail_sign_in(http, " JAKUB ", PASSWORD)
    unknown = await fail_sign_in(http, "stranger", PASSWORD)

    assert known.status_code == unknown.status_code == 429
    assert visible_page(known) == visible_page(unknown)
    assert known.headers["retry-after"] == unknown.headers["retry-after"] == "900"
    assert "Too many sign-in attempts" in known.text
    assert "location" not in known.headers
    assert (await sign_in(http, "ola"))["access_token"]

    clock.now += timedelta(minutes=15)

    assert (await sign_in(http, "jakub"))["access_token"]


async def test_a_successful_sign_in_clears_earlier_failures_for_that_name(http: httpx2.AsyncClient) -> None:
    for _ in range(4):
        assert (await fail_sign_in(http, "jakub", "wrong-password")).status_code == 401

    await sign_in(http, "jakub")

    for _ in range(4):
        assert (await fail_sign_in(http, "jakub", "wrong-password")).status_code == 401

    assert (await sign_in(http, "jakub"))["access_token"]


async def test_failed_sign_ins_under_many_names_do_not_lock_out_the_household(
    http: httpx2.AsyncClient,
) -> None:
    for attempt in range(30):
        assert (await fail_sign_in(http, f"guess-{attempt}", "wrong-password")).status_code == 401

    assert (await sign_in(http, "jakub"))["access_token"]


async def test_very_long_names_share_one_throttle(http: httpx2.AsyncClient) -> None:
    prefix = "x" * 200

    for attempt in range(5):
        assert (await fail_sign_in(http, f"{prefix}{attempt}", "wrong-password")).status_code == 401

    assert (await fail_sign_in(http, prefix, "wrong-password")).status_code == 429


async def test_simultaneous_failed_sign_ins_cannot_outrun_the_throttle(http: httpx2.AsyncClient) -> None:
    import asyncio

    data = await login_form(http)

    data.update(username="jakub", password="wrong-password")

    attempts = [http.post("/authorize", data=data) for _ in range(8)]
    responses = await asyncio.gather(*attempts)
    statuses = sorted([response.status_code for response in responses])

    accepted = statuses.count(401)

    assert 1 <= accepted <= 5
    assert statuses == [401] * accepted + [429] * (8 - accepted)


async def test_one_form_cannot_run_forty_password_checks_under_different_names(
    http: httpx2.AsyncClient, password_checks: list[bytes]
) -> None:
    data = await login_form(http)
    responses: list[httpx2.Response] = []

    for attempt in range(40):
        data.update(username=f"guess-{attempt}", password="wrong-password")
        response = await http.post("/authorize", data=data)

        responses.append(response)

    statuses = [response.status_code for response in responses]

    assert statuses == [401] * 5 + [429] * 35
    assert len(password_checks) == 5
    assert "Start sign-in again" in responses[-1].text
    assert "<form" not in responses[-1].text
    assert (await sign_in(http, "jakub"))["access_token"]


async def test_a_server_wide_sign_in_limit_recovers_in_one_minute_and_preserves_connected_users(
    http: httpx2.AsyncClient, clock: FakeClock, password_checks: list[bytes]
) -> None:
    tokens = await sign_in(http)

    for attempt in range(59):
        response = await fail_sign_in(http, f"guess-{attempt}", "wrong-password")

        assert response.status_code == 401

    known = await fail_sign_in(http, "jakub", PASSWORD)
    unknown = await fail_sign_in(http, "stranger", PASSWORD)

    assert known.status_code == unknown.status_code == 429
    assert visible_page(known) == visible_page(unknown)
    assert known.headers["retry-after"] == unknown.headers["retry-after"] == "60"
    assert len(password_checks) == 60
    assert (await http.get("/health")).status_code == 200
    assert (await diary(http, tokens["access_token"])).status_code == 200
    assert (await refresh(http, tokens["refresh_token"])).status_code == 200

    clock.now += timedelta(seconds=30)
    refused = await fail_sign_in(http, "another-guess", "wrong-password")

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "30"
    assert len(password_checks) == 60

    clock.now += timedelta(seconds=30)

    assert (await sign_in(http, "jakub"))["access_token"]


async def test_a_sign_in_flood_has_two_password_workers_and_leaves_connected_users_working(
    http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = await sign_in(http)
    data = await login_form(http)
    started = anyio.Event()
    release = threading.Event()
    lock = threading.Lock()
    checks = 0
    responses: list[httpx2.Response] = []

    def derive(password: bytes, **parameters: Any) -> bytes:
        nonlocal checks

        with lock:
            checks += 1

            if checks == 2:
                anyio.from_thread.run_sync(started.set)

        assert release.wait(timeout=5), "Password workers were not released"

        return bytes(64)

    async def submit(username: str) -> None:
        response = await http.post(
            "/authorize",
            data={"request": data["request"], "username": username, "password": "wrong-password"},
        )

        responses.append(response)

    monkeypatch.setattr(hashlib, "scrypt", derive)

    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(submit, "first-guess")
            group.start_soon(submit, "second-guess")

            try:
                await started.wait()
                overflow = await http.post(
                    "/authorize",
                    data={"request": data["request"], "username": "third-guess", "password": PASSWORD},
                )

                assert overflow.status_code == 429
                assert overflow.headers["retry-after"] == "1"
                assert checks == 2
                assert (await http.get("/health")).status_code == 200
                assert (await http.get("/.well-known/oauth-authorization-server")).status_code == 200
                assert (await diary(http, tokens["access_token"])).status_code == 200
                assert (await refresh(http, tokens["refresh_token"])).status_code == 200
            finally:
                release.set()

    statuses = [response.status_code for response in responses]

    assert statuses == [401, 401]

    monkeypatch.undo()

    assert (await sign_in(http, "jakub"))["access_token"]


async def test_cancelling_sign_in_requests_does_not_release_running_password_checks(
    http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = await login_form(http)
    started = anyio.Event()
    finished = anyio.Event()
    release = threading.Event()
    lock = threading.Lock()
    checks = 0
    completed = 0

    def derive(password: bytes, **parameters: Any) -> bytes:
        nonlocal checks, completed

        with lock:
            checks += 1
            position = checks

            if checks == 2:
                anyio.from_thread.run_sync(started.set)

        if position <= 2:
            assert release.wait(timeout=5), "Password workers were not released"

            with lock:
                completed += 1

                if completed == 2:
                    anyio.from_thread.run_sync(finished.set)

        return bytes(64)

    monkeypatch.setattr(hashlib, "scrypt", derive)
    tasks = [
        asyncio.create_task(
            http.post(
                "/authorize",
                data={"request": data["request"], "username": name, "password": "wrong-password"},
            )
        )
        for name in ("first-guess", "second-guess")
    ]

    with anyio.fail_after(3):
        try:
            await started.wait()

            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)
            overflow = await http.post(
                "/authorize",
                data={"request": data["request"], "username": "third-guess", "password": PASSWORD},
            )

            assert overflow.status_code == 429
            assert checks == 2
        finally:
            release.set()
            await finished.wait()


async def test_chatgpt_is_refused_without_fetching_while_the_operator_hasnt_turned_it_on(
    http: httpx2.AsyncClient, metadata_requests: list[str]
) -> None:
    response = await open_authorize(http, **CHATGPT)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert metadata_requests == []


async def test_with_only_chatgpt_turned_on_claude_is_refused_without_fetching(
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
    chatgpt_metadata: dict[str, object],
    metadata_requests: list[str],
) -> None:
    only_chatgpt = remote_household.model_copy(update={"connector_clients": ("chatgpt",)})

    async with serve(
        only_chatgpt,
        fatsecret,
        clock,
        client_metadata,
        metadata_requests,
        other_documents={CHATGPT_ID: chatgpt_metadata},
    ) as deployed:
        claude = await open_authorize(deployed)

        assert claude.status_code == 400
        assert metadata_requests == []

        await sign_in(deployed, **CHATGPT)

    assert metadata_requests == [CHATGPT_ID]


async def test_chatgpt_signs_in_calls_a_tool_and_refreshes(
    both_http: httpx2.AsyncClient, fatsecret: FakeFatSecret, household: Household, clock: FakeClock
) -> None:
    fatsecret.add_food_entry(
        household.user("jakub").fatsecret,
        food_entry("Ryż", Meal.LUNCH, TODAY, kcal=195),
    )

    tokens = await sign_in(both_http, **CHATGPT)
    response = await diary(both_http, tokens["access_token"])

    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["totals"]["kcal"] == 195

    clock.now += timedelta(hours=1)
    refreshed = await refresh(both_http, tokens["refresh_token"], client_id=CHATGPT_ID)

    assert refreshed.status_code == 200, refreshed.text

    refreshed_diary = await diary(both_http, refreshed.json()["access_token"])

    assert refreshed_diary.status_code == 200


@pytest.mark.parametrize(
    ("client_id", "redirect_uri"),
    [
        (CLIENT_ID, CHATGPT_CALLBACK),
        (CHATGPT_ID, CALLBACK),
    ],
)
async def test_a_callback_pinned_for_the_other_app_is_refused_before_the_sign_in_page(
    both_http: httpx2.AsyncClient,
    client_metadata: dict[str, object],
    chatgpt_metadata: dict[str, object],
    client_id: str,
    redirect_uri: str,
) -> None:
    # Both documents list both callbacks, so only each app's own pinned callbacks can refuse it.
    client_metadata["redirect_uris"] = [CALLBACK, CHATGPT_CALLBACK]
    chatgpt_metadata["redirect_uris"] = [CALLBACK, CHATGPT_CALLBACK]

    response = await open_authorize(both_http, client_id=client_id, redirect_uri=redirect_uri)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "set-cookie" not in response.headers


async def test_alternating_sign_ins_fetch_each_apps_metadata_once(
    both_http: httpx2.AsyncClient, metadata_requests: list[str]
) -> None:
    await sign_in(both_http)
    await sign_in(both_http, **CHATGPT)
    await sign_in(both_http)
    await sign_in(both_http, **CHATGPT)

    assert metadata_requests == [CLIENT_ID, CHATGPT_ID]


@pytest.mark.parametrize(
    "changes",
    [
        {"token_endpoint_auth_methods_supported": ["private_key_jwt"]},
        {"token_endpoint_auth_methods_supported": None},
        {"client_secret": "a-secret"},
        {"client_id": CHATGPT_ID + "?connector=1"},
        {"client_id": "https://chatgpt.com/oauth/another/client.json"},
        {"grant_types": ["refresh_token"]},
    ],
)
async def test_chatgpts_document_must_allow_a_public_code_client_with_its_exact_id(
    both_http: httpx2.AsyncClient, chatgpt_metadata: dict[str, object], changes: dict[str, object]
) -> None:
    chatgpt_metadata.update(changes)

    response = await open_authorize(both_http, **CHATGPT)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


async def test_a_refresh_token_is_refused_when_the_other_app_presents_it(
    both_http: httpx2.AsyncClient,
) -> None:
    claude = await sign_in(both_http)
    chatgpt = await sign_in(both_http, **CHATGPT)

    claude_as_chatgpt = await refresh(both_http, claude["refresh_token"], client_id=CHATGPT_ID)
    chatgpt_as_claude = await refresh(both_http, chatgpt["refresh_token"], client_id=CLIENT_ID)

    assert claude_as_chatgpt.json() == {"error": "invalid_grant"}
    assert chatgpt_as_claude.json() == {"error": "invalid_grant"}


async def test_each_apps_code_is_refused_by_the_other_apps_exchange(both_http: httpx2.AsyncClient) -> None:
    code = await authorize(both_http, **CHATGPT)
    response = await exchange(both_http, code)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_the_sign_in_page_names_the_app_and_the_host_it_returns_to(
    both_http: httpx2.AsyncClient,
) -> None:
    chatgpt = visible_page(await open_authorize(both_http, **CHATGPT))
    claude = visible_page(await open_authorize(both_http))

    assert "your browser returns to <strong>chatgpt.com</strong>" in chatgpt
    assert "Sign in to let ChatGPT read your diary" in chatgpt
    assert "Connect to ChatGPT</button>" in chatgpt
    assert "your browser returns to <strong>claude.ai</strong>" in claude
    assert "Connect to Claude</button>" in claude


async def test_a_failed_chatgpt_sign_in_still_names_chatgpt(both_http: httpx2.AsyncClient) -> None:
    data = await login_form(both_http, **CHATGPT)

    data.update(username="jakub", password="wrong-password")

    response = await both_http.post("/authorize", data=data)

    assert response.status_code == 401
    assert "Connect to ChatGPT</button>" in response.text
    assert "chatgpt.com" in response.headers["content-security-policy"]


async def test_bumping_a_session_generation_signs_the_user_out_of_both_apps(
    both_http: httpx2.AsyncClient,
    both_apps: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
    chatgpt_metadata: dict[str, object],
) -> None:
    claude = await sign_in(both_http)
    chatgpt = await sign_in(both_http, **CHATGPT)
    users = [bump_if_jakub(user) for user in both_apps.users]
    bumped = with_users(both_apps, users)

    async with serve(
        bumped,
        fatsecret,
        clock,
        client_metadata,
        other_documents={CHATGPT_ID: chatgpt_metadata},
    ) as deployed:
        claude_diary = await diary(deployed, claude["access_token"])
        chatgpt_diary = await diary(deployed, chatgpt["access_token"])
        chatgpt_refresh = await refresh(deployed, chatgpt["refresh_token"], client_id=CHATGPT_ID)

    assert claude_diary.status_code == 401
    assert chatgpt_diary.status_code == 401
    assert chatgpt_refresh.json() == {"error": "invalid_grant"}


async def test_turning_chatgpt_off_signs_everyone_out_of_it_but_not_out_of_claude(
    both_http: httpx2.AsyncClient,
    remote_household: Household,
    fatsecret: FakeFatSecret,
    clock: FakeClock,
    client_metadata: dict[str, object],
) -> None:
    claude = await sign_in(both_http)
    chatgpt = await sign_in(both_http, **CHATGPT)

    async with serve(remote_household, fatsecret, clock, client_metadata) as deployed:
        chatgpt_diary = await diary(deployed, chatgpt["access_token"])
        chatgpt_refresh = await refresh(deployed, chatgpt["refresh_token"], client_id=CHATGPT_ID)
        claude_diary = await diary(deployed, claude["access_token"])
        claude_refresh = await refresh(deployed, claude["refresh_token"])

    assert chatgpt_diary.status_code == 401
    assert chatgpt_refresh.json() == {"error": "invalid_grant"}
    assert claude_diary.status_code == 200
    assert claude_refresh.status_code == 200
