"""What the server needs from FatSecret, as one narrow interface.

`FatSecretApi` in `client.py` implements it against the real API; the tests use an in-memory fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from fatsecret_mcp.domain.models import CatalogFood, DraftEntry, EntryEdit, Food, FoodEntry, Meal, Nutrition


@dataclass(frozen=True)
class ConsumerKey:
    """The server's own OAuth 1.0 consumer key and secret from the FatSecret developer console."""

    key: str = field(repr=False)
    secret: str = field(repr=False)


@dataclass(frozen=True)
class AccessToken:
    """A User's OAuth 1.0 access token and secret, which act on their own FatSecret account."""

    token: str = field(repr=False)
    secret: str = field(repr=False)


@dataclass(frozen=True)
class RequestToken:
    """A short-lived OAuth 1.0 token from the PIN flow, which the User approves on FatSecret's site."""

    token: str = field(repr=False)
    secret: str = field(repr=False)


class FatSecretGateway(Protocol):
    async def search_catalog(self, query: str, *, max_results: int | None = None) -> list[CatalogFood]:
        """The first page of catalog matches, in FatSecret's order; no User credentials needed."""
        ...

    async def get_food(self, food_id: str) -> Food:
        """A catalog Food with Servings, including derived Servings that cannot be logged (id 0)."""
        ...

    async def list_food_entries(self, user: AccessToken, day: date) -> list[FoodEntry]:
        """The User's Food Entries for one Diary Day; an empty list when there are none."""
        ...

    async def get_month_totals(self, user: AccessToken, month: date) -> dict[date, Nutrition]:
        """Per-day totals for the calendar month containing `month`.

        Missing days have no Food Entries. All-zero days may also have no Food Entries after deletion;
        use `list_food_entries` to distinguish those from entries with zero nutrition.
        """
        ...

    async def create_food_entry(self, user: AccessToken, entry: DraftEntry, meal: Meal, day: date) -> str:
        """Write one Food Entry to the User's Food Diary under `entry.entry_name`; returns its id.

        It works for any Food the User could log in the app, including Foods the catalog can't return.
        """
        ...

    async def edit_food_entry(self, user: AccessToken, edit: EntryEdit) -> None:
        """Change one Food Entry's units, Meal or name in place, sending only the values that are set.

        It works for Brand Foods too. A Food Entry's Food and Diary Day can't change.
        """
        ...

    async def delete_food_entry(self, user: AccessToken, food_entry_id: str) -> None:
        """Remove one Food Entry; FatSecret answers success even when it was already removed."""
        ...

    async def request_token(self) -> RequestToken:
        """Start the PIN flow: a request token for the User to approve; no User credentials needed."""
        ...

    def authorize_url(self, request_token: RequestToken) -> str:
        """Where the User signs in with their own FatSecret account and approves; FatSecret shows a PIN."""
        ...

    async def exchange_pin(self, request_token: RequestToken, pin: str) -> AccessToken:
        """Finish the PIN flow: the access token for the account that approved `request_token`."""
        ...
