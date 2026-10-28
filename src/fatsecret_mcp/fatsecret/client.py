"""The real FatSecret gateway: signed HTTP calls to the Platform API.

Calls are method-based (`rest/server.api?method=…`) with the OAuth 1.0 parameters in the query string,
the wire format verified against a Basic-tier key. Errors come back as HTTP 200 with an error code.

The PIN flow (3-legged OAuth 1.0 with `oob`) has its own endpoints, which answer with form-encoded text.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, cast
from urllib.parse import quote

import httpx2

from fatsecret_mcp.domain.models import CatalogFood, DraftEntry, EntryEdit, Food, FoodEntry, Meal, Nutrition
from fatsecret_mcp.fatsecret import errors, parsing
from fatsecret_mcp.fatsecret.errors import FatSecretError, PinRejected, UpstreamFailure
from fatsecret_mcp.fatsecret.gateway import AccessToken, ConsumerKey, RequestToken
from fatsecret_mcp.fatsecret.oauth import signed_url
from fatsecret_mcp.fatsecret.request_limits import RequestLimits

API_URL = "https://platform.fatsecret.com/rest/server.api"
REQUEST_TOKEN_URL = "https://authentication.fatsecret.com/oauth/request_token"
AUTHORIZE_URL = "https://authentication.fatsecret.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://authentication.fatsecret.com/oauth/access_token"

logger = logging.getLogger(__name__)

# httpx2 logs every request URL at INFO, and these URLs carry the OAuth token and signature.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)


@asynccontextmanager
async def connected_api(consumer: ConsumerKey) -> AsyncGenerator[FatSecretApi]:
    """The real gateway with an HTTP client of its own, closed when the block ends."""
    async with httpx2.AsyncClient(timeout=20) as http:
        yield FatSecretApi(
            consumer=consumer,
            http=http,
        )


class FatSecretApi:
    def __init__(
        self, consumer: ConsumerKey, http: httpx2.AsyncClient, *, request_limits: RequestLimits | None = None
    ) -> None:
        self._consumer = consumer
        self._http = http
        self._request_limits = request_limits if request_limits is not None else RequestLimits()

    async def search_catalog(self, query: str, *, max_results: int | None = None) -> list[CatalogFood]:
        params = {"search_expression": query}

        if max_results is not None:
            params["max_results"] = str(max_results)

        body = await self._call("foods.search", params, None)

        return parsing.catalog_foods(body)

    async def get_food(self, food_id: str) -> Food:
        body = await self._call("food.get.v5", {"food_id": food_id}, None)

        return parsing.food(body)

    async def list_food_entries(self, user: AccessToken, day: date) -> list[FoodEntry]:
        body = await self._call("food_entries.get.v2", {"date": str(parsing.day_number(day))}, user)

        return parsing.food_entries(body)

    async def get_month_totals(self, user: AccessToken, month: date) -> dict[date, Nutrition]:
        first_day = month.replace(day=1)
        body = await self._call(
            "food_entries.get_month.v2", {"date": str(parsing.day_number(first_day))}, user
        )

        return parsing.month_totals(body)

    async def create_food_entry(self, user: AccessToken, entry: DraftEntry, meal: Meal, day: date) -> str:
        params = {
            "food_id": entry.food_id,
            "food_entry_name": entry.entry_name,
            "serving_id": entry.serving_id,
            "number_of_units": parsing.number_text(entry.number_of_units),
            "meal": parsing.meal_text(meal),
            "date": str(parsing.day_number(day)),
        }
        body = await self._call("food_entry.create", params, user, http_method="POST")

        return parsing.created_food_entry_id(body)

    async def edit_food_entry(self, user: AccessToken, edit: EntryEdit) -> None:
        params = {"food_entry_id": edit.food_entry_id}

        if edit.entry_name is not None:
            params["food_entry_name"] = edit.entry_name
        if edit.number_of_units is not None:
            params["number_of_units"] = parsing.number_text(edit.number_of_units)
        if edit.meal is not None:
            params["meal"] = parsing.meal_text(edit.meal)

        body = await self._call("food_entry.edit", params, user, http_method="POST")

        parsing.check_success(body)

    async def delete_food_entry(self, user: AccessToken, food_entry_id: str) -> None:
        body = await self._call(
            "food_entry.delete", {"food_entry_id": food_entry_id}, user, http_method="POST"
        )

        parsing.check_success(body)

    async def request_token(self) -> RequestToken:
        token, secret = await self._sign_in_call(REQUEST_TOKEN_URL, {"oauth_callback": "oob"}, None)

        return RequestToken(
            token=token,
            secret=secret,
        )

    def authorize_url(self, request_token: RequestToken) -> str:
        return f"{AUTHORIZE_URL}?oauth_token={quote(request_token.token, safe='')}"

    async def exchange_pin(self, request_token: RequestToken, pin: str) -> AccessToken:
        params = {"oauth_verifier": pin.strip()}
        token, secret = await self._sign_in_call(ACCESS_TOKEN_URL, params, request_token)

        return AccessToken(
            token=token,
            secret=secret,
        )

    async def _sign_in_call(
        self, url: str, params: Mapping[str, str], request_token: RequestToken | None
    ) -> tuple[str, str]:
        """One PIN flow step; GET works, though FatSecret's docs say POST."""
        try:
            async with self._request_limits.request():
                signed = signed_url("GET", url, params, self._consumer, request_token)
                response = await self._send("GET", signed)

            # A PIN that wasn't approved, was mistyped or came too late is answered with HTTP 400.
            if response.status_code == 400 and url == ACCESS_TOKEN_URL:
                raise PinRejected()

            if response.status_code != 200:
                raise UpstreamFailure(f"HTTP {response.status_code}")

            return parsing.token_and_secret(response.text)
        except FatSecretError as error:
            logger.warning("FatSecret sign-in %s: %s", _outcome(error), type(error).__name__)
            raise

    async def _call(
        self, method: str, params: Mapping[str, str], user: AccessToken | None, *, http_method: str = "GET"
    ) -> dict[str, Any]:
        try:
            async with self._request_limits.request():
                # Sign after any pacing wait so OAuth timestamps are fresh when the request is sent.
                call = {"method": method, "format": "json"}
                call.update(params)

                url = signed_url(http_method, API_URL, call, self._consumer, user)
                body = _json_object(await self._send(http_method, url))

                if "error" in body:
                    raise _error(body["error"])
        except FatSecretError as error:
            logger.warning(
                "FatSecret %s %s: %s (code %s)", method, _outcome(error), type(error).__name__, error.code
            )
            raise

        return body

    async def _send(self, http_method: str, url: str) -> httpx2.Response:
        try:
            return await self._http.request(http_method, url)
        except httpx2.TimeoutException:
            raise UpstreamFailure("the request timed out") from None
        except httpx2.HTTPError as error:
            # The exception's text can quote the signed URL, so only its type is kept.
            raise UpstreamFailure(type(error).__name__) from None


def _json_object(response: httpx2.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise UpstreamFailure(f"HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError:
        raise UpstreamFailure("the response isn't JSON") from None

    if not isinstance(body, dict):
        raise UpstreamFailure("unexpected response")

    return cast(dict[str, Any], body)


def _outcome(error: FatSecretError) -> str:
    """How a failed request reads in the logs: FatSecret's refusal, or this server's own."""
    return "failed" if error.sent else "not sent"


def _error(error: object) -> FatSecretError:
    # FatSecret's own message can quote OAuth values, so only the code is used.
    code = parsing.error_code(error)

    if code is None:
        return UpstreamFailure("an error without a code")

    return errors.from_code(code)
